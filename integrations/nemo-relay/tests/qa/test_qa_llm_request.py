# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `llm_request`: intent schema injection, intent checks, transcript ids, the Nomos message."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from nemo_relay_nomos import llm_request as lr

TURN = "nt_" + "b" * 32
INTENT = {
    "turn": TURN,
    "description": "List files",
    "justification": "User asked",
    "target_resource": "/workspace",
    "blast_radius": "read-only",
}


def _tool(name: str, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"tool {name}",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"] if required is None else required,
            },
        },
    }


def _ev(n: int) -> str:
    return "ev_" + f"{n:032x}"


def _guard_run_shell(name: str) -> bool:
    return name == "run_shell"


# ---- constants ------------------------------------------------------------------------------------


def test_intent_fields_are_the_four_named_fields_in_order():
    assert lr.INTENT_FIELDS == ("description", "justification", "target_resource", "blast_radius"), (
        f"INTENT_FIELDS is {lr.INTENT_FIELDS!r}"
    )


def test_nomos_field_requires_the_turn_and_exactly_the_four_intent_fields():
    required = lr.NOMOS_FIELD.get("required")
    assert sorted(required or []) == sorted(["turn", *lr.INTENT_FIELDS]), (
        f"NOMOS_FIELD.required is {required!r}"
    )


@pytest.mark.parametrize("turn", ["nt_" + "B" * 32, "nt_" + "a" * 31, "ev_" + "a" * 32, "", 5])
def test_nomos_field_rejects_a_malformed_turn(turn):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**INTENT, "turn": turn}, lr.NOMOS_FIELD)


def test_nomos_field_disallows_additional_properties():
    assert lr.NOMOS_FIELD.get("additionalProperties") is False, (
        f"NOMOS_FIELD.additionalProperties is {lr.NOMOS_FIELD.get('additionalProperties')!r}"
    )


def test_nomos_field_accepts_a_complete_intent_without_evidence():
    jsonschema.validate(INTENT, lr.NOMOS_FIELD)


def test_nomos_field_accepts_exactly_20_unique_evidence_ids():
    intent = {**INTENT, "evidence": [_ev(i) for i in range(20)]}
    jsonschema.validate(intent, lr.NOMOS_FIELD)


@pytest.mark.parametrize(
    "evidence,why",
    [
        ([_ev(i) for i in range(21)], "21 ids"),
        ([_ev(1), _ev(1)], "duplicate ids"),
        (["ev_" + "A" * 32], "uppercase hex"),
        (["ev_" + "0" * 31], "31 hex chars"),
        (["ev_" + "0" * 33], "33 hex chars"),
        (["xx_" + "0" * 32], "wrong prefix"),
        ([1], "non-string"),
        ("ev_" + "0" * 32, "string not array"),
    ],
)
def test_nomos_field_rejects_bad_evidence(evidence, why):
    intent = {**INTENT, "evidence": evidence}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(intent, lr.NOMOS_FIELD)
    # why: carried by the parametrize id


@pytest.mark.parametrize(
    "missing", ["turn", "description", "justification", "target_resource", "blast_radius"]
)
def test_nomos_field_rejects_a_missing_intent_field(missing):
    intent = {k: v for k, v in INTENT.items() if k != missing}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(intent, lr.NOMOS_FIELD)


def test_nomos_field_rejects_an_extra_property():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**INTENT, "sneaky": "x"}, lr.NOMOS_FIELD)


# ---- inject_intent --------------------------------------------------------------------------------


def test_inject_intent_on_empty_list_returns_nothing_changed():
    tools, changed = lr.inject_intent([], lambda n: True)
    assert tools == [] and changed == [], f"inject_intent([]) -> {(tools, changed)!r}"


def test_inject_intent_adds_nomos_to_properties_and_end_of_required():
    tools = [_tool("run_shell", required=["command", "timeout"])]
    new, changed = lr.inject_intent(tools, _guard_run_shell)
    params = new[0]["function"]["parameters"]
    assert changed == ["run_shell"], f"changed names {changed!r} for guarded run_shell"
    assert params["properties"]["nomos"] == lr.NOMOS_FIELD, (
        f"nomos property is {params['properties'].get('nomos')!r}"
    )
    assert params["required"] == ["command", "timeout", "nomos"], f"required is {params['required']!r}"
    assert params["properties"]["command"] == {"type": "string"}, "existing properties must be kept"


def test_inject_intent_leaves_unguarded_tools_alone():
    tools = [_tool("run_shell"), _tool("read_file")]
    new, changed = lr.inject_intent(tools, _guard_run_shell)
    assert changed == ["run_shell"], f"changed {changed!r}; only run_shell is guarded"
    assert new[1] == tools[1], f"unguarded read_file was edited: {new[1]!r}"


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search"},
        {"name": "run_shell", "input_schema": {"type": "object", "properties": {}}},
        {"type": "custom", "function": {"name": "run_shell", "parameters": {"type": "object"}}},
        {"type": "function"},
    ],
    ids=["builtin", "anthropic-shape", "custom-type", "function-without-body"],
)
def test_inject_intent_edits_only_type_function_tools(tool):
    new, changed = lr.inject_intent([copy.deepcopy(tool)], lambda n: True)
    assert changed == [], f"non-function tool {tool!r} reported changed: {changed!r}"
    assert new == [tool], f"non-function tool {tool!r} was edited into {new!r}"


def test_inject_intent_never_mutates_its_input():
    tools = [_tool("run_shell"), _tool("other")]
    before = copy.deepcopy(tools)
    lr.inject_intent(tools, lambda n: True)
    assert tools == before, f"inject_intent mutated input: {tools!r}"


def test_inject_intent_is_idempotent():
    tools = [_tool("run_shell"), _tool("other")]
    once, changed_once = lr.inject_intent(tools, lambda n: True)
    twice, changed_twice = lr.inject_intent(once, lambda n: True)
    assert twice == once, f"second inject_intent changed tools: {twice!r} vs {once!r}"
    assert changed_twice == [], f"second inject_intent reported changes {changed_twice!r}"
    assert twice[0]["function"]["parameters"]["required"].count("nomos") == 1, (
        "nomos listed twice in required"
    )
    assert sorted(changed_once) == ["other", "run_shell"], f"first call changed {changed_once!r}"


def test_injected_nomos_schema_has_no_per_call_data():
    a, _ = lr.inject_intent([_tool("run_shell")], lambda n: True)
    b, _ = lr.inject_intent([_tool("deploy_service")], lambda n: True)
    sa = a[0]["function"]["parameters"]["properties"]["nomos"]
    sb = b[0]["function"]["parameters"]["properties"]["nomos"]
    assert sa == sb == lr.NOMOS_FIELD, (
        "the injected nomos schema must be the fixed NOMOS_FIELD for every tool"
    )


# ---- intent_problems ------------------------------------------------------------------------------


def test_intent_problems_empty_for_a_complete_intent():
    assert lr.intent_problems(INTENT) == [], f"intent_problems({INTENT!r}) must be []"


def test_intent_problems_empty_for_complete_intent_with_evidence():
    intent = {**INTENT, "evidence": [_ev(1)]}
    assert lr.intent_problems(intent) == [], f"intent_problems({intent!r}) must be []"


@pytest.mark.parametrize("turn", [None, "MISSING", "", "nt_123", "nt_" + "A" * 32, "nt_" + "a" * 33])
def test_intent_problems_reports_a_missing_or_malformed_turn(turn):
    intent = (
        {k: v for k, v in INTENT.items() if k != "turn"} if turn == "MISSING" else {**INTENT, "turn": turn}
    )
    got = lr.intent_problems(intent)
    assert any("turn" in p for p in got), f"intent_problems({intent!r}) -> {got!r}; must name turn"


@pytest.mark.parametrize("intent", [None, "text", 5, [], {}], ids=["none", "str", "int", "list", "empty"])
def test_intent_problems_reports_a_non_intent(intent):
    got = lr.intent_problems(intent)
    assert isinstance(got, list) and got, f"intent_problems({intent!r}) -> {got!r}; must be non-empty"


@pytest.mark.parametrize("field", ["description", "justification", "target_resource", "blast_radius"])
@pytest.mark.parametrize("bad", [None, "", "   ", "\n\t", 5, ["x"], "MISSING"])
def test_intent_problems_reports_each_missing_blank_or_non_string_field(field, bad):
    intent = dict(INTENT)
    if bad == "MISSING":
        del intent[field]
    else:
        intent[field] = bad
    got = lr.intent_problems(intent)
    assert got, f"intent_problems with {field}={bad!r} -> {got!r}; must report a problem"


@pytest.mark.parametrize("field", ["description", "justification", "target_resource", "blast_radius"])
def test_intent_problems_accepts_4096_characters(field):
    intent = {**INTENT, field: "x" * 4096}
    assert lr.intent_problems(intent) == [], f"{field} of 4096 chars must be accepted"


@pytest.mark.parametrize("field", ["description", "justification", "target_resource", "blast_radius"])
def test_intent_problems_rejects_4097_characters(field):
    intent = {**INTENT, field: "x" * 4097}
    assert lr.intent_problems(intent), f"{field} of 4097 chars must be reported"


# ---- transcript_ids -------------------------------------------------------------------------------


def test_transcript_ids_finds_tokens_in_tool_calls_and_ids_in_tool_results_in_order():
    t1, t2 = "nt_" + "1" * 32, "nt_" + "2" * 32
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"arguments": json.dumps({"nomos": {"turn": t1}})}}],
        },
        {"role": "tool", "content": json.dumps({"evidence_id": _ev(5)})},
        {"role": "assistant", "content": [{"type": "tool_use", "input": {"nomos": {"turn": t2}}}]},
        {"role": "tool", "content": json.dumps({"evidence_id": _ev(6), "again": _ev(5)})},
        {"role": "assistant", "content": f"repeating {t1}"},
    ]
    assert lr.transcript_ids(messages) == ([t1, t2], [_ev(5), _ev(6)])


def test_transcript_ids_of_no_messages_is_empty():
    assert lr.transcript_ids([]) == ([], [])


# ---- render_message -------------------------------------------------------------------------------


def _entry(n: int, *, certified: bool, outcome: str = "executed", args: dict | None = None) -> dict:
    return {
        "evidence_id": _ev(n),
        "tool_name": f"tool_{n}",
        "arguments": args if args is not None else {"command": f"echo {n}"},
        "outcome": outcome,
        "certified": certified,
        "created_at": 1_700_000_000.0 + n,
    }


def test_render_message_of_no_entries_is_the_turn_line_alone():
    text = lr.render_message(TURN, [])
    assert text == lr.TURN_LINE.format(turn=TURN), f"render_message(turn, []) is {text!r}"
    assert TURN in text and "nomos.turn" in text and lr.CATALOG_HEADER not in text


def test_render_message_is_the_turn_line_then_the_header_then_one_line_per_entry():
    entries = [_entry(1, certified=True), _entry(2, certified=False, outcome="denied")]
    text = lr.render_message(TURN, entries)
    lines = text.splitlines()
    assert lines[0] == lr.TURN_LINE.format(turn=TURN), f"first line {lines[0]!r} must be the turn line"
    assert text.split("\n", 1)[1].startswith(lr.CATALOG_HEADER), f"the catalog header must follow: {text!r}"
    body = [ln for ln in text.split(lr.CATALOG_HEADER, 1)[1].splitlines() if ln.strip()]
    assert len(body) == len(entries), f"catalog body {body!r} must have one line per entry ({len(entries)})"
    for e in entries:
        found = [ln for ln in body if e["evidence_id"] in ln]
        assert len(found) == 1, f"exactly one line must hold {e['evidence_id']}: {body!r}"


@pytest.mark.parametrize("certified,label", [(True, "certified"), (False, "plugin-signed")])
def test_render_message_line_holds_id_tool_outcome_trust_and_compact_arguments(certified, label):
    entry = _entry(7, certified=certified, outcome="failed", args={"command": "ls -la /tmp"})
    text = lr.render_message(TURN, [entry])
    line = [ln for ln in text.splitlines() if entry["evidence_id"] in ln][0]
    for part in (entry["evidence_id"], "tool_7", "failed", label, '{"command":"ls -la /tmp"}'):
        assert part in line, f"catalog line {line!r} for {entry!r} must contain {part!r}"
    if not certified:
        assert "certified" not in line.replace("plugin-signed", ""), (
            f"plugin-signed line claims certified: {line!r}"
        )
    else:
        assert "plugin-signed" not in line, f"certified line says plugin-signed: {line!r}"


@pytest.mark.parametrize("outcome", ["executed", "failed", "denied", "error", "not_executed"])
def test_render_message_shows_every_outcome(outcome):
    text = lr.render_message(TURN, [_entry(3, certified=False, outcome=outcome)])
    assert outcome in text.split(lr.CATALOG_HEADER, 1)[1], f"catalog for outcome={outcome!r} does not show it"


# ---- append_message -------------------------------------------------------------------------------

TEXT = "QA CATALOG TEXT"


def _raw() -> dict:
    return {
        "headers": {"x": "y"},
        "content": {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": []},
    }


def test_append_message_raw_path_appends_a_system_message():
    req = _raw()
    new_req, new_ann = lr.append_message(req, None, TEXT)
    assert new_ann is None, f"raw path must return annotated None, got {new_ann!r}"
    msgs = new_req["content"]["messages"]
    assert msgs[:-1] == req["content"]["messages"], f"existing messages changed: {msgs!r}"
    assert msgs[-1] == {"role": "system", "content": TEXT}, f"last raw message is {msgs[-1]!r}"


def test_append_message_raw_path_never_mutates_input():
    req = _raw()
    before = copy.deepcopy(req)
    lr.append_message(req, None, TEXT)
    assert req == before, f"append_message mutated the raw request: {req!r}"


def test_append_message_anthropic_appends_to_existing_instructions_with_blank_line():
    req = _raw()
    ann = {"api_specific": {"api": "anthropic_messages"}, "instructions": "Be careful.", "messages": []}
    new_req, new_ann = lr.append_message(req, ann, TEXT)
    assert new_req == req, f"annotated path changed raw request: {new_req!r}"
    assert new_ann["instructions"] == "Be careful.\n\n" + TEXT, f"instructions {new_ann['instructions']!r}"
    assert new_ann["messages"] == [], f"anthropic path must not add a message: {new_ann['messages']!r}"


def test_append_message_anthropic_creates_instructions_when_absent():
    ann = {"api_specific": {"api": "anthropic_messages"}, "messages": []}
    _, new_ann = lr.append_message(_raw(), ann, TEXT)
    assert new_ann["instructions"] == TEXT, f"instructions {new_ann.get('instructions')!r} for absent input"


def test_append_message_openai_responses_appends_a_developer_message():
    ann = {"api_specific": {"api": "openai_responses"}, "messages": [{"role": "user", "content": "hi"}]}
    new_req, new_ann = lr.append_message(_raw(), ann, TEXT)
    assert new_req == _raw(), "annotated path must leave the raw request unchanged"
    assert new_ann["messages"][-1] == {"role": "developer", "content": TEXT}, (
        f"messages {new_ann['messages']!r}"
    )
    assert len(new_ann["messages"]) == 2, f"exactly one message must be appended: {new_ann['messages']!r}"


@pytest.mark.parametrize("api_specific", [{"api": "openai_chat"}, {"api": "custom"}, None], ids=str)
def test_append_message_other_apis_append_a_system_message(api_specific):
    ann = {"messages": [{"role": "user", "content": "hi"}]}
    if api_specific is not None:
        ann["api_specific"] = api_specific
    new_req, new_ann = lr.append_message(_raw(), ann, TEXT)
    assert new_req == _raw(), f"annotated path (api_specific={api_specific!r}) changed raw request"
    assert new_ann["messages"][-1] == {"role": "system", "content": TEXT}, (
        f"api_specific={api_specific!r}: messages {new_ann['messages']!r}"
    )


@pytest.mark.parametrize("api", ["anthropic_messages", "openai_responses", "openai_chat"])
def test_append_message_annotated_never_mutates_inputs(api):
    req = _raw()
    ann = {"api_specific": {"api": api}, "instructions": "x", "messages": [{"role": "user", "content": "hi"}]}
    before = (copy.deepcopy(req), copy.deepcopy(ann))
    lr.append_message(req, ann, TEXT)
    assert (req, ann) == before, f"append_message mutated inputs for api={api!r}"


# ---- without evidence (no signing key) ------------------------------------------------------------


def test_nomos_field_without_evidence_has_no_evidence_property_and_is_otherwise_the_same():
    assert lr.nomos_field(True) is lr.NOMOS_FIELD
    without = lr.nomos_field(False)
    assert "evidence" not in without["properties"], f"{sorted(without['properties'])!r}"
    assert {k: v for k, v in lr.NOMOS_FIELD["properties"].items() if k != "evidence"} == without["properties"]
    assert without["required"] == lr.NOMOS_FIELD["required"] and without["additionalProperties"] is False


def test_inject_intent_uses_the_given_field():
    tools, _ = lr.inject_intent([_tool("run_shell")], _guard_run_shell, lr.nomos_field(False))
    assert tools[0]["function"]["parameters"]["properties"]["nomos"] == lr.NOMOS_FIELD_WITHOUT_EVIDENCE


def test_intent_problems_without_evidence_reports_an_evidence_field():
    assert lr.intent_problems(INTENT, evidence=False) == []
    got = lr.intent_problems({**INTENT, "evidence": [_ev(1)]}, evidence=False)
    assert any("evidence" in p for p in got), f"intent_problems -> {got!r}"


def test_teach_nomos_mentions_evidence_only_when_it_is_offered():
    assert "evidence" in lr.teach_nomos("run_shell") and "run_shell" in lr.teach_nomos("run_shell")
    assert "evidence" not in lr.teach_nomos("run_shell", evidence=False)
