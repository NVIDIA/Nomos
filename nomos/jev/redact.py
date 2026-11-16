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

_PLACEHOLDER = "[REDACTED]"

# (name, compiled pattern). Order matters: multi-line blocks first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}")),
    (
        "authorization_header",
        re.compile(r"(?i)\bAuthorization\s*[:=]\s*\S+"),
    ),
    # key=value / key: value assignments for sensitive-looking keys.
    (
        "sensitive_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|client[_-]?secret)\b\s*[:=]\s*('[^']*'|\"[^\"]*\"|\S+)"
        ),
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
