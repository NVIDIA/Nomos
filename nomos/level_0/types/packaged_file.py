"""Packaged file payloads carried by intents and writs."""

import base64
import binascii
import hashlib
import posixpath

from pydantic import BaseModel, Field, field_validator, model_validator


class PackagedFile(BaseModel):
    """A file bundled with an intent and later materialized by the executor."""

    path: str = Field(..., min_length=1, description="Relative destination path")
    content_base64: str = Field(..., min_length=1, description="Base64-encoded file bytes")
    sha256: str = Field(default="", description="SHA-256 hex digest of decoded bytes")
    mode: str = Field(default="0600", description="Octal file mode applied after unpacking")
    description: str = Field(default="", description="Human-readable purpose of this file")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("packaged file path must not contain null bytes")
        normalized = posixpath.normpath(value)
        if normalized in {"", "."}:
            raise ValueError("packaged file path must name a file")
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise ValueError(
                "packaged file path must be relative and stay within working_directory"
            )
        return normalized

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = value.strip()
        if len(mode) == 4:
            if not mode.startswith("0"):
                raise ValueError("four-digit mode strings must start with 0")
            mode_digits = mode[1:]
        else:
            mode_digits = mode
        if len(mode_digits) != 3 or any(ch not in "01234567" for ch in mode_digits):
            raise ValueError("mode must be an octal string such as 0600 or 0755")
        return "0" + mode_digits

    @model_validator(mode="after")
    def validate_hash(self) -> "PackagedFile":
        content = self.decoded_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if self.sha256 and self.sha256.lower() != digest:
            raise ValueError("packaged file sha256 does not match decoded content")
        self.sha256 = digest
        return self

    def decoded_bytes(self) -> bytes:
        """Decode the base64 payload."""
        try:
            return base64.b64decode(self.content_base64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as e:
            raise ValueError("content_base64 must be valid base64") from e

    def content_for_hashing(self) -> dict[str, str]:
        """Return canonical content for intent/writ hashing and signing."""
        return {
            "path": self.path,
            "content_base64": self.content_base64,
            "sha256": self.sha256,
            "mode": self.mode,
            "description": self.description,
        }

    def text_for_review(self, max_bytes: int) -> tuple[str, bool]:
        """Return a UTF-8 review preview and whether it was truncated."""
        raw = self.decoded_bytes()
        truncated = len(raw) > max_bytes
        preview = raw[:max_bytes]
        try:
            text = preview.decode("utf-8")
        except UnicodeDecodeError:
            text = preview.decode("utf-8", errors="replace")
        return text, truncated
