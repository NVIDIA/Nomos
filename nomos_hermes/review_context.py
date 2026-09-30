# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Privacy-bounded review context derived from Hermes model requests."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAX_REVIEW_MESSAGES = 6
MAX_MESSAGE_CHARS = 3_000
MAX_CONTEXT_BYTES = 24 * 1024
MAX_SOURCE_MESSAGE_CHARS = 12_000
MAX_SOURCE_ENVELOPES = 64
MAX_CONTENT_BLOCKS = 16
_SCHEMA = "nomos.hermes.review-context.v1"
_ALLOWED_ROLES = {"user", "assistant"}
_ALLOWED_TEXT_BLOCK_TYPES = {
    "user": {"text", "input_text"},
    "assistant": {"text", "output_text"},
}

# These patterns are deliberately small and high-confidence. They are a
# best-effort second layer after structural data minimization, not a promise
# that arbitrary user text is free of secrets or personal data.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret)\b(\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|api[_-]?key|key|password|secret|signature|token)=)[^&#\s]+"
)
_URL_USERINFO_RE = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)


@dataclass(frozen=True)
class ReviewContextSnapshot:
    """Immutable serialized context plus non-sensitive capture metadata."""

    serialized: str = field(repr=False)
    retry_count: int
    message_count: int
    redaction_count: int
    truncated: bool

    @property
    def size_bytes(self) -> int:
        """Return the serialized UTF-8 transport size."""
        return len(self.serialized.encode("utf-8"))


@dataclass(frozen=True)
class _Message:
    index: int
    role: str
    text: str
    is_current_user: bool = False


def _redact_secrets(text: str) -> tuple[str, int]:
    count = 0

    def replace(
        pattern: re.Pattern[str], replacement: str | Callable[[re.Match[str]], str]
    ) -> None:
        nonlocal text, count
        text, matches = pattern.subn(replacement, text)
        count += matches

    replace(_PRIVATE_KEY_RE, "[REDACTED PRIVATE KEY]")
    replace(_BEARER_RE, lambda match: f"{match.group(1)}[REDACTED]")
    replace(
        _ASSIGNMENT_RE,
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
    )
    replace(_QUERY_SECRET_RE, lambda match: f"{match.group(1)}[REDACTED]")
    replace(_URL_USERINFO_RE, lambda match: f"{match.group(1)}[REDACTED]@")
    return text, count


def _truncate_head_tail(text: str, limit: int = MAX_MESSAGE_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    # Compute the marker before the split and count it inside the limit.
    omitted = len(text) - limit
    marker = ""
    for _ in range(3):
        marker = f"\n...[{omitted} characters omitted]...\n"
        omitted = len(text) - (limit - len(marker))
    marker = f"\n...[{omitted} characters omitted]...\n"
    available = max(0, limit - len(marker))
    head_chars = (available * 2) // 3
    tail_chars = available - head_chars
    tail = text[-tail_chars:] if tail_chars else ""
    return f"{text[:head_chars]}{marker}{tail}", True


def _text_from_content(content: Any, *, role: str) -> str | None:
    """Accept only known text shapes; reject a message containing any other block."""

    if isinstance(content, str):
        return content if len(content) <= MAX_SOURCE_MESSAGE_CHARS else None
    if not isinstance(content, list | tuple) or len(content) > MAX_CONTENT_BLOCKS:
        return None
    parts: list[str] = []
    total_chars = 0
    for block in content:
        if not isinstance(block, Mapping):
            return None
        if len(block) > 2:
            return None
        keys = set(block)
        # Bedrock ordinary text blocks are exactly {"text": ...}. Other
        # provider shapes must carry an allowlisted, role-appropriate type.
        if keys == {"text"}:
            value = block.get("text")
        else:
            block_type = block.get("type")
            if block_type not in _ALLOWED_TEXT_BLOCK_TYPES[role]:
                return None
            if not keys.issubset({"type", "text"}):
                return None
            value = block.get("text")
        if not isinstance(value, str):
            return None
        total_chars += len(value)
        if parts:
            total_chars += 1
        if total_chars > MAX_SOURCE_MESSAGE_CHARS:
            return None
        parts.append(value)
    return "\n".join(parts)


def _extract_messages(raw_messages: Any) -> tuple[list[_Message], bool]:
    if not isinstance(raw_messages, list | tuple):
        return [], False

    extracted: list[_Message] = []
    window_start = max(0, len(raw_messages) - MAX_SOURCE_ENVELOPES)
    for index in range(len(raw_messages) - 1, window_start - 1, -1):
        raw = raw_messages[index]
        if not isinstance(raw, Mapping):
            continue
        if raw.get("type") not in (None, "message"):
            continue
        role = raw.get("role")
        if role not in _ALLOWED_ROLES:
            continue
        text = _text_from_content(raw.get("content"), role=role)
        if text is None or not text.strip():
            continue
        extracted.append(_Message(index=index, role=role, text=text))
    extracted.reverse()
    return extracted, window_start > 0


def _select_messages(messages: list[_Message]) -> tuple[list[_Message], int]:
    if not messages:
        return [], 0
    current_user_position = next(
        (
            position
            for position in range(len(messages) - 1, -1, -1)
            if messages[position].is_current_user
        ),
        None,
    )
    if current_user_position is None:
        return [], len(messages)
    selected_positions = list(range(max(0, len(messages) - MAX_REVIEW_MESSAGES), len(messages)))
    if current_user_position is not None and current_user_position not in selected_positions:
        selected_positions[0] = current_user_position
        selected_positions.sort()
    selected = [
        _Message(
            index=messages[position].index,
            role=messages[position].role,
            text=messages[position].text,
            is_current_user=messages[position].is_current_user,
        )
        for position in selected_positions
    ]
    return selected, len(messages) - len(selected)


def build_review_context(
    request_messages: Any,
    *,
    current_user_message: Any,
    retry_count: int = 0,
) -> ReviewContextSnapshot | None:
    """Build the canonical live-review projection for one Hermes API request.

    ``request_messages`` and ``current_user_message`` are raw compatibility
    fields. This function immediately performs the strict Nomos projection and
    never retains either source object.
    """

    if not isinstance(current_user_message, str):
        return None
    if len(current_user_message) > MAX_SOURCE_MESSAGE_CHARS:
        return None
    if not current_user_message.strip():
        return None
    messages, source_window_truncated = _extract_messages(request_messages)
    bounded_current = current_user_message
    matching_position = next(
        (
            position
            for position in range(len(messages) - 1, -1, -1)
            if messages[position].role == "user" and messages[position].text == bounded_current
        ),
        None,
    )
    if matching_position is None:
        next_index = messages[-1].index + 1 if messages else 0
        messages.append(
            _Message(
                index=next_index,
                role="user",
                text=bounded_current,
                is_current_user=True,
            )
        )
    else:
        current = messages[matching_position]
        messages[matching_position] = _Message(
            index=current.index,
            role=current.role,
            text=current.text,
            is_current_user=True,
        )
    selected, omitted_messages = _select_messages(messages)
    if not selected:
        return None

    redaction_count = 0
    truncated_count = 0
    projected: list[dict[str, Any]] = []
    for message in selected:
        redacted, count = _redact_secrets(message.text)
        bounded, truncated = _truncate_head_tail(redacted)
        redaction_count += count
        truncated_count += int(truncated)
        projected.append(
            {
                "current_user": message.is_current_user,
                "role": message.role,
                "text": bounded,
            }
        )

    def serialize() -> str:
        payload = {
            "messages": projected,
            "schema": _SCHEMA,
            "truncation": {
                "messages_omitted": omitted_messages,
                "messages_truncated": truncated_count,
                "source_window_truncated": source_window_truncated,
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    serialized = serialize()
    # UTF-8 is the transport cost. Drop the oldest optional message until the
    # byte bound is met; the current user is retained as the final anchor.
    while len(serialized.encode("utf-8")) > MAX_CONTEXT_BYTES and len(projected) > 1:
        drop_at = next(
            (index for index, item in enumerate(projected) if not item["current_user"]),
            None,
        )
        if drop_at is None:
            break
        projected.pop(drop_at)
        omitted_messages += 1
        serialized = serialize()
    if len(serialized.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return None

    return ReviewContextSnapshot(
        serialized=serialized,
        retry_count=max(0, int(retry_count or 0)),
        message_count=len(projected),
        redaction_count=redaction_count,
        truncated=bool(omitted_messages or truncated_count or source_window_truncated),
    )


@dataclass(frozen=True)
class _StoredSnapshot:
    snapshot: ReviewContextSnapshot | None
    retry_count: int
    captured_at: float

    @property
    def size_bytes(self) -> int:
        return self.snapshot.size_bytes if self.snapshot is not None else 0


class ReviewContextStore:
    """Thread-safe monotonic TTL/LRU store for immutable context strings."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        max_bytes: int = 6 * 1024 * 1024,
        ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0 or max_bytes <= 0 or ttl_seconds <= 0:
            raise ValueError("review context store bounds must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: OrderedDict[tuple[str, str, str, str], _StoredSnapshot] = OrderedDict()
        self._size_bytes = 0
        self._lock = threading.RLock()

    def _remove(self, key: tuple[str, str, str, str]) -> None:
        stored = self._items.pop(key)
        self._size_bytes -= stored.size_bytes

    def _prune(self, now: float) -> None:
        # Access order (LRU) and capture time (TTL) are independent: a read can
        # move an old item behind a newer one, so inspect every bounded entry.
        for key, stored in list(self._items.items()):
            if now - stored.captured_at > self._ttl_seconds:
                self._remove(key)

    def put(
        self, key: tuple[str, str, str, str], snapshot: ReviewContextSnapshot
    ) -> bool:
        """Store a captured snapshot unless a newer retry already superseded it."""
        return self._put_outcome(key, snapshot=snapshot, retry_count=snapshot.retry_count)

    def mark_unavailable(self, key: tuple[str, str, str, str], *, retry_count: int) -> bool:
        """Supersede older attempts without retaining any request payload."""

        return self._put_outcome(
            key,
            snapshot=None,
            retry_count=max(0, int(retry_count or 0)),
        )

    def _put_outcome(
        self,
        key: tuple[str, str, str, str],
        *,
        snapshot: ReviewContextSnapshot | None,
        retry_count: int,
    ) -> bool:
        now = self._clock()
        with self._lock:
            self._prune(now)
            existing = self._items.get(key)
            if existing is not None and existing.retry_count > retry_count:
                return False
            if existing is not None:
                self._remove(key)
            stored = _StoredSnapshot(
                snapshot=snapshot,
                retry_count=retry_count,
                captured_at=now,
            )
            self._items[key] = stored
            self._size_bytes += stored.size_bytes
            while len(self._items) > self._max_entries or self._size_bytes > self._max_bytes:
                oldest_key = next(iter(self._items))
                self._remove(oldest_key)
            return key in self._items

    def get(self, key: tuple[str, str, str, str]) -> ReviewContextSnapshot | None:
        """Return an exact live snapshot, or ``None`` for absence or a tombstone."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            stored = self._items.get(key)
            if stored is None:
                return None
            self._items.move_to_end(key)
            return stored.snapshot

    def delete_session(self, session_id: str) -> None:
        """Delete all ephemeral review context associated with one session."""
        with self._lock:
            for key in [key for key in self._items if key[0] == session_id]:
                self._remove(key)
