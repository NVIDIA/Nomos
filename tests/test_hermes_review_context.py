# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for Hermes-specific, privacy-bounded live review context."""

from __future__ import annotations

import json

from nomos_hermes.review_context import (
    MAX_CONTENT_BLOCKS,
    MAX_CONTEXT_BYTES,
    MAX_MESSAGE_CHARS,
    MAX_SOURCE_ENVELOPES,
    MAX_SOURCE_MESSAGE_CHARS,
    ReviewContextStore,
    build_review_context,
)


def _request(messages):
    return {"method": "POST", "body": {"model": "test", "messages": messages}}


def _build(request, *, retry_count=0, current_user_message=None):
    body = request.get("body", {})
    messages = body.get("messages", body.get("input", []))
    if current_user_message is None:
        current_user_message = next(
            (
                item.get("content")
                for item in reversed(messages)
                if isinstance(item, dict)
                and item.get("role") == "user"
                and isinstance(item.get("content"), str)
            ),
            "current user request",
        )
    return build_review_context(
        messages,
        current_user_message=current_user_message,
        retry_count=retry_count,
    )


def test_build_review_context_keeps_only_allowlisted_text_shapes():
    request = _request(
        [
            {"role": "system", "content": "SYSTEM-SENTINEL"},
            {"role": "developer", "content": "DEVELOPER-SENTINEL"},
            {"role": "user", "content": "ordinary user request"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "visible assistant text"}],
                "tool_calls": [{"function": {"arguments": "TOOL-ARGUMENT-SENTINEL"}}],
            },
            {"role": "tool", "content": "TOOL-RESULT-SENTINEL"},
            {
                # Bedrock can represent a tool result using role=user. Reject
                # the unknown block rather than trusting the role alone.
                "role": "user",
                "content": [{"toolResult": {"content": [{"text": "BEDROCK-SENTINEL"}]}}],
            },
            {
                "type": "function_call_output",
                "role": "user",
                "content": "RESPONSES-SENTINEL",
            },
            {
                "role": "assistant",
                "content": [{"type": "reasoning", "text": "REASONING-SENTINEL"}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "current user request"}],
            },
        ]
    )

    snapshot = _build(request, current_user_message="current user request")

    assert snapshot is not None
    payload = json.loads(snapshot.serialized)
    assert [message["text"] for message in payload["messages"]] == [
        "ordinary user request",
        "visible assistant text",
        "current user request",
    ]
    assert payload["messages"][-1]["current_user"] is True
    for sentinel in (
        "SYSTEM-SENTINEL",
        "DEVELOPER-SENTINEL",
        "TOOL-ARGUMENT-SENTINEL",
        "TOOL-RESULT-SENTINEL",
        "BEDROCK-SENTINEL",
        "RESPONSES-SENTINEL",
        "REASONING-SENTINEL",
    ):
        assert sentinel not in snapshot.serialized


def test_build_review_context_supports_responses_input_messages():
    snapshot = _build(
        {
            "method": "POST",
            "body": {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "PRIVATE-OUTPUT",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "visible"}],
                    },
                ]
            },
        },
        current_user_message="hello",
    )

    assert snapshot is not None
    assert [item["text"] for item in json.loads(snapshot.serialized)["messages"]] == [
        "hello",
        "visible",
    ]
    assert "PRIVATE-OUTPUT" not in snapshot.serialized


def test_unknown_envelopes_crossed_blocks_and_mixed_media_fail_by_omission():
    messages = [
        {"role": "user", "content": "previous benign request"},
        {
            "type": "computer_call_output",
            "role": "user",
            "content": "PRIVATE-COMPUTER-OUTPUT",
        },
        {
            "type": "future_tool_output",
            "role": "user",
            "content": "PRIVATE-FUTURE-OUTPUT",
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "output_text", "text": "CROSSED-USER-OUTPUT"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "input_text", "text": "CROSSED-ASSISTANT-INPUT"}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "CURRENT-MIXED-TEXT"},
                {"type": "image_url", "image_url": {"url": "PRIVATE-IMAGE"}},
            ],
        },
        {"role": "assistant", "content": [{"text": "ordinary Bedrock text"}]},
    ]

    snapshot = build_review_context(
        messages,
        current_user_message="actual current request",
    )

    assert snapshot is not None
    payload = json.loads(snapshot.serialized)
    assert [message["text"] for message in payload["messages"]] == [
        "previous benign request",
        "ordinary Bedrock text",
        "actual current request",
    ]
    assert payload["messages"][-1]["current_user"] is True
    assert "PRIVATE" not in snapshot.serialized
    assert "CROSSED" not in snapshot.serialized
    assert "CURRENT-MIXED-TEXT" not in snapshot.serialized


def test_six_most_recent_messages_force_current_user_and_preserve_order():
    messages = [{"role": "user", "content": "current-user-anchor"}]
    messages.extend(
        {"role": "assistant", "content": f"assistant-step-{index}"} for index in range(7)
    )

    snapshot = _build(_request(messages), current_user_message="current-user-anchor")

    assert snapshot is not None
    payload = json.loads(snapshot.serialized)
    assert len(payload["messages"]) == 6
    assert payload["messages"][0]["text"] == "current-user-anchor"
    assert payload["messages"][0]["current_user"] is True
    assert [item["text"] for item in payload["messages"][1:]] == [
        "assistant-step-2",
        "assistant-step-3",
        "assistant-step-4",
        "assistant-step-5",
        "assistant-step-6",
    ]
    assert payload["truncation"]["messages_omitted"] == 2


def test_redacts_before_exact_head_tail_truncation():
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "s" * 3500
        + "\n-----END PRIVATE KEY-----"
    )
    text = "BEGINNING " + private_key + " password=visible-secret " + "z" * 3500 + " ENDING"

    snapshot = _build(_request([{"role": "user", "content": text}]))

    assert snapshot is not None
    bounded = json.loads(snapshot.serialized)["messages"][0]["text"]
    assert len(bounded) == MAX_MESSAGE_CHARS
    assert bounded.startswith("BEGINNING [REDACTED PRIVATE KEY]")
    assert bounded.endswith(" ENDING")
    assert "visible-secret" not in bounded
    assert "characters omitted" in bounded
    assert snapshot.redaction_count == 2
    assert snapshot.truncated is True


def test_utf8_byte_limit_drops_oldest_optional_messages_and_json_is_deterministic():
    messages = [
        {"role": "user", "content": f"message-{index}-" + "😀" * 5000}
        for index in range(6)
    ]

    first = _build(_request(messages))
    second = _build(_request(messages))

    assert first is not None and second is not None
    assert first.serialized == second.serialized
    assert first.size_bytes <= MAX_CONTEXT_BYTES
    assert json.loads(first.serialized)["schema"] == "nomos.hermes.review-context.v1"
    assert first.message_count < 6


def test_malformed_or_globally_truncated_hermes_request_yields_no_context():
    assert _build({"body": {"messages": "not-a-list"}}, current_user_message="") is None
    assert _build(
        {"body": {"messages": [{"role": "user", "content": []}]}},
        current_user_message="",
    ) is None


def test_raw_source_structural_limits_fail_by_omission():
    oversized_pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "KEY-MATERIAL" * 2000
        + "\n-----END PRIVATE KEY-----"
        + " trailing text" * 500
    )
    assert len(oversized_pem) > MAX_SOURCE_MESSAGE_CHARS
    assert build_review_context(
        [{"role": "user", "content": oversized_pem}],
        current_user_message=oversized_pem,
    ) is None
    assert build_review_context(
        [],
        current_user_message=" " * (MAX_SOURCE_MESSAGE_CHARS + 1),
    ) is None

    too_many_blocks = [
        {"type": "text", "text": f"block-{index}"}
        for index in range(MAX_CONTENT_BLOCKS + 1)
    ]
    snapshot = build_review_context(
        [{"role": "user", "content": too_many_blocks}],
        current_user_message="bounded current user",
    )
    assert snapshot is not None
    assert "block-" not in snapshot.serialized

    excessive_keys = {"type": "text", "text": "safe"}
    excessive_keys.update({f"extra-{index}": "PRIVATE" for index in range(1000)})
    snapshot = build_review_context(
        [{"role": "user", "content": [excessive_keys]}],
        current_user_message="bounded current user",
    )
    assert snapshot is not None
    assert "PRIVATE" not in snapshot.serialized

    many_messages = [
        {"role": "assistant", "content": f"old-{index}"}
        for index in range(MAX_SOURCE_ENVELOPES + 50)
    ]
    many_messages.append({"role": "user", "content": "bounded current user"})
    snapshot = build_review_context(
        many_messages,
        current_user_message="bounded current user",
    )
    assert snapshot is not None
    payload = json.loads(snapshot.serialized)
    assert payload["truncation"]["source_window_truncated"] is True
    assert len(payload["messages"]) <= 6


def test_context_store_exact_key_retry_ttl_lru_and_repeated_reads():
    now = [10.0]
    store = ReviewContextStore(
        max_entries=2,
        max_bytes=MAX_CONTEXT_BYTES * 2,
        ttl_seconds=5,
        clock=lambda: now[0],
    )
    key = ("session", "task", "turn", "request")
    retry_one = _build(
        _request([{"role": "user", "content": "retry-one"}]), retry_count=1
    )
    retry_zero = _build(
        _request([{"role": "user", "content": "retry-zero"}]), retry_count=0
    )
    assert retry_one is not None and retry_zero is not None

    assert store.put(key, retry_one) is True
    assert store.put(key, retry_zero) is False
    assert store.get(key) is retry_one
    assert store.get(key) is retry_one
    assert store.get(("session", "task", "turn", "different")) is None

    other = _build(_request([{"role": "user", "content": "other"}]))
    third = _build(_request([{"role": "user", "content": "third"}]))
    assert other is not None and third is not None
    store.put(("session", "task", "turn", "other"), other)
    store.put(("session", "task", "turn", "third"), third)
    assert store.get(key) is None

    now[0] = 16.0
    assert store.get(("session", "task", "turn", "third")) is None


def test_newer_unavailable_retry_tombstone_prevents_stale_or_delayed_context():
    store = ReviewContextStore()
    key = ("session", "task", "turn", "request")
    retry_zero = _build(
        _request([{"role": "user", "content": "retry-zero"}]), retry_count=0
    )
    retry_one = _build(
        _request([{"role": "user", "content": "retry-one"}]), retry_count=1
    )
    assert retry_zero is not None and retry_one is not None

    assert store.put(key, retry_zero) is True
    assert store.mark_unavailable(key, retry_count=1) is True
    assert store.get(key) is None
    assert store.put(key, retry_zero) is False
    assert store.get(key) is None

    assert store.put(key, retry_one) is True
    assert store.mark_unavailable(key, retry_count=0) is False
    assert store.get(key) is retry_one


def test_context_store_delete_session_does_not_touch_other_sessions():
    store = ReviewContextStore()
    snapshot = _build(_request([{"role": "user", "content": "hello"}]))
    assert snapshot is not None
    first = ("session-1", "task", "turn", "request")
    second = ("session-2", "task", "turn", "request")
    store.put(first, snapshot)
    store.put(second, snapshot)

    store.delete_session("session-1")

    assert store.get(first) is None
    assert store.get(second) is snapshot


def test_context_store_enforces_total_payload_byte_budget():
    first_snapshot = _build(
        _request([{"role": "user", "content": "a" * 1000}])
    )
    second_snapshot = _build(
        _request([{"role": "user", "content": "b" * 1000}])
    )
    assert first_snapshot is not None and second_snapshot is not None
    store = ReviewContextStore(
        max_entries=10,
        max_bytes=max(first_snapshot.size_bytes, second_snapshot.size_bytes) + 10,
    )
    first_key = ("session", "task", "turn", "first")
    second_key = ("session", "task", "turn", "second")

    store.put(first_key, first_snapshot)
    store.put(second_key, second_snapshot)

    assert store.get(first_key) is None
    assert store.get(second_key) is second_snapshot
