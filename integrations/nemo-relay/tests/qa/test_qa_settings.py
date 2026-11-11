# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `settings`: SCHEMA, diagnostics, Settings defaults, Guard, policies, semantic errors."""

from __future__ import annotations

import copy
import importlib.resources
import json
import os
import time
import tomllib
from pathlib import Path

import jsonschema
import pytest
import qa_support as qa

from nemo_relay_nomos import settings as st

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SECRET = "s3cr3t-QA-SETTINGS-never-in-a-diagnostic-77aa"


def _diags(raw, environ=None):
    return st.diagnostics(raw, environ={} if environ is None else environ)


def _all_text(diags) -> str:
    return " | ".join(f"{d.field}: {d.message}" for d in diags)


# ---- SCHEMA and config.schema.json ----------------------------------------------------------------


def test_config_schema_json_named_by_the_manifest_equals_dumped_schema():
    manifest = tomllib.loads((PLUGIN_ROOT / "relay-plugin.toml").read_text(encoding="utf-8"))
    path = PLUGIN_ROOT / manifest["config_schema"]["path"]
    want = json.dumps(st.SCHEMA, indent=2) + "\n"
    got = path.read_text(encoding="utf-8")
    assert got == want, f"{path} differs from json.dumps(SCHEMA, indent=2) + '\\n'"


def test_packaged_config_schema_json_equals_dumped_schema():
    resource = importlib.resources.files("nemo_relay_nomos") / "config.schema.json"
    if not resource.is_file():
        pytest.skip("no packaged nemo_relay_nomos/config.schema.json")
    want = json.dumps(st.SCHEMA, indent=2) + "\n"
    assert resource.read_text(encoding="utf-8") == want, "packaged config.schema.json differs from SCHEMA"


def test_schema_is_a_valid_json_schema():
    jsonschema.validators.validator_for(st.SCHEMA).check_schema(st.SCHEMA)


def _wrap_leaves(node):
    if isinstance(node, dict):
        return {k: _wrap_leaves(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_wrap_leaves(v) for v in node]
    return {"value": node}


def _full_config() -> dict:
    return qa.raw_config(
        connection={
            "mcp_url": "http://127.0.0.1:9901/mcp",
            "executor_url": "http://127.0.0.1:9907",
            "agent_id": qa.AGENT_ID,
            "council_id": qa.COUNCIL_ID,
            "auth_token": "tok",
            "proxy_token": "ptok",
            "working_directory": "/workspace",
            "timeout_s": 30.0,
        },
        guard={"include": ["run_shell", "deploy_*"], "exclude": ["deploy_prod"]},
        tools={"run_shell": {"executor": "nomos_http", "command_argument": "command"}},
        default_executor="local",
        writ_handler={"kind": "default", "claim": "auto"},
        max_denials=3,
        catalog={"enabled": True, "max_entries": 20, "max_argument_chars": 400},
        signing_key=qa.PLUGIN_SEED.hex(),
    )


def test_schema_accepts_a_value_reference_at_every_string_number_and_boolean_leaf():
    raw = _wrap_leaves(_full_config())
    errors = sorted(jsonschema.validators.validator_for(st.SCHEMA)(st.SCHEMA).iter_errors(raw), key=str)
    assert not errors, f"SCHEMA rejected leaf references: {[e.message for e in errors][:5]!r}"
    assert _diags(raw) == [], f"diagnostics for all-leaves-wrapped config: {_all_text(_diags(raw))}"


@pytest.mark.parametrize(
    "ref",
    [
        {"env": "QA_X"},
        {"file": "/abs/x"},
        {"encrypted": "ct", "key_env": "K"},
        {"encrypted": "ct", "key_file": "/k"},
    ],
)
def test_schema_accepts_every_reference_form_at_a_string_leaf(ref):
    raw = qa.raw_config()
    raw["connection"]["auth_token"] = ref
    errors = list(jsonschema.validators.validator_for(st.SCHEMA)(st.SCHEMA).iter_errors(raw))
    assert not errors, f"SCHEMA rejected auth_token={ref!r}: {[e.message for e in errors]!r}"


# ---- diagnostics / from_config --------------------------------------------------------------------


def test_minimal_config_is_valid(qa_config):
    got = _diags(qa_config)
    assert got == [], f"minimal config {qa_config!r} produced diagnostics: {_all_text(got)}"


def test_diagnostic_is_a_field_message_named_tuple(qa_config):
    raw = copy.deepcopy(qa_config)
    del raw["connection"]["agent_id"]
    diags = _diags(raw)
    assert diags, "missing connection.agent_id must produce a diagnostic"
    d = diags[0]
    field, message = d
    assert (field, message) == (d.field, d.message), f"Diagnostic {d!r} is not a (field, message) named tuple"
    assert isinstance(d, tuple) and isinstance(d.message, str), f"Diagnostic {d!r} has wrong types"


@pytest.mark.parametrize(
    "path",
    [("connection", "agent_id"), ("connection", "council_id")],
    ids=".".join,
)
def test_required_fields_are_diagnosed_when_missing(qa_config, path):
    raw = copy.deepcopy(qa_config)
    del raw[path[0]][path[1]]
    assert _diags(raw), f"config without {'.'.join(path)} must produce diagnostics"


@pytest.mark.parametrize(
    "mutate,desc",
    [
        (lambda r: r.__setitem__("max_denials", "three"), "max_denials='three'"),
        (lambda r: r["connection"].__setitem__("timeout_s", "slow"), "timeout_s='slow'"),
        (lambda r: r.__setitem__("default_executor", "ssh"), "default_executor='ssh'"),
        (lambda r: r.__setitem__("writ_handler", {"kind": "magic"}), "writ_handler.kind='magic'"),
        (lambda r: r.__setitem__("writ_handler", {"claim": "never"}), "writ_handler.claim='never'"),
        (lambda r: r.__setitem__("tools", {"t": {"executor": "ssh"}}), "tools.t.executor='ssh'"),
        (lambda r: r.__setitem__("connection", "not-an-object"), "connection='not-an-object'"),
        (lambda r: r.__setitem__("signing_key", "ab" * 31 + "a"), "signing_key of 63 hex"),
        (lambda r: r.__setitem__("signing_key", "zz" * 32), "signing_key non-hex"),
        (lambda r: r.__setitem__("signing_key", "ab" * 33), "signing_key of 66 hex"),
    ],
)
def test_wrong_type_or_shape_is_diagnosed(qa_config, mutate, desc):
    raw = copy.deepcopy(qa_config)
    mutate(raw)
    assert _diags(raw), f"config with {desc} must produce diagnostics"


def test_from_config_raises_settings_error_when_diagnostics_non_empty(qa_config):
    raw = copy.deepcopy(qa_config)
    del raw["connection"]["agent_id"]
    with pytest.raises(st.SettingsError):
        st.Settings.from_config(raw, environ={})


def test_from_config_of_minimal_config_succeeds(qa_config):
    s = st.Settings.from_config(qa_config, environ={})
    assert s.connection.agent_id == qa.AGENT_ID, f"agent_id {s.connection.agent_id!r}"
    assert s.session.id == qa.SESSION_ID, f"session.id {s.session.id!r}"


def test_session_is_optional_and_defaults_to_a_random_activation_id(qa_config):
    raw = copy.deepcopy(qa_config)
    del raw["session"]
    assert _diags(raw) == [], f"a config without session must be valid: {_diags(raw)!r}"
    a = st.Settings.from_config(raw, environ={}).session.id
    b = st.Settings.from_config(raw, environ={}).session.id
    assert a.startswith("activation-") and len(a) == len("activation-") + 12 and a != b, (a, b)


def test_storage_dir_is_no_longer_accepted(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["session"]["storage_dir"] = "/var/lib/nomos"
    assert any(d.field.startswith("session") for d in _diags(raw)), f"storage_dir accepted: {_diags(raw)!r}"


def test_settings_defaults(qa_config):
    s = st.Settings.from_config(qa_config, environ={})
    c = s.connection
    got = (c.mcp_url, c.executor_url, c.auth_token, c.proxy_token, c.working_directory, c.timeout_s)
    want = ("http://127.0.0.1:9901/mcp", "http://127.0.0.1:9907", "", "", "/workspace", 180.0)
    assert got == want, f"connection defaults {got!r}, want {want!r}"
    assert tuple(s.guard.include) == ("run_shell",) and tuple(s.guard.exclude) == (), f"guard {s.guard!r}"
    assert s.tools == {"run_shell": st.ToolPolicy("nomos_http", "command")}, f"tools default {s.tools!r}"
    assert s.default_executor == "local", f"default_executor {s.default_executor!r}"
    wh = s.writ_handler
    assert (wh.kind, wh.claim, wh.entrypoint, dict(wh.options)) == ("default", "auto", None, {}), f"{wh!r}"
    assert s.max_denials == 3, f"max_denials {s.max_denials!r}"
    cat = s.catalog
    assert (cat.enabled, cat.max_entries, cat.max_argument_chars) == (True, 20, 400), f"catalog {cat!r}"
    assert s.signing_key is None, f"signing_key default {s.signing_key!r}"
    lim = s.limits
    got_lim = (
        lim.max_result_bytes,
        lim.max_storage_bytes,
        lim.max_records,
        lim.max_invocations,
        lim.retention_seconds,
        lim.max_citation_bytes,
        lim.max_output_chars,
        lim.conversation_share_percent,
    )
    want_lim = (2 * 1024**2, 64 * 1024**2, 1000, 10000, 3600, 4 * 1024**2, 65536, 25)
    assert got_lim == want_lim, f"limits defaults {got_lim!r}, want {want_lim!r}"


def test_limits_defaults_without_config():
    lim = st.Limits()
    assert (lim.max_result_bytes, lim.max_records, lim.retention_seconds) == (2 * 1024**2, 1000, 3600), (
        f"Limits() defaults {lim!r}"
    )


def test_trust_is_carried_into_settings(qa_config):
    s = st.Settings.from_config(qa_config, environ={})
    t = qa_config["trust"]
    assert s.trust.policy_version == t["policy_version"], f"trust.policy_version {s.trust.policy_version!r}"
    assert s.trust.council_public_key_hex == t["council_public_key_hex"]
    assert {k: list(v) for k, v in s.trust.executors.items()} == t["executors"], (
        f"executors {s.trust.executors!r}"
    )


def test_signing_key_of_64_hex_is_accepted(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["signing_key"] = qa.PLUGIN_SEED.hex()
    assert _diags(raw) == [], f"signing_key={raw['signing_key']!r}: {_all_text(_diags(raw))}"


# ---- references in settings -----------------------------------------------------------------------


def test_env_reference_resolves_into_settings(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["connection"]["auth_token"] = {"env": "QA_AUTH"}
    s = st.Settings.from_config(raw, environ={"QA_AUTH": SECRET})
    assert s.connection.auth_token == SECRET, (
        f"auth_token {{'env':'QA_AUTH'}} resolved to {s.connection.auth_token!r}"
    )


def test_file_reference_resolves_into_settings(qa_config, tmp_path):
    secret = tmp_path / "tok"
    secret.write_text(SECRET + "\n")
    os.chmod(secret, 0o600)
    raw = copy.deepcopy(qa_config)
    raw["connection"]["proxy_token"] = {"file": str(secret)}
    s = st.Settings.from_config(raw, environ={})
    assert s.connection.proxy_token == SECRET, (
        f"proxy_token file ref resolved to {s.connection.proxy_token!r}"
    )


def test_value_references_resolve_numbers_and_booleans(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["connection"]["timeout_s"] = {"value": 5}
    raw["max_denials"] = {"value": 7}
    raw["catalog"] = {"enabled": {"value": False}}
    s = st.Settings.from_config(raw, environ={})
    assert (s.connection.timeout_s, s.max_denials, s.catalog.enabled) == (5, 7, False), (
        f"value refs resolved to {(s.connection.timeout_s, s.max_denials, s.catalog.enabled)!r}"
    )


def test_missing_env_reference_is_a_diagnostic_on_its_dotted_field(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["connection"]["auth_token"] = {"env": "QA_ABSENT"}
    diags = _diags(raw)
    assert any(d.field == "connection.auth_token" for d in diags), (
        f"missing env QA_ABSENT on connection.auth_token gave {_all_text(diags)}"
    )


@pytest.mark.parametrize("key", ["key_env", "key_file"])
def test_encrypted_reference_is_a_not_supported_yet_diagnostic(qa_config, key):
    raw = copy.deepcopy(qa_config)
    raw["connection"]["auth_token"] = {"encrypted": "ct", key: "QA_KEY" if key == "key_env" else "/k"}
    diags = _diags(raw, environ={"QA_KEY": SECRET})
    assert any("not supported yet" in d.message for d in diags), (
        f"encrypted ({key}) diagnostics: {_all_text(diags)}"
    )
    assert SECRET not in _all_text(diags), f"encrypted diagnostics leaked the key: {_all_text(diags)}"


def test_diagnostics_never_contain_a_secret_from_a_bad_mode_file(qa_config, tmp_path):
    secret = tmp_path / "tok"
    secret.write_text(SECRET)
    os.chmod(secret, 0o644)
    raw = copy.deepcopy(qa_config)
    raw["connection"]["auth_token"] = {"file": str(secret)}
    raw["connection"]["proxy_token"] = {"env": "QA_PROXY"}
    raw["max_denials"] = {"env": "QA_DENIALS_ABSENT"}
    diags = _diags(raw, environ={"QA_PROXY": SECRET})
    assert diags, "0644 secret file must produce a diagnostic"
    assert SECRET not in _all_text(diags), f"diagnostics leaked a secret: {_all_text(diags)}"


def test_resolution_errors_come_before_semantic_errors(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["connection"]["auth_token"] = {"env": "QA_ABSENT"}
    raw["writ_handler"] = {"kind": "custom"}
    diags = _diags(raw)
    res = [i for i, d in enumerate(diags) if d.field == "connection.auth_token"]
    sem = [i for i, d in enumerate(diags) if "custom writ handlers are not implemented yet" in d.message]
    assert res, f"resolution error missing from {_all_text(diags)}"
    if sem:
        assert max(res) < min(sem), f"semantic error precedes resolution error: {_all_text(diags)}"


# ---- semantic errors ------------------------------------------------------------------------------


def test_nomos_http_tool_without_command_argument_is_diagnosed(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["tools"] = {"qa_http": {"executor": "nomos_http"}}
    raw["guard"] = {"include": ["qa_http"]}
    assert _diags(raw), f"tools={raw['tools']!r} (nomos_http, no command_argument) must be diagnosed"


def test_custom_writ_handler_is_diagnosed_as_not_implemented(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["writ_handler"] = {"kind": "custom", "entrypoint": "pkg.mod:Handler"}
    diags = _diags(raw)
    assert any(d.message == "custom writ handlers are not implemented yet" for d in diags) or any(
        "custom writ handlers are not implemented yet" in d.message for d in diags
    ), f"writ_handler.kind=custom gave {_all_text(diags)}"


def test_council_claim_is_diagnosed_as_no_claim_api(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["writ_handler"] = {"claim": "council"}
    diags = _diags(raw)
    assert any("claim" in d.message.lower() for d in diags), (
        f"writ_handler.claim=council gave {_all_text(diags)}"
    )


def test_executor_claim_for_a_local_tool_is_diagnosed(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["tools"] = {"qa_local": {"executor": "local"}}
    raw["guard"] = {"include": ["qa_local"]}
    raw["writ_handler"] = {"claim": "executor"}
    assert _diags(raw), "claim=executor with a local tool must be diagnosed"


def test_expired_trust_is_diagnosed(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["trust"]["valid_until"] = int(time.time()) - 60
    assert _diags(raw), f"trust.valid_until={raw['trust']['valid_until']} (past) must be diagnosed"


def test_storage_budget_too_small_for_one_result_is_diagnosed(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["limits"] = {"max_result_bytes": 1024 * 1024, "max_storage_bytes": 1024}
    assert _diags(raw), f"limits={raw['limits']!r} must be diagnosed"


def test_storage_budget_too_small_for_one_result_within_a_conversations_share_is_diagnosed(qa_config):
    # 8 MiB holds one default result (about 2.3 MiB) store-wide, but not within a 25% share.
    raw = copy.deepcopy(qa_config)
    raw["limits"] = {"max_storage_bytes": 8 * 1024**2}
    assert _diags(raw), f"limits={raw['limits']!r} must be diagnosed"
    raw["limits"]["conversation_share_percent"] = 100
    assert not _diags(raw), f"limits={raw['limits']!r} must be accepted"


# ---- Guard and policy -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "include,exclude,name,want",
    [
        (("run_shell",), (), "run_shell", True),
        (("run_shell",), (), "read_file", False),
        (("*",), (), "anything", True),
        (("*",), (), "nomos_evidence_list", False),
        (("*",), (), "nomos_", False),
        (("nomos_*",), (), "nomos_evidence_get", False),
        (("nomos_evidence_get",), (), "nomos_evidence_get", False),
        (("run_*",), ("run_unsafe",), "run_shell", True),
        (("run_*",), ("run_unsafe",), "run_unsafe", False),
        (("run_*",), (), "Run_shell", False),
        (("*",), ("*",), "run_shell", False),
        ((), (), "run_shell", False),
        (("deploy_?",), (), "deploy_a", True),
        (("deploy_?",), (), "deploy_ab", False),
        (("*",), ("read_*",), "read_file", False),
        (("*",), ("read_*",), "write_file", True),
        (("*",), (), "nomosx", True),
    ],
)
def test_guard_matches_include_globs_minus_exclude_globs_never_nomos_prefix(include, exclude, name, want):
    g = st.Guard(include=include, exclude=exclude)
    got = g.matches(name)
    assert got is want, f"Guard(include={include!r}, exclude={exclude!r}).matches({name!r}) -> {got!r}"


def test_guard_from_config(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["guard"] = {"include": ["*"], "exclude": ["safe_*"]}
    s = st.Settings.from_config(raw, environ={})
    got = (s.guard.matches("run_shell"), s.guard.matches("safe_read"), s.guard.matches("nomos_x"))
    assert got == (True, False, False), f"guard {raw['guard']!r} matched {got!r}"


def test_policy_returns_configured_policy_or_default_executor(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["tools"] = {"run_shell": {"executor": "nomos_http", "command_argument": "cmd"}}
    raw["default_executor"] = "local"
    s = st.Settings.from_config(raw, environ={})
    assert s.policy("run_shell") == st.ToolPolicy("nomos_http", "cmd"), (
        f"policy(run_shell) {s.policy('run_shell')!r}"
    )
    assert s.policy("unlisted") == st.ToolPolicy("local", None), f"policy(unlisted) {s.policy('unlisted')!r}"


def test_structured_tool_policy_requires_local_executor_and_operator_subject(qa_config):
    raw = copy.deepcopy(qa_config)
    raw["tools"] = {"send_message": {"executor": "local"}}
    raw.pop("tool_call", None)
    assert any(d.field == "tool_call.subject" for d in _diags(raw))

    raw["tool_call"] = {"subject": "opaque-user"}
    assert _diags(raw) == []
    policy = st.Settings.from_config(raw, environ={}).policy("send_message")
    assert policy == st.ToolPolicy("local", None)

    raw["tools"]["send_message"]["command_argument"] = "command"
    assert any(d.field == "tools.send_message.command_argument" for d in _diags(raw))
