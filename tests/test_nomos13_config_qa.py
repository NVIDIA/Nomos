# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent NOMOS-13 C01-C07 acceptance, authored from frozen design only.

Run with PYTHONPATH pointing at the target checkout. No implementation source,
engineer tests, NAT, live LLM, or external service is needed. New API imports are
lazy: existing-interface behavioral failures remain measurable on the base.
"""

from __future__ import annotations

import copy
import importlib
import json
import math
import socket
import urllib.request
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nomos.config.council_catalog_store import CouncilCatalogStore, CouncilCatalogStoreError
from nomos.config.settings import CouncilSettings, load_config, load_council_catalog
from nomos.level_0.types.tool_call import ToolCallRequest

CONFIG_ERRORS = (ValueError, TypeError, yaml.YAMLError, CouncilCatalogStoreError)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def schema():
    return {
        "type": "object",
        "properties": {
            "table": {"const": "scratch"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["table", "limit"],
        "additionalProperties": False,
    }


def definition(name="warehouse.delete_rows", **changes):
    item = {
        "name": name,
        "description": "Delete selected scratch rows.",
        "parameters": schema(),
        "usage_constraints": ["Only on the operator-authorized scratch table."],
    }
    item.update(changes)
    return item


def settings_with(items):
    return CouncilSettings(council_id="alpha", tools=items)


def write_yaml(path, value):
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return path


def exported_tools(settings):
    return settings.model_dump(mode="json")["tools"]


@pytest.fixture
def schema_api():
    return importlib.import_module("nomos.level_0.tool_schema")


@pytest.fixture
def resolver_api():
    return importlib.import_module("nomos.level_0.tool_catalog")


def call(name="warehouse.delete_rows", arguments=None):
    return ToolCallRequest(
        tool_name=name,
        arguments=arguments if arguments is not None else {"table": "scratch", "limit": 5},
        subject="scratch",
    )


# C01/C07: omission, explicit empty and explicit null must remain distinct.
def test_c01_legacy_omitted_control():
    value = CouncilSettings(council_id="alpha")
    assert "tools" not in value.model_dump(mode="json")
    assert "tools" not in CouncilSettings.model_validate(value.model_dump(mode="json")).model_dump(
        mode="json"
    )


def test_c01_empty_survives_dump_and_reload():
    value = settings_with([])
    assert exported_tools(value) == []
    assert exported_tools(CouncilSettings.model_validate(value.model_dump(mode="json"))) == []


@pytest.mark.parametrize("value", [None, {}, "", "tools", 1, True])
def test_c01_wrong_catalog_type_rejected(value):
    with pytest.raises(ValidationError):
        settings_with(value)


@pytest.mark.parametrize("mode", ["single", "inline", "referenced"])
def test_c01_load_modes_preserve_complete_definition(tmp_path, mode):
    item = definition()
    profile = {"council_id": "alpha", "tools": [item]}
    if mode == "single":
        target = write_yaml(tmp_path / "profile.yaml", profile)
        actual = load_config(str(target))
    else:
        entry = profile
        if mode == "referenced":
            write_yaml(tmp_path / "profile.yaml", profile)
            entry = {"config_file": "profile.yaml"}
        target = write_yaml(
            tmp_path / "catalog.yaml", {"default_council_id": "alpha", "councils": {"alpha": entry}}
        )
        actual = load_council_catalog(str(target)).get("alpha")
    assert exported_tools(actual) == [item]


def test_c01_shared_lists_replace_and_councils_isolate(tmp_path):
    a, b = (
        definition(description="Alpha only"),
        definition(
            description="Beta only",
            parameters={"type": "object", "properties": {"flag": {"const": True}}},
        ),
    )
    target = write_yaml(
        tmp_path / "catalog.yaml",
        {
            "default_council_id": "alpha",
            "shared": {"tools": [a]},
            "councils": {"alpha": {}, "beta": {"tools": [b]}, "empty": {"tools": []}},
        },
    )
    catalog = load_council_catalog(str(target))
    assert exported_tools(catalog.get("alpha")) == [a]
    assert exported_tools(catalog.get("beta")) == [b]
    assert exported_tools(catalog.get("empty")) == []


def test_c01_referenced_override_replaces_list(tmp_path):
    write_yaml(tmp_path / "base.yaml", {"council_id": "alpha", "tools": [definition()]})
    target = write_yaml(
        tmp_path / "catalog.yaml",
        {
            "default_council_id": "alpha",
            "councils": {"alpha": {"config_file": "base.yaml", "tools": []}},
        },
    )
    assert exported_tools(load_council_catalog(str(target)).get()) == []


# C02: invalid declarations must raise at the existing settings boundary.
@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        " leading",
        "trailing ",
        "bad name",
        "\tbad",
        "bad\n",
        "x\x00",
        "x\x7f",
        "工具",
        "é",
        "_start",
        "-start",
        ".start",
        "/start",
        "x" * 129,
        7,
        None,
        True,
    ],
)
def test_c02_invalid_names(name):
    with pytest.raises(ValidationError):
        settings_with([definition(name=name)])


@pytest.mark.parametrize("name", ["a", "Z", "7", "A0_.:/-", "a" * 128])
def test_c02_valid_name_boundaries(name):
    assert exported_tools(settings_with([definition(name=name)]))[0]["name"] == name


@pytest.mark.parametrize(
    "mutation",
    [
        {"description": ""},
        {"description": " \t\n"},
        {"description": 1},
        {"description": "x" * 8001},
        {"usage_constraints": []},
        {"usage_constraints": [""]},
        {"usage_constraints": [" \t"]},
        {"usage_constraints": [1]},
        {"usage_constraints": "do safely"},
        {"usage_constraints": ["x"] * 17},
        {"usage_constraints": ["x" * 2001]},
        {"unknown": "never ignored"},
        {"parameters": []},
        {"parameters": None},
        {"parameters": {"type": "array"}},
        {"parameters": {}},
        {"parameters": {"type": "object", "properties": {"x": {"maximum": math.inf}}}},
        {"parameters": {"type": "object", "default": {1: "nonstring key"}}},
    ],
)
def test_c02_invalid_fields(mutation):
    with pytest.raises(CONFIG_ERRORS):
        settings_with([definition(**mutation)])


@pytest.mark.parametrize("missing", ["name", "description", "parameters", "usage_constraints"])
def test_c02_missing_fields(missing):
    item = definition()
    del item[missing]
    with pytest.raises(ValidationError):
        settings_with([item])


def test_c02_text_and_count_boundaries():
    item = definition(description="x" * 8000, usage_constraints=["x" * 2000] * 16)
    assert exported_tools(settings_with([item])) == [item]
    items = [definition(name=f"tool{i}") for i in range(32)]
    assert len(exported_tools(settings_with(items))) == 32
    with pytest.raises(ValidationError):
        settings_with([*items, definition(name="tool32")])


def test_c02_duplicate_names_and_exact_case():
    with pytest.raises(ValidationError):
        settings_with([definition(), definition(description="Conflicting definition")])
    assert (
        len(exported_tools(settings_with([definition(name="tool"), definition(name="Tool")]))) == 2
    )


def schema_bytes(size):
    value = {"type": "object", "description": ""}
    value["description"] = "x" * (size - len(canonical(value).encode()))
    assert len(canonical(value).encode()) == size
    return value


def test_c02_schema_utf8_byte_boundary():
    assert exported_tools(settings_with([definition(parameters=schema_bytes(32000))]))[0][
        "parameters"
    ] == schema_bytes(32000)
    with pytest.raises(CONFIG_ERRORS):
        settings_with([definition(parameters=schema_bytes(32001))])
    value = {"type": "object", "description": "é" * 16000}
    assert len(canonical(value).encode()) > 32000
    with pytest.raises(CONFIG_ERRORS):
        settings_with([definition(parameters=value)])


def catalog_bytes(size):
    items = [
        definition(name=f"t{i}", parameters={"type": "object", "description": "x" * 24000})
        for i in range(5)
    ]
    items[-1]["description"] += "x" * (size - len(canonical(items).encode()))
    assert len(items[-1]["description"]) <= 8000
    assert len(canonical(items).encode()) == size
    return items


def test_c02_catalog_byte_boundary():
    items = catalog_bytes(128000)
    assert len(exported_tools(settings_with(items))) == 5
    with pytest.raises(CONFIG_ERRORS):
        settings_with(catalog_bytes(128001))


def test_c02_depth_nodes_cycles_and_nonjson_fail_closed():
    deep = {"type": "object"}
    node = deep
    for _ in range(70):
        node["properties"] = {"x": {"type": "object"}}
        node = node["properties"]["x"]
    many = {"type": "object", "examples": [0] * 10001}
    cyclic = {"type": "object"}
    cyclic["default"] = cyclic
    for invalid in [deep, many, cyclic, {"type": "object", "default": {"set"}}]:
        with pytest.raises(CONFIG_ERRORS):
            settings_with([definition(parameters=invalid)])


# C03: malicious syntax uses actual safe operator entry points.
@pytest.mark.parametrize(
    "fragment",
    [
        "name: first\n    name: second",
        "parameters: {type: object}\n    parameters: {type: object}",
        "parameters: {type: object, required: [a], required: [b]}",
        "parameters: {type: object, properties: {a: {type: string}, a: {type: integer}}}",
        "parameters: {type: object, properties: {a: {type: string, type: integer}}}",
    ],
)
def test_c03_duplicate_yaml_keys_rejected(tmp_path, fragment):
    text = (
        "council_id: alpha\ntools:\n  - description: safe\n    usage_constraints: [safe]\n    "
        + fragment
        + "\n"
    )
    # Add otherwise required fields so rejection cannot be blamed on omissions.
    if not fragment.startswith("name:"):
        text += "    name: delete_rows\n"
    if not fragment.startswith("parameters:"):
        text += "    parameters: {type: object}\n"
    path = tmp_path / "duplicate.yaml"
    path.write_text(text)
    with pytest.raises(CONFIG_ERRORS):
        load_config(str(path))


@pytest.mark.parametrize(
    "payload",
    [
        "!!python/object/apply:builtins.eval ['1+1']",
        "{council_id: alpha, tools: [{name: t, description: safe, "
        "usage_constraints: [safe], parameters: &cycle {type: object, default: *cycle}}]}",
        "{council_id: alpha, tools: [{name: t, description: safe, "
        "usage_constraints: [safe], parameters: {type: object, default: .nan}}]}",
        "{council_id: alpha, tools: [{name: t, description: safe, "
        "usage_constraints: [safe], parameters: {type: object, default: .inf}}]}",
    ],
)
def test_c03_unsafe_cyclic_nonfinite_yaml(tmp_path, payload):
    path = tmp_path / "unsafe.yaml"
    path.write_text(payload)
    with pytest.raises(CONFIG_ERRORS):
        load_config(str(path))


# C04/C05: full schema behavior and denial without transformation.
SCHEMA_CASES = [
    (
        {
            "type": "object",
            "properties": {
                "x": {
                    "type": "object",
                    "properties": {"n": {"type": "number", "exclusiveMinimum": 0}},
                    "required": ["n"],
                    "additionalProperties": False,
                }
            },
            "required": ["x"],
            "additionalProperties": False,
        },
        {"x": {"n": 1}},
        [{"x": {}}, {"x": {"n": 0}}, {"x": {"n": 1, "z": 2}}, {"x": {"n": 1}, "z": 2}],
    ),
    (
        {"type": "object", "properties": {"x": {"enum": ["a", "b"]}}, "required": ["x"]},
        {"x": "a"},
        [{"x": "c"}, {}],
    ),
    (
        {
            "type": "object",
            "allOf": [{"properties": {"n": {"minimum": 2}}}, {"properties": {"n": {"maximum": 4}}}],
            "required": ["n"],
        },
        {"n": 3},
        [{"n": 1}, {"n": 5}],
    ),
    (
        {"type": "object", "oneOf": [{"required": ["a"]}, {"required": ["b"]}]},
        {"a": 1},
        [{}, {"a": 1, "b": 1}],
    ),
    (
        {
            "type": "object",
            "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
            "not": {"required": ["forbidden"]},
        },
        {"a": 1, "b": 2},
        [{}, {"a": 1, "forbidden": True}],
    ),
    (
        {
            "type": "object",
            "if": {"properties": {"kind": {"const": "delete"}}, "required": ["kind"]},
            "then": {"required": ["confirm"]},
            "else": {"not": {"required": ["confirm"]}},
        },
        {"kind": "delete", "confirm": True},
        [{"kind": "delete"}, {"kind": "read", "confirm": True}],
    ),
    (
        {"type": "object", "dependentRequired": {"credit": ["billing"]}},
        {"credit": 1, "billing": "x"},
        [{"credit": 1}],
    ),
    (
        {
            "type": "object",
            "properties": {
                "xs": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                }
            },
            "required": ["xs"],
        },
        {"xs": [1, 2]},
        [{"xs": []}, {"xs": [1, 1]}, {"xs": [1, 2, 3]}, {"xs": ["1"]}],
    ),
    (
        {
            "type": "object",
            "allOf": [{"properties": {"a": {"type": "string"}}}],
            "unevaluatedProperties": False,
        },
        {"a": "ok"},
        [{"a": "ok", "b": 1}],
    ),
    (
        {
            "type": "object",
            "$defs": {"number": {"type": "integer", "maximum": 5}},
            "properties": {"n": {"$ref": "#/$defs/number"}},
            "required": ["n"],
        },
        {"n": 5},
        [{"n": 6}, {"n": "5"}],
    ),
    (
        {
            "type": "object",
            "$defs": {"number": {"$anchor": "bounded", "type": "integer", "minimum": 1}},
            "properties": {"n": {"$ref": "#bounded"}},
        },
        {"n": 1},
        [{"n": 0}],
    ),
    (
        {"type": "object", "properties": {"allowed": True, "forbidden": False}},
        {"allowed": 1},
        [{"forbidden": 1}],
    ),
]


@pytest.mark.parametrize(
    "spec,valid,invalid",
    SCHEMA_CASES,
    ids=[
        "nested",
        "enum",
        "allOf",
        "oneOf",
        "anyOf-not",
        "conditional",
        "dependentRequired",
        "arrays",
        "unevaluated",
        "local-pointer",
        "local-anchor",
        "boolean",
    ],
)
def test_c04_composition_assertions(schema_api, spec, valid, invalid):
    schema_api.validate_tool_schema(spec)
    before = copy.deepcopy(valid)
    schema_api.validate_tool_arguments(spec, valid)
    assert valid == before
    for bad in invalid:
        with pytest.raises(schema_api.ToolSchemaError):
            schema_api.validate_tool_arguments(spec, bad)


def test_c04_defaults_and_annotations_do_not_execute_or_mutate(schema_api):
    spec = {
        "type": "object",
        "properties": {
            "email": {"type": "string", "format": "email"},
            "count": {"type": "integer", "default": 3},
            "blob": {"contentEncoding": "base64", "contentMediaType": "application/json"},
        },
        "examples": [{"$ref": "https://never.invalid/annotation", "$id": "file:///annotation"}],
        "x-custom": {"$ref": "https://never.invalid/annotation"},
        "default": {"$ref": "file:///annotation"},
    }
    args = {"email": "not-an-email", "blob": "not base64"}
    before = copy.deepcopy(args)
    schema_api.validate_tool_schema(spec)
    schema_api.validate_tool_arguments(spec, args)
    assert args == before and "count" not in args


@pytest.mark.parametrize(
    "change",
    [
        {"required": "x"},
        {"properties": []},
        {"allOf": {}},
        {"type": "madeup"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"$schema": "https://never.invalid/dialect"},
        {"$vocabulary": {"https://never.invalid/vocabulary": True}},
        {"$id": "urn:rebased"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "#missing"},
        {"$defs": {"unused": {"$id": "https://never.invalid/rebase"}}},
    ],
)
def test_c05_invalid_schema_config_rejected(schema_api, change):
    spec = {"type": "object", **change}
    with pytest.raises(schema_api.ToolSchemaError):
        schema_api.validate_tool_schema(spec)


@pytest.mark.parametrize(
    "ref",
    [
        "https://never.invalid/schema",
        "http://127.0.0.1/schema",
        "file:///tmp/config_qa-secret",
        "relative.json",
        "/tmp/config_qa-secret",
        "urn:forbidden",
    ],
)
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_c05_external_unused_refs_never_fetch(schema_api, monkeypatch, ref, keyword):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append("retrieval")
        raise AssertionError("schema attempted external retrieval")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(Path, "open", blocked)
    spec = {"type": "object", "$defs": {"unused": {keyword: ref}}}
    with pytest.raises(schema_api.ToolSchemaError):
        schema_api.validate_tool_schema(spec)
    assert attempts == []


def test_c05_recursive_evaluation_normalizes_failure(schema_api):
    spec = {"type": "object", "$ref": "#"}
    with pytest.raises(schema_api.ToolSchemaError):
        schema_api.validate_tool_arguments(spec, {})


def test_c05_errors_do_not_disclose_arguments(schema_api):
    # Fixed fake sentinel verifies exception privacy; no credential is used.
    secret = "config_qa-business-secret-sentinel"  # pragma: allowlist secret
    with pytest.raises(schema_api.ToolSchemaError) as caught:
        schema_api.validate_tool_arguments(
            {"type": "object", "properties": {"private": {"type": "integer"}}}, {"private": secret}
        )
    assert secret not in str(caught.value)


# C06/C07: exact matching, complete snapshots and operation binding.
def test_c06_omitted_empty_and_availability(resolver_api):
    assert (
        resolver_api.resolve_tool_review_context(council_id="alpha", tools=None, tool_call=call())
        is None
    )
    empty = resolver_api.resolve_tool_review_context(council_id="alpha", tools=()).to_public_dict()
    assert empty == {
        "council_id": "alpha",
        "tools": [],
        "matched_tool": None,
        "arguments": None,
        "operation": None,
    }
    with pytest.raises(resolver_api.ToolCatalogError):
        resolver_api.resolve_tool_review_context(council_id="alpha", tools=(), tool_call=call())


@pytest.mark.parametrize(
    "name,args",
    [
        ("unknown", {"table": "scratch", "limit": 1}),
        ("Warehouse.delete_rows", {"table": "scratch", "limit": 1}),
        ("warehouse.delete_rows", {"table": "scratch", "limit": 6}),
        ("warehouse.delete_rows", {"table": "scratch"}),
        ("warehouse.delete_rows", {"table": "scratch", "limit": True}),
        ("warehouse.delete_rows", {"table": "prod", "limit": 1}),
    ],
)
def test_c06_unknown_and_invalid_args_raise(resolver_api, name, args):
    tools = settings_with([definition()]).tools
    with pytest.raises(resolver_api.ToolCatalogError):
        resolver_api.resolve_tool_review_context(
            council_id="alpha", tools=tools, tool_call=call(name, args)
        )


def test_c07_definition_and_context_snapshot_no_aliasing(resolver_api):
    original = definition()
    cfg = settings_with([original])
    original["parameters"]["properties"]["limit"]["maximum"] = 500
    original["usage_constraints"].append("UNTRUSTED")
    assert cfg.tools[0].to_public_dict() == definition()
    exported = cfg.tools[0].to_public_dict()
    exported["parameters"]["properties"]["limit"]["maximum"] = 500
    assert cfg.tools[0].to_public_dict() == definition()
    request = call()
    ctx = resolver_api.resolve_tool_review_context(
        council_id="alpha", tools=cfg.tools, tool_call=request
    )
    before = ctx.to_public_dict()
    assert before["tools"] == [definition()]
    assert before["matched_tool"] == definition()
    assert before["arguments"] == request.arguments
    assert before["operation"] == request.to_operation().model_dump(mode="json")
    request.arguments["limit"] = 999
    try:
        cfg.tools[0].parameters["properties"]["limit"]["maximum"] = 1000
    except TypeError:
        pass  # A deeply immutable public model is also a valid implementation.
    fresh = ctx.to_public_dict()
    fresh["tools"][0]["description"] = "edited"
    fresh["matched_tool"]["parameters"].clear()
    fresh["arguments"]["limit"] = 1000
    assert ctx.to_public_dict() == before


def test_c07_literal_config_strings_not_interpolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_QA_LITERAL", "must-not-appear")
    item = definition(
        description="${CONFIG_QA_LITERAL}",
        usage_constraints=["${CONFIG_QA_LITERAL}"],
        parameters={"type": "object", "properties": {"x": {"const": "${CONFIG_QA_LITERAL}"}}},
    )
    target = write_yaml(tmp_path / "literal.yaml", {"council_id": "alpha", "tools": [item]})
    assert exported_tools(load_config(str(target))) == [item]


@pytest.fixture
def dynamic_store(tmp_path):
    target = write_yaml(
        tmp_path / "catalog.yaml",
        {"default_council_id": "alpha", "councils": {"alpha": {"council_id": "alpha"}}},
    )
    return CouncilCatalogStore(target, tmp_path / "profiles")


def visible_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_c07_store_update_show_enable_roundtrip(dynamic_store):
    item = definition()
    value = dynamic_store.update_council_profile("alpha", {"council_id": "alpha", "tools": [item]})
    assert exported_tools(value) == [item]
    assert dynamic_store.show_council("alpha")["tools"] == [item]
    value = dynamic_store.set_enabled("alpha", False)
    assert exported_tools(value) == [item]
    assert exported_tools(dynamic_store.load_catalog().get("alpha")) == [item]
    dynamic_store.update_council_profile("alpha", {"council_id": "alpha", "tools": []})
    assert dynamic_store.show_council("alpha")["tools"] == []


@pytest.mark.parametrize("invalid", [None, [], "bad", "duplicate", "bad-schema"])
def test_c03_invalid_dynamic_update_is_atomic(dynamic_store, tmp_path, invalid):
    dynamic_store.update_council_profile("alpha", {"council_id": "alpha", "tools": [definition()]})
    profile = {"council_id": "alpha", "tools": invalid}
    if invalid == "duplicate":
        profile["tools"] = [definition(), definition()]
    elif invalid == "bad-schema":
        profile["tools"] = [definition(parameters={"type": "object", "$ref": "file:///forbidden"})]
    elif invalid == []:
        # [] itself is valid; the contained definition is deliberately incomplete.
        profile["tools"] = [{}]
    before = visible_bytes(tmp_path)
    with pytest.raises(CONFIG_ERRORS):
        dynamic_store.update_council_profile("alpha", profile)
    assert visible_bytes(tmp_path) == before


def test_c03_duplicate_dynamic_file_does_not_write(dynamic_store, tmp_path):
    source = tmp_path / "candidate.yaml"
    source.write_text("council_id: alpha\ntools: []\ntools: null\n")
    before = visible_bytes(tmp_path)
    with pytest.raises(CONFIG_ERRORS):
        dynamic_store.update_council_from_file("alpha", source)
    assert visible_bytes(tmp_path) == before


def test_c07_create_legacy_control(dynamic_store, monkeypatch):
    # Existing shipped template requires an API key even though this test is offline.
    monkeypatch.setenv("NVIDIA_API_KEY", "config-qa-placeholder-no-network")
    settings_module = importlib.import_module(CouncilSettings.__module__)
    monkeypatch.chdir(Path(settings_module.__file__).resolve().parents[2])
    created = dynamic_store.create_council("beta", enabled=False)
    assert created.council_id == "beta" and created.enabled is False
    assert "tools" not in created.model_dump(mode="json")
    assert "tools" not in dynamic_store.show_council("beta")


def test_c07_loader_rereads_valid_and_invalid_replacements(tmp_path):
    path = tmp_path / "profile.yaml"
    write_yaml(path, {"council_id": "alpha", "tools": [definition()]})
    acquired = load_config(str(path))
    before = acquired.model_dump(mode="json")
    replacement = definition(
        description="Changed revision", parameters={"type": "object", "additionalProperties": False}
    )
    write_yaml(path, {"council_id": "alpha", "tools": [replacement]})
    assert exported_tools(load_config(str(path))) == [replacement]
    write_yaml(path, {"council_id": "alpha", "tools": None})
    with pytest.raises(CONFIG_ERRORS):
        load_config(str(path))
    assert acquired.model_dump(mode="json") == before


def test_c01_legacy_loader_control(tmp_path):
    path = write_yaml(tmp_path / "legacy.yaml", {"council_id": "alpha", "enabled": False})
    actual = load_config(str(path))
    assert actual.council_id == "alpha" and actual.enabled is False
    assert "tools" not in actual.model_dump(mode="json")


def test_c06_legacy_request_operation_control():
    args = {"table": "prod", "limit": 999}
    request = call(arguments=args)
    operation = request.to_operation()
    assert operation.tool_name == request.tool_name
    assert operation.subject == request.subject
    args["limit"] = 1
    assert request.arguments["limit"] == 999
    assert request.to_operation() == operation


@pytest.mark.parametrize(
    "reader", [load_config, load_council_catalog], ids=["single-reader", "catalog-reader"]
)
def test_c03_duplicate_top_level_yaml_rejected(tmp_path, reader):
    path = tmp_path / "duplicate-top.yaml"
    path.write_text("council_id: alpha\ntools: []\ntools: []\n")
    with pytest.raises(CONFIG_ERRORS):
        reader(str(path))


def test_c07_python_dump_omission_and_declared_empty():
    omitted = CouncilSettings(council_id="alpha").model_dump()
    assert "tools" not in omitted
    empty = settings_with([]).model_dump()
    assert "tools" in empty and len(empty["tools"]) == 0
    assert exported_tools(CouncilSettings.model_validate(empty)) == []


def test_c06_same_name_council_catalogs_never_bleed(resolver_api):
    alpha = settings_with(
        [
            definition(
                description="Alpha",
                parameters={
                    "type": "object",
                    "properties": {"table": {"const": "scratch"}},
                    "required": ["table"],
                },
            )
        ]
    ).tools
    beta = settings_with(
        [
            definition(
                description="Beta",
                parameters={
                    "type": "object",
                    "properties": {"table": {"const": "production"}},
                    "required": ["table"],
                },
            )
        ]
    ).tools
    first = resolver_api.resolve_tool_review_context(
        council_id="alpha", tools=alpha, tool_call=call(arguments={"table": "scratch"})
    )
    second = resolver_api.resolve_tool_review_context(
        council_id="beta", tools=beta, tool_call=call(arguments={"table": "production"})
    )
    assert first.to_public_dict()["matched_tool"]["description"] == "Alpha"
    assert second.to_public_dict()["matched_tool"]["description"] == "Beta"
    for cid, tools, args in [
        ("alpha", alpha, {"table": "production"}),
        ("beta", beta, {"table": "scratch"}),
    ]:
        with pytest.raises(resolver_api.ToolCatalogError):
            resolver_api.resolve_tool_review_context(
                council_id=cid, tools=tools, tool_call=call(arguments=args)
            )
    assert first.to_public_dict()["matched_tool"]["description"] == "Alpha"


def test_c05_literal_ref_property_const_enum_are_data(schema_api):
    spec = {
        "type": "object",
        "properties": {
            "$ref": {"type": "string"},
            "record": {
                "const": {"$ref": "https://never.invalid/constant", "$id": "file:///constant"}
            },
            "choice": {"enum": [{"$dynamicRef": "https://never.invalid/enum"}]},
        },
    }
    args = {
        "$ref": "file:///ordinary-property",
        "record": {"$ref": "https://never.invalid/constant", "$id": "file:///constant"},
        "choice": {"$dynamicRef": "https://never.invalid/enum"},
    }
    schema_api.validate_tool_schema(spec)
    schema_api.validate_tool_arguments(spec, args)
