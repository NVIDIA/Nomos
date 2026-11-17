# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Best-effort secret redaction for the Jev pre-council filter.

The intent state is sent to a third-party API, so obvious credentials are
scrubbed before egress. This is defense-in-depth, not a guarantee: it targets
common secret shapes and is not a substitute for keeping secrets out of intent
text in the first place. Patterns are conservative to avoid mangling legitimate
command text.
"""

import re
from typing import Any

_PLACEHOLDER = "[REDACTED]"

_SENSITIVE_NAMES = (
    r"password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret"
)
# A mapping key that names a credential: one of the names above, or the header.
_SENSITIVE_KEY = re.compile(rf"(?i)\b(?:{_SENSITIVE_NAMES}|authorization)$")

# (name, compiled pattern). Order matters: multi-line blocks first.
# This runs on the event loop over text the requester wrote, so no pattern may
# start a match at many places that each scan far ahead before failing.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # A header with no footer masks to the end of its text: the key runs that
    # far, and one scan settles it for every header after the first.
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?"
            r"(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Starts only where a run of token characters starts ("-" is one of them).
    ("jwt", re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}")),
    (
        "authorization_header",
        re.compile(r"(?i)\bAuthorization\s*[:=]\s*\S+"),
    ),
    # key=value / key: value assignments for sensitive-looking keys. The key may
    # be quoted, as in JSON text ("password": "x"), with the quote escaped or not.
    (
        "sensitive_assignment",
        re.compile(rf"(?i)\b({_SENSITIVE_NAMES})\b\\?[\"']?\s*[:=]\s*('[^']*'|\"[^\"]*\"|\S+)"),
    ),
]


def redact(text: str) -> str:
    """Return ``text`` with common secret shapes replaced by a placeholder."""
    if not text:
        return text
    scrubbed = text
    for _name, pattern in _PATTERNS:
        if _name == "sensitive_assignment":
            scrubbed = pattern.sub(lambda m: f"{m.group(1)}={_PLACEHOLDER}", scrubbed)
        else:
            scrubbed = pattern.sub(_PLACEHOLDER, scrubbed)
    return scrubbed


def redact_value(value: Any) -> Any:
    """Return a JSON-like value with every string in it redacted.

    Each string is scrubbed on its own, so a match cannot run past the field it
    started in, into another field or into the syntax of a serialized form. A
    mapping entry whose key names a credential is masked whole: nothing in the
    value of ``{"password": "x"}`` marks it as a secret.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            redact_value(key): _PLACEHOLDER
            if item not in (None, "") and _SENSITIVE_KEY.search(str(key))
            else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
