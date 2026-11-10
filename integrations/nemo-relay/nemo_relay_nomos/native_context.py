# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native Agent identity and bounded extraction of provider-issued tool-call IDs.

Execution intercepts retain their own request snapshot until its response is
complete. The resulting (Agent, tool-call ID) binding is independent of response
completion order and contains no model-authored tracking field.
"""

from typing import Any
from uuid import UUID


class NativeContextError(RuntimeError):
    """The host did not provide usable native request ancestry."""


def scope_entries(snapshot: Any) -> list[Any]:
    """Validate a bounded native snapshot, returning its ordered entries."""
    entries = getattr(snapshot, "scopes", None)
    if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
        raise NativeContextError("Native request ancestry is unavailable; nothing ran.")
    try:
        ids = [entry.scope_id for entry in entries]
        if any(not isinstance(value, str) or str(UUID(value)) != value for value in ids):
            raise ValueError
        if len(set(ids)) != len(ids):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise NativeContextError("Native request ancestry is invalid; nothing ran.") from exc
    return entries


def agent_identity(snapshot: Any, *, allow_single_agent_root: bool = False) -> str:
    """Select an application Agent, with an explicit singleton-root host assertion.

    A reconstructed singleton may preserve an application Agent UUID, but Relay
    can also label a non-Agent parent as Agent. The opt-in trusts the operator's
    host contract; the snapshot alone cannot establish its original provenance.
    """
    entries = scope_entries(snapshot)
    agents = [
        i
        for i, entry in enumerate(entries)
        if getattr(entry.scope_type, "value", entry.scope_type) == "agent"
    ]
    # Never infer ownership from a root with descendants. The compatibility
    # exception is limited to the singleton shape verified by the host pilot.
    if allow_single_agent_root and len(entries) == 1 and agents == [0]:
        return entries[0].scope_id
    if not agents or agents[-1] == 0:
        raise NativeContextError("The host must provide an application Agent scope.")
    return entries[agents[-1]].scope_id


def tool_calls(response: Any, limit: int) -> list[tuple[str, str]]:
    """Read bounded call identities from a host-codec-normalized unary response."""
    if not isinstance(response, dict):
        raise NativeContextError("The host returned an unsupported model response.")
    calls = response.get("tool_calls", [])
    if calls is None:
        calls = []
    if not isinstance(calls, list) or len(calls) > limit:
        raise NativeContextError("The model response exceeds the tool-call identity limit.")
    result = []
    for call in calls:
        if not isinstance(call, dict):
            raise NativeContextError("The model returned an invalid tool-call identity.")
        result.append(_identity(call.get("id"), call.get("name")))
    if len({call_id for call_id, _ in result}) != len(result):
        raise NativeContextError("The model returned ambiguous tool-call IDs.")
    return result


def _identity(call_id: Any, name: Any) -> tuple[str, str]:
    """Validate provider identity lengths before retaining a response binding."""
    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
        raise NativeContextError("The model must supply a bounded tool-call ID.")
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise NativeContextError("The model must supply a bounded tool name.")
    return call_id, name


class ChatStreamCalls:
    """Collect only call IDs and names from the supported OpenAI-chat stream shape.

    No arguments, prose or whole chunks are retained. Bindings are published by
    the execution interceptor only after normal stream completion and cleanup.
    """

    def __init__(self, limit: int) -> None:
        """Bound the number of identities retained across a single response."""
        self.limit = limit
        self.calls: dict[tuple[int, int], dict[str, Any]] = {}
        self.finished: set[int] = set()

    def observe(self, chunk: Any) -> None:
        """Validate one wire chunk and retain only bounded identity fragments."""
        if not isinstance(chunk, dict) or not isinstance(chunk.get("choices", []), list):
            raise NativeContextError("Unsupported chat stream chunk.")
        for choice in chunk.get("choices", []):
            if not isinstance(choice, dict):
                raise NativeContextError("Unsupported chat stream choice.")
            choice_id = choice.get("index", 0)
            if type(choice_id) is not int or choice_id < 0:
                raise NativeContextError("Invalid chat stream choice index.")
            delta = choice.get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, dict) or not isinstance(delta.get("tool_calls", []), list):
                raise NativeContextError("Unsupported chat stream tool-call delta.")
            if choice_id in self.finished and delta.get("tool_calls"):
                raise NativeContextError("A stream continued a finished tool-call proposal.")
            for call in delta.get("tool_calls", []):
                if not isinstance(call, dict) or type(call.get("index")) is not int or call["index"] < 0:
                    raise NativeContextError("Invalid chat stream tool-call index.")
                key = choice_id, call["index"]
                if key not in self.calls:
                    if len(self.calls) >= self.limit:
                        raise NativeContextError("The stream exceeds the tool-call identity limit.")
                    self.calls[key] = {"id": None, "name": ""}
                entry = self.calls[key]
                if "id" in call:
                    if entry["id"] is not None and entry["id"] != call["id"]:
                        raise NativeContextError("A stream tool-call ID changed.")
                    if not isinstance(call["id"], str) or not 1 <= len(call["id"]) <= 256:
                        raise NativeContextError("Invalid stream tool-call ID.")
                    entry["id"] = call["id"]
                function = call.get("function")
                if function is None:
                    function = {}
                if not isinstance(function, dict):
                    raise NativeContextError("Invalid stream tool-call function.")
                name = function.get("name", "")
                if not isinstance(name, str) or len(entry["name"]) + len(name) > 128:
                    raise NativeContextError("Invalid stream tool name.")
                entry["name"] += name
            if choice.get("finish_reason") == "tool_calls":
                if choice_id not in self.finished and len(self.finished) >= self.limit:
                    raise NativeContextError("The stream exceeds the completed-choice limit.")
                self.finished.add(choice_id)

    def complete(self) -> list[tuple[str, str]]:
        """Return complete identities, refusing truncated or ambiguous calls."""
        result = []
        for (choice, _), entry in self.calls.items():
            if choice not in self.finished:
                raise NativeContextError("The stream did not finish its tool-call proposal.")
            result.append(_identity(entry["id"], entry["name"]))
        if len({call_id for call_id, _ in result}) != len(result):
            raise NativeContextError("The stream returned ambiguous tool-call IDs.")
        return result
