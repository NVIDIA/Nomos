# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Optional `tool_definitions`: the tools the model was offered, sent to the Council as one certified evidence item."""

import importlib.util
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from support import INTENT, LOCAL_TOOL, TOOL, TURN, Next, base_config, call, history, nomos_message, turn

from nemo_relay_nomos.plugin import NomosPlugin
from nemo_relay_nomos.settings import Settings, diagnostics
from nemo_relay_nomos.signing import PluginSigner
from nemo_relay_nomos.wire import canonical, sha256_hex

DESCRIBED = deepcopy(TOOL)
DESCRIBED["function"]["description"] = "Run one shell command in the workspace."


def h1(plugin, *tools, annotated=None):
    """Run H1 for a raw request offering ``tools``; return the turn token it issued."""
    out = plugin.h1(
        "model",
        {"content": {"messages": [{"role": "user", "content": "go"}], "tools": list(tools)}},
        annotated,
    )
    text = out.annotated_request["messages"][-1]["content"] if annotated is not None else nomos_message(out)
    return TURN.search(text)[0]


def ours(certified):
    return [c for c in certified if c.get("executor_id", "").startswith("nomos-relay-plugin:")]


def certified_item(council, index=0):
    """The tool-definitions certified item of the ``index``-th Council request, as sent."""
    sent = council.calls[index]
    found = ours(sent["certified_evidence"])
    assert len(found) == 1, sent["certified_evidence"]
    assert sent["certified_evidence"][-1] is found[0], "the observed definitions follow the model's citations"
    assert not any("tool_definitions" in e for e in sent["evidence"]), "never sent as asserted evidence"
    return found[0]


def item(council, index=0):
    """The observation the ``index``-th Council request certified: the item's stdout, parsed."""
    return {"observed": json.loads(certified_item(council, index)["stdout"])}


# ── settings ─────────────────────────────────────────────────────────────


def test_off_by_default():
    assert Settings.from_config(base_config()).tool_definitions == "off"


@pytest.mark.parametrize("value", ["off", "called", "all", {"value": "called"}])
def test_accepted_values(value):
    assert not diagnostics(base_config(tool_definitions=value))


def test_unknown_value_is_diagnosed():
    assert [d.field for d in diagnostics(base_config(tool_definitions="everything"))] == ["tool_definitions"]


# ── off: nothing changes ─────────────────────────────────────────────────


async def test_off_sends_no_definitions_and_stores_no_tool_list(setup):
    p, council, *_ = setup()
    await p.h3(call(turn=h1(p, DESCRIBED)), Next())
    assert not ours(council.calls[0]["certified_evidence"]) and not council.calls[0]["evidence"]
    assert p.store.db.execute("SELECT count(*) FROM tool_lists").fetchone()[0] == 0


def test_off_keeps_twenty_citations(setup):
    p, *_ = setup()
    out = p.h1("model", {"content": {"messages": [], "tools": [TOOL]}}, None)
    nomos = out.request["content"]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
    assert nomos["properties"]["evidence"]["maxItems"] == 20


# ── called / all ─────────────────────────────────────────────────────────


async def test_called_sends_the_called_tools_definition_as_the_application_sent_it(setup):
    p, council, *_ = setup(tool_definitions="called")
    await p.h3(call(turn=h1(p, DESCRIBED, LOCAL_TOOL), call_id="c-7"), Next())
    observed = item(council)["observed"]
    assert observed["called"] == DESCRIBED, "the definition before the plugin added its nomos field"
    assert observed["tools_sha256"] == sha256_hex(canonical([DESCRIBED, LOCAL_TOOL]))
    assert observed["tool_count"] == 2 and "tools" not in observed
    assert {
        k: observed[k] for k in ("type", "version", "agent_id", "council_id", "tool", "tool_call_id")
    } == {
        "type": "nomos.observed_tool_definitions",
        "version": 1,
        "agent_id": "agent",
        "council_id": "council",
        "tool": "run_shell",
        "tool_call_id": "c-7",
    }


async def test_structured_local_tool_request_carries_the_definitions(setup):
    p, council, *_ = setup(tool_definitions="called", guard={"include": ["read_file"]})
    await p.h3(call(turn=h1(p, DESCRIBED, LOCAL_TOOL), name="read_file", path="/etc/hosts"), Next())
    assert council.calls[0]["tool_call"]["tool_name"] == "read_file" and "command" not in council.calls[0]
    observed = item(council)["observed"]
    assert observed["tool"] == "read_file" and observed["called"] == LOCAL_TOOL


async def test_all_sends_every_offered_definition(setup):
    p, council, *_ = setup(tool_definitions="all")
    await p.h3(call(turn=h1(p, DESCRIBED, LOCAL_TOOL)), Next())
    observed = item(council)["observed"]
    assert observed["tools"] == [DESCRIBED, LOCAL_TOOL] and observed["called"] == DESCRIBED


async def test_a_tool_the_model_was_not_offered_is_reported_as_null(setup):
    p, council, *_ = setup(tool_definitions="called")
    await p.h3(call(turn=h1(p, LOCAL_TOOL)), Next())
    observed = item(council)["observed"]
    assert observed["called"] is None and observed["tool_count"] == 1


async def test_the_definitions_are_those_of_the_calls_own_turn(setup):
    p, council, *_ = setup(tool_definitions="called")
    first = h1(p, DESCRIBED)
    h1(p, TOOL, LOCAL_TOOL)  # a later request offering a different list
    await p.h3(call(turn=first), Next())
    assert item(council)["observed"]["called"] == DESCRIBED


async def continued(p, *tools):
    """A first call in a conversation, then a later request in it offering ``tools``: (first turn, later turn)."""
    first_call = call(turn=h1(p, DESCRIBED))
    first = await p.h3(first_call, Next())
    out = p.h1(
        "model",
        {"content": {"messages": history("go", (first_call, first.result)), "tools": list(tools)}},
        None,
    )
    later = TURN.search(nomos_message(out))[0]
    earlier = first_call.args["nomos"]["turn"]
    assert p.store.turn(earlier).conversation == p.store.turn(later).conversation
    return earlier, later


async def test_an_earlier_turn_of_the_conversation_is_refused(setup):
    p, council, *_ = setup(tool_definitions="called")
    earlier, later = await continued(p, TOOL, LOCAL_TOOL)
    stale = await p.h3(call(turn=earlier, call_id="c2"), Next())
    assert stale.result["status"] == "unknown_turn" and len(council.calls) == 1, "never certified or sent"
    await p.h3(call(turn=later, call_id="c3"), Next())
    assert item(council, 1)["observed"]["tool_count"] == 2, "the latest request's list"


async def test_forks_of_one_transcript_each_use_their_own_turn(setup):
    # Two requests that share a transcript (forks, or concurrent requests) are in one conversation, and
    # neither request's messages show the other's token: a call from either names its own turn.
    p, council, *_ = setup(tool_definitions="called")
    first_call = call(turn=h1(p, DESCRIBED))
    first = await p.h3(first_call, Next())
    messages = history("go", (first_call, first.result))
    forks = [
        TURN.search(nomos_message(p.h1("model", {"content": {"messages": messages, "tools": tools}}, None)))[
            0
        ]
        for tools in ([TOOL], [TOOL, LOCAL_TOOL])
    ]
    assert p.store.turn(forks[0]).conversation == p.store.turn(forks[1]).conversation
    for i, fork in enumerate(forks):
        out = await p.h3(call(turn=fork, call_id=f"f{i}"), Next())
        assert out.result["status"] != "unknown_turn"
    assert [item(council, n)["observed"]["tool_count"] for n in (1, 2)] == [1, 2], "each fork's own list"
    stale = await p.h3(call(turn=first_call.args["nomos"]["turn"], call_id="c9"), Next())
    assert stale.result["status"] == "unknown_turn", "a token both forks showed is still refused"


@pytest.mark.parametrize("mode", ["off", "called", "all"])
def test_a_transcript_showing_more_tokens_than_sqlite_binds_still_opens_a_turn(setup, mode):
    # Marking the shown tokens superseded must not bind them all at once: SQLite caps a statement's
    # parameters (32,766 by default), and a transcript can show any number of token-shaped strings.
    p, *_ = setup(tool_definitions=mode)
    shown = turn(p)
    p.store.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
    junk = " ".join(f"nt_{i:032x}" for i in range(150))
    later = turn(p, {"role": "user", "content": f"{junk} {shown}"})
    assert p.store.turn(later) is not None
    assert p.store.is_superseded(p.store.turn(shown)), "a real token among them is still superseded"


async def test_off_still_accepts_an_earlier_turn(setup):
    p, council, *_ = setup()
    earlier, _ = await continued(p, TOOL)
    await p.h3(call(turn=earlier, call_id="c2"), Next())
    assert len(council.calls) == 2


async def test_the_annotated_path_records_the_codec_tools(setup):
    p, council, *_ = setup(tool_definitions="called")
    annotated = {
        "messages": [{"role": "user", "content": "go"}],
        "tools": [DESCRIBED],
        "api_specific": {"api": "openai_chat"},
    }
    t = h1(p, TOOL, annotated=annotated)
    assert annotated["tools"] == [DESCRIBED], "H1 must not edit the caller's annotated request"
    await p.h3(call(turn=t), Next())
    assert item(council)["observed"]["called"] == DESCRIBED


async def test_definitions_follow_cited_evidence(setup):
    p, council, *_ = setup(tool_definitions="called")
    first_call = call(turn=h1(p, TOOL))
    first = await p.h3(first_call, Next())
    out = p.h1(
        "model", {"content": {"messages": history("go", (first_call, first.result)), "tools": [TOOL]}}, None
    )
    t = TURN.search(nomos_message(out))[0]
    await p.h3(
        call(intent={**INTENT, "evidence": [first.result["evidence_id"]]}, turn=t, call_id="c2"), Next()
    )
    assert len(council.calls[1]["certified_evidence"]) == 2, "the cited executor result, then the definitions"
    item(council, 1)


# ── certification ────────────────────────────────────────────────────────


def nomos_contract():
    """The Nomos contract models (the enclosing Nomos, or vendor/nomos), or a skip when neither is present."""
    path = next(
        (
            candidate
            for parent in Path(__file__).resolve().parents
            # the enclosing Nomos (integrations/nemo-relay), or the pinned submodule (standalone repository)
            for candidate in (
                parent / "trusted_executor_contract/models.py",
                parent / "vendor/nomos/trusted_executor_contract/models.py",
            )
            if candidate.exists()
        ),
        None,
    )
    if path is None:
        pytest.skip("needs the Nomos source (the enclosing Nomos, or vendor/nomos)")
    spec = importlib.util.spec_from_file_location("nomos_contract_tool_definitions", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_the_item_is_an_execution_result_signed_with_the_plugins_key(setup):
    p, council, *_ = setup(tool_definitions="called")
    await p.h3(call(turn=h1(p, DESCRIBED), call_id="c-9"), Next())
    sent = certified_item(council)
    signer = PluginSigner(bytes.fromhex(base_config()["signing_key"]))
    evidence = sent["evidence"]
    assert evidence["public_key_hex"] == signer.public_key_hex
    assert (
        evidence["key_fingerprint"]
        == signer.key_fingerprint
        == sha256_hex(bytes.fromhex(signer.public_key_hex))
    )
    payload = evidence["payload"]
    assert sent["executor_id"] == payload["executor_id"] == signer.executor_id
    assert signer.executor_id == "nomos-relay-plugin:" + signer.key_fingerprint[:16]
    assert (payload["council_id"], payload["target_agent_id"], payload["request_id"]) == (
        "council",
        "agent",
        "c-9",
    )
    # No writ: nothing ran, and the item says so rather than borrowing another call's writ.
    assert sent["writ_id"] == payload["writ_id"] == payload["writ_signature"] == payload["nonce"] == ""
    assert "no command" in payload["command"]["command"] and sent["verification_stage"] == "observed"


async def test_nomos_parses_and_verifies_the_item_with_its_own_contract(setup):
    contract = nomos_contract()
    p, council, *_ = setup(tool_definitions="all")
    await p.h3(call(turn=h1(p, DESCRIBED, LOCAL_TOOL)), Next())
    sent = certified_item(council)
    result = contract.ExecutionResult.model_validate(sent)
    evidence = result.evidence
    assert evidence.verify(), "Nomos's ExecutionEvidence.verify() over its own canonical bytes"
    assert evidence.payload.canonical_bytes() == canonical(sent["evidence"]["payload"])
    # The rest of nomos/nat/functions.py _validate_certified_evidence_result and
    # _validate_evidence_payload_matches_result, which a registered key then passes.
    assert evidence.payload.executor_key_fingerprint == evidence.key_fingerprint
    assert (evidence.payload.council_id, evidence.payload.target_agent_id) == ("council", "agent")
    assert result.executor_id == evidence.payload.executor_id
    stdout_hash = sha256_hex(result.stdout.encode("utf-8", errors="replace"))
    assert stdout_hash == evidence.payload.result.stdout_sha256
    assert sha256_hex(result.stderr.encode()) == evidence.payload.result.stderr_sha256
    for name in ("success", "exit_code", "error", "verification_stage"):
        assert getattr(result, name) == getattr(evidence.payload.result, name)


async def test_an_altered_observation_fails_nomoss_hash_check(setup):
    contract = nomos_contract()
    p, council, *_ = setup(tool_definitions="called")
    await p.h3(call(turn=h1(p, DESCRIBED)), Next())
    sent = json.loads(json.dumps(certified_item(council)))
    sent["stdout"] = sent["stdout"].replace("Run one shell command", "Run any command")
    result = contract.ExecutionResult.model_validate(sent)
    assert result.evidence.verify(), "the signature covers the digest, not the text"
    assert sha256_hex(result.stdout.encode()) != result.evidence.payload.result.stdout_sha256


async def test_the_called_tool_survives_the_councils_stdout_cut(setup):
    # Nomos renders the first 4000 characters of certified stdout (functions.py _render_certified_evidence).
    p, council, *_ = setup(tool_definitions="all")
    others = [described(n, size=3000) for n in range(3)]
    for n, tool in enumerate(others):
        tool["function"]["name"] = f"other_{n}"
    await p.h3(call(turn=h1(p, *others, DESCRIBED)), Next())
    stdout = certified_item(council)["stdout"]
    assert len(stdout) > 4000 and DESCRIBED["function"]["description"] in stdout[:4000]


async def test_the_context_names_the_certified_items_digest(setup):
    # Review metadata for the Council's reviewers; Nomos's intent hash covers neither `context` nor the item.
    p, council, *_ = setup(tool_definitions="called")
    await p.h3(call(turn=h1(p, DESCRIBED)), Next())
    digest = certified_item(council)["evidence"]["payload"]["result"]["stdout_sha256"]
    assert f"tool definitions stdout_sha256={digest}" in council.calls[0]["context"]


async def test_a_council_refusing_asserted_evidence_gets_none(setup):
    # allow_uncertified_evidence = false refuses any request whose `evidence` is non-empty.
    p, council, *_ = setup(tool_definitions="all")
    await p.h3(call(turn=h1(p, DESCRIBED)), Next())
    assert council.calls[0]["evidence"] == []


async def test_without_a_signing_key_nothing_is_sent_or_kept(setup):
    cfg = base_config(tool_definitions="called")
    del cfg["signing_key"]
    p, council, *_ = setup(config=cfg)
    out = p.h1("model", {"content": {"messages": [], "tools": [DESCRIBED]}}, None)
    assert p.store.db.execute("SELECT count(*) FROM tool_lists").fetchone()[0] == 0
    await p.h3(call(turn=TURN.search(nomos_message(out))[0]), Next())
    assert council.calls[0]["certified_evidence"] == [] and council.calls[0]["evidence"] == []
    assert "tool definitions" not in council.calls[0]["context"]


@pytest.mark.parametrize("value", ["called", {"value": "called"}])
def test_without_a_signing_key_the_option_is_reported_disabled(value):
    cfg = base_config(tool_definitions=value)
    del cfg["signing_key"]
    codes = [d.code for d in NomosPlugin().validate(cfg)]
    assert "nomos.config.tool_definitions_disabled" in codes
    assert "nomos.config.tool_definitions_disabled" not in [
        d.code for d in NomosPlugin().validate(base_config(tool_definitions=value))
    ]


def test_a_referenced_off_is_not_reported_disabled():
    cfg = base_config(tool_definitions={"value": "off"})
    del cfg["signing_key"]
    assert "nomos.config.tool_definitions_disabled" not in [d.code for d in NomosPlugin().validate(cfg)]


# ── citations and storage ────────────────────────────────────────────────


def test_on_reserves_one_evidence_slot(setup):
    p, *_ = setup(tool_definitions="called")
    out = p.h1("model", {"content": {"messages": [], "tools": [TOOL]}}, None)
    nomos = out.request["content"]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
    assert nomos["properties"]["evidence"]["maxItems"] == 19


async def test_on_refuses_twenty_citations_before_the_council(setup):
    p, council, *_ = setup(tool_definitions="called")
    ids = [f"ev_{i:032x}" for i in range(20)]
    out = await p.h3(call(intent={**INTENT, "evidence": ids}, turn=h1(p, TOOL)), Next())
    assert out.result["status"] == "missing_intent" and "at most 19" in out.result["message"]
    assert not council.calls


def test_identical_lists_are_stored_once_and_purged_with_their_turns(setup):
    p, *_ = setup(tool_definitions="all")
    count = lambda: p.store.db.execute("SELECT count(*) FROM tool_lists").fetchone()[0]  # noqa: E731
    h1(p, TOOL)
    h1(p, TOOL)
    assert count() == 1
    with p.store.db:
        p.store.db.execute("UPDATE turns SET expires=0")
    h1(p, LOCAL_TOOL)  # opening a turn purges expired ones
    assert count() == 1
    assert p.store.tool_list(sha256_hex(canonical([LOCAL_TOOL]))) == [LOCAL_TOOL]


# ── storage limits ───────────────────────────────────────────────────────


def described(n, size=4000):
    """A distinct tool definition about ``size`` bytes long."""
    tool = deepcopy(TOOL)
    tool["function"]["description"] = f"{n}:" + "x" * size
    return tool


def kept(plugin):
    return plugin.store.db.execute("SELECT count(*) FROM tool_lists").fetchone()[0]


def h1_after(plugin, token, *tools):
    """H1 for a request that continues the conversation ``token`` was issued in."""
    messages = [{"role": "user", "content": "go"}, {"role": "assistant", "content": f"used {token}"}]
    out = plugin.h1("model", {"content": {"messages": messages, "tools": list(tools)}}, None)
    return TURN.search(nomos_message(out))[0]


async def test_a_list_over_max_citation_bytes_is_not_kept_and_reported_unavailable(setup):
    p, council, *_ = setup(tool_definitions="all", limits={"max_citation_bytes": 2048})
    big = described(1, size=4000)
    await p.h3(call(turn=h1(p, big)), Next())
    observed = item(council)["observed"]
    assert kept(p) == 0
    assert observed["tools_sha256"] == sha256_hex(canonical([big]))
    assert "unavailable" in observed and not {"called", "tools", "tool_count"} & set(observed)


async def test_tool_lists_stop_being_kept_at_the_storage_budget(setup):
    # Reserve for one in-flight result is 81920 bytes here (four results: the record, the key's certified copy,
    # escaped twice, and the journal), leaving about 4 KB of the 86144 for tool lists.
    limits = {
        "max_result_bytes": 1024,
        "max_output_chars": 256,
        "max_storage_bytes": 86144,
        "conversation_share_percent": 100,
    }
    p, council, *_ = setup(tool_definitions="called", limits=limits)
    turns = [h1(p, described(n, size=1500)) for n in range(5)]
    assert kept(p) == 2
    await p.h3(call(turn=turns[-1]), Next())
    assert "unavailable" in item(council)["observed"]


def test_one_conversation_cannot_take_more_than_its_share(setup):
    # Per-conversation budget: 25% of 416384 = 104096, less the 81920 reserve: room for two 10 KB lists.
    limits = {
        "max_result_bytes": 1024,
        "max_output_chars": 256,
        "max_storage_bytes": 416384,
        "conversation_share_percent": 25,
    }
    p, *_ = setup(tool_definitions="called", limits=limits)
    token = h1(p, described(0, size=10000))
    for n in range(1, 4):
        token = h1_after(p, token, described(n, size=10000))
    assert kept(p) == 2
    h1(p, described(9, size=10000))  # a new conversation still has its own share
    assert kept(p) == 3


async def test_kept_tool_lists_count_toward_the_storage_quota(setup):
    limits = {
        "max_result_bytes": 1024,
        "max_output_chars": 256,
        "max_storage_bytes": 84096,
        "conversation_share_percent": 100,
    }
    p, *_ = setup(tool_definitions="called", limits=limits)
    t = h1(p, TOOL)
    with p.store.db:  # a list already filling the budget (as if the limits had been larger when it was kept)
        p.store.db.execute("INSERT INTO tool_lists VALUES (?, ?)", ("f" * 64, canonical(["x" * 10000])))
        p.store.db.execute("UPDATE turns SET tools_sha256=?", ("f" * 64,))
    out = await p.h3(call(turn=t), Next())
    assert out.result["status"] == "storage_quota_exceeded"


# ── review follow-ups (#22) ──────────────────────────────────────────────


async def test_reused_lists_are_admitted_per_conversation_and_never_block_its_calls(setup):
    # Per-conversation room for lists: 25% of 400000 less the 75776 reserve = 24224 bytes; each list is ~10 KB.
    limits = {
        "max_result_bytes": 1024,
        "max_output_chars": 256,
        "max_storage_bytes": 400000,
        "conversation_share_percent": 25,
    }
    p, council, *_ = setup("denied", tool_definitions="called", limits=limits)
    lists = [described(n, size=10000) for n in range(3)]
    for tool in lists:  # each kept by a conversation of its own
        h1(p, tool)
    assert kept(p) == 3
    token = h1(p, lists[0])
    for tool in lists[1:]:  # one conversation reusing all three already-kept lists
        token = h1_after(p, token, tool)
    out = await p.h3(call(turn=token), Next())
    # "denied" means the call passed the journal's quota check and reached the Council.
    assert out.result["status"] == "denied", "reused lists must not exhaust the conversation's quota"
    assert "unavailable" in item(council)["observed"], "the list over the conversation's share is not shown"


def test_count_eviction_drops_the_evicted_turns_list_at_once(setup):
    p, *_ = setup(tool_definitions="all", limits={"max_invocations": 1})
    h1(p, TOOL)
    h1(p, LOCAL_TOOL)
    assert kept(p) == 1


async def test_a_codec_provider_native_tool_is_found_by_its_value_name(setup):
    p, council, *_ = setup(tool_definitions="called")
    native = {
        "type": "provider_native",
        "provider": "anthropic",
        "kind": "bash_20250124",
        "value": {"type": "bash_20250124", "name": "run_shell"},
    }
    annotated = {
        "messages": [{"role": "user", "content": "go"}],
        "tools": [native],
        "api_specific": {"api": "anthropic_messages"},
    }
    out = p.h1("model", {"content": {"messages": [], "tools": []}}, annotated)
    t = TURN.search(out.annotated_request["instructions"])[0]
    await p.h3(call(turn=t), Next())
    assert item(council)["observed"]["called"] == native


async def test_definitions_follow_a_cited_denial(setup):
    p, council, *_ = setup("denied", tool_definitions="called")
    first_call = call(turn=h1(p, TOOL))
    first = await p.h3(first_call, Next())  # a council_denial, certified by the plugin
    out = p.h1(
        "model", {"content": {"messages": history("go", (first_call, first.result)), "tools": [TOOL]}}, None
    )
    t = TURN.search(nomos_message(out))[0]
    await p.h3(
        call(intent={**INTENT, "evidence": [first.result["evidence_id"]]}, turn=t, call_id="c2"), Next()
    )
    sent = council.calls[1]
    assert sent["evidence"] == [] and len(sent["certified_evidence"]) == 2, (
        "the cited denial, then the definitions"
    )
    kinds = [json.loads(e["stdout"])["type"] for e in sent["certified_evidence"]]
    assert kinds == ["nomos.council_decision", "nomos.observed_tool_definitions"]
