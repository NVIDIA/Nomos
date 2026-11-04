# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Component C unit tests; independent acceptance tests are owned separately."""

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nomos.config.settings import CouncilSettings


def definition(**overrides):
    value = {
        "name": "warehouse.delete_rows",
        "description": "Delete scratch rows ${LITERAL}.",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"const": "scratch"},
                "limit": {"type": "integer", "maximum": 5},
            },
            "required": ["table", "limit"],
            "additionalProperties": False,
        },
        "usage_constraints": ["Only delete explicitly authorized scratch rows."],
    }
    return value | overrides


def test_settings_preserve_opt_in_and_omission():
    assert "tools" not in CouncilSettings().model_dump(mode="json")
    assert CouncilSettings(tools=[]).model_dump(mode="json")["tools"] == []
    settings = CouncilSettings(tools=[definition()])
    assert settings.model_dump(mode="json")["tools"] == [definition()]
    assert CouncilSettings.model_validate_json(settings.model_dump_json()).tools == settings.tools
    with pytest.raises(ValidationError):
        CouncilSettings(tools=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "bad name"},
        {"name": "é"},
        {"name": "x\n"},
        {"name": "x" * 129},
        {"description": " "},
        {"description": 3},
        {"description": "x" * 8001},
        {"usage_constraints": []},
        {"usage_constraints": [1]},
        {"usage_constraints": "rules"},
        {"usage_constraints": ["x"] * 17},
        {"usage_constraints": ["x" * 2001]},
        {"parameters": None},
        {"parameters": {"type": "array"}},
        {"parameters": {"type": "object", "required": "x"}},
        {"unexpected": True},
    ],
)
def test_reject_invalid_definitions(overrides):
    with pytest.raises(ValueError):
        CouncilSettings(tools=[definition(**overrides)])


@pytest.mark.parametrize("field", ["name", "description", "parameters", "usage_constraints"])
def test_all_definition_fields_required(field):
    value = definition()
    del value[field]
    with pytest.raises(ValueError):
        CouncilSettings(tools=[value])


def test_catalog_bounds_and_duplicates():
    with pytest.raises(ValueError):
        CouncilSettings(tools=[definition(), definition()])
    with pytest.raises(ValueError):
        CouncilSettings(tools=[definition(name=f"tool{i}") for i in range(33)])
    with pytest.raises(ValueError):
        CouncilSettings(
            tools=[definition(description="x" * 8000, name=f"tool{i}") for i in range(16)]
        )
    assert len(CouncilSettings(tools=[definition(name=f"tool{i}") for i in range(32)]).tools) == 32


def test_schema_resource_bounds():
    from nomos.level_0.tool_schema import ToolSchemaError, validate_tool_schema

    schemas = [
        {"type": "object", "default": "x" * 32000},
        {"type": "object", "default": [None] * 10000},
        {"type": "object", "default": float("inf")},
        {"type": "object", "default": {1: "non-string key"}},
        {"type": "object", "default": {1, 2}},
    ]
    nested = {}
    for _ in range(65):
        nested = {"x": nested}
    schemas.append({"type": "object", "default": nested})
    cyclic = {"type": "object"}
    cyclic["default"] = cyclic
    schemas.append(cyclic)
    for schema in schemas:
        with pytest.raises(ToolSchemaError):
            validate_tool_schema(schema)


def test_definition_and_context_own_deep_snapshots():
    from nomos.level_0.tool_catalog import resolve_tool_review_context
    from nomos.level_0.types.tool_call import ToolCallRequest

    source = definition()
    settings = CouncilSettings(tools=[source])
    source["parameters"]["properties"]["limit"]["maximum"] = 100
    tool = settings.tools[0]
    assert tool.parameters["properties"]["limit"]["maximum"] == 5
    with pytest.raises(TypeError):
        tool.parameters["properties"]["limit"]["maximum"] = 100
    with pytest.raises(TypeError):
        tool.parameters["required"].append("new")
    exported = tool.to_public_dict()
    exported["parameters"]["properties"].clear()
    assert tool.to_public_dict() == definition()
    call = ToolCallRequest(
        tool_name=tool.name, subject="tenant", arguments={"table": "scratch", "limit": 1.0}
    )
    context = resolve_tool_review_context(council_id="A", tools=settings.tools, tool_call=call)
    expected = context.to_public_dict()
    assert expected["matched_tool"] == definition()
    assert context.arguments_json == call.canonical_arguments().decode()
    assert context.operation == call.to_operation()
    expected["tools"].clear()
    expected["arguments"]["limit"] = 9
    call.arguments["limit"] = 4
    assert context.to_public_dict()["arguments"]["limit"] == 1.0
    assert context.to_public_dict()["tools"] == [definition()]
    assert copy.deepcopy(settings).model_dump(mode="json")["tools"] == [definition()]


def test_resolver_exact_matching_and_legacy():
    from nomos.level_0.tool_catalog import ToolCatalogError, resolve_tool_review_context
    from nomos.level_0.types.tool_call import ToolCallRequest

    tools = CouncilSettings(tools=[definition()]).tools
    assert resolve_tool_review_context(council_id="A", tools=None) is None
    available = resolve_tool_review_context(council_id="A", tools=tools).to_public_dict()
    assert available == {
        "council_id": "A",
        "tools": [definition()],
        "matched_tool": None,
        "arguments": None,
        "operation": None,
    }
    for name, args, catalog in [
        ("Warehouse.delete_rows", {"table": "scratch", "limit": 1}, tools),
        ("warehouse.delete_rows", {"table": "production", "limit": 1}, tools),
        ("warehouse.delete_rows", {"table": "scratch", "limit": 9}, tools),
        ("warehouse.delete_rows", {"table": "scratch", "limit": 1}, ()),
    ]:
        with pytest.raises(ToolCatalogError) as error:
            resolve_tool_review_context(
                council_id="A",
                tools=catalog,
                tool_call=ToolCallRequest(tool_name=name, arguments=args, subject="tenant"),
            )
        assert "production" not in str(error.value)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
        {"type": "object", "$vocabulary": {"https://example.test/custom": True}},
        {"type": "object", "$id": "#foo"},
        {"type": "object", "$defs": {"unused": {"$ref": "https://example.test/remote"}}},
        {"type": "object", "$defs": {"unused": {"$dynamicRef": "file:///tmp/schema"}}},
        {"type": "object", "$ref": "relative.json"},
        {"type": "object", "$ref": "#/missing"},
        {"type": "object", "$dynamicRef": "#missing"},
        {"type": "object", "properties": {"x": {"pattern": "["}}},
        {"type": "object", "properties": {"x": {"$schema": "other"}}},
        {"type": "object", "$ref": "#/default", "default": {"$ref": "https://example.test/hidden"}},
    ],
)
def test_offline_schema_restrictions(schema):
    from nomos.level_0.tool_schema import ToolSchemaError, validate_tool_schema

    with pytest.raises(ToolSchemaError):
        validate_tool_schema(schema)


def test_full_schema_semantics_and_annotation_data():
    from nomos.level_0.tool_schema import (
        ToolSchemaError,
        validate_tool_arguments,
        validate_tool_schema,
    )

    schema = {
        "type": "object",
        "$defs": {"small": {"$anchor": "small", "type": "integer", "maximum": 5}},
        "properties": {
            "n": {"$ref": "#small"},
            "mode": {"enum": ["a", "b"]},
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "hint": {"type": "string", "format": "email"},
        },
        "required": ["n", "mode"],
        "dependentRequired": {"items": ["hint"]},
        "allOf": [
            {
                "if": {"properties": {"mode": {"const": "a"}}},
                "then": {"properties": {"n": {"minimum": 2}}},
            }
        ],
        "unevaluatedProperties": False,
        "examples": [{"$ref": "file:///inert", "$id": "https://inert", "$schema": "other"}],
        "default": {"$ref": "remote-inert"},
        "x-annotation": {"$ref": "remote-inert"},
    }
    validate_tool_schema(schema)
    arguments = {"n": 2.0, "mode": "a", "hint": "not-email"}
    before = json.dumps(arguments)
    validate_tool_arguments(schema, arguments)
    assert json.dumps(arguments) == before
    for args in [
        {"n": 8, "mode": "b"},
        {"n": 1, "mode": "a"},
        {"n": 2, "mode": "c"},
        {"n": 2, "mode": "b", "items": ["x"]},
        {"n": 2, "mode": "b", "extra": 1},
    ]:
        with pytest.raises(ToolSchemaError):
            validate_tool_arguments(schema, args)
    recursive = {"type": "object", "$ref": "#"}
    validate_tool_schema(recursive)
    with pytest.raises(ToolSchemaError):
        validate_tool_arguments(recursive, {})


def test_config_inheritance_literals_and_roundtrip(tmp_path: Path, monkeypatch):
    from nomos.config.settings import load_council_catalog

    monkeypatch.setenv("LITERAL", "must not interpolate")
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({"tools": [definition(name="profile.tool")]}))
    path = tmp_path / "catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "shared": {"tools": [definition()]},
                "councils": {
                    "A": {},
                    "B": {"tools": []},
                    "C": {"config_file": "profile.yaml"},
                    "D": {
                        "config_file": "profile.yaml",
                        "tools": [definition(name="override.tool")],
                    },
                },
            }
        )
    )
    catalog = load_council_catalog(str(path))
    assert catalog.get("A").tools[0].to_public_dict() == definition()
    assert catalog.get("B").tools == ()
    assert catalog.get("C").tools[0].name == "profile.tool"
    assert catalog.get("D").tools[0].name == "override.tool"
    path.write_text(yaml.safe_dump(catalog.get("A").model_dump(mode="json")))
    assert load_council_catalog(str(path)).get().tools == catalog.get("A").tools


@pytest.mark.parametrize(
    "text",
    [
        "tools: []\ntools: []\n",
        "tools: [{name: x, name: y}]\n",
        "metadata: &cycle {x: *cycle}\n",
        "tools: !!python/object/apply:builtins.print ['unsafe']\n",
    ],
)
def test_strict_yaml_config_boundaries(tmp_path, text):
    from nomos.config.settings import load_config

    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(str(path))


def test_dynamic_store_preserves_tools_and_rejects_before_write(tmp_path, monkeypatch):
    from nomos.config.council_catalog_store import TEMPLATE_CONFIGS, CouncilCatalogStore

    path = tmp_path / "catalog.yaml"
    path.write_text("councils: {legacy: {}}\n")
    store = CouncilCatalogStore(catalog_path=path, profile_dir=tmp_path / "profiles")
    template = tmp_path / "template.yaml"
    template.write_text(yaml.safe_dump({"tools": [definition()]}))
    monkeypatch.setitem(TEMPLATE_CONFIGS, "test", str(template))
    store.create_council("created", template="test")
    assert store.show_council("created")["tools"] == [definition()]
    store.set_enabled("created", True)
    assert store.show_council("created")["tools"] == [definition()]
    before = {p: p.read_bytes() for p in [path, store.profile_path("created")]}
    with pytest.raises(ValueError):
        store.update_council_profile("created", {"tools": None})
    assert before == {p: p.read_bytes() for p in before}
    bad = tmp_path / "bad.yaml"
    bad.write_text("tools: []\ntools: []\n")
    with pytest.raises(ValueError):
        store.update_council_from_file("created", bad)
    assert before == {p: p.read_bytes() for p in before}
    store.update_council_profile("created", {"tools": []})
    assert store.show_council("created")["tools"] == []
    store.update_council_profile("created", {})
    assert "tools" not in store.show_council("created")


def test_yaml_merge_keeps_valid_overrides_and_rejects_ambiguous_source():
    from nomos.config.strict_yaml import strict_safe_load

    assert strict_safe_load("base: &b {x: 1}\nfirst: &f {<<: *b, x: 2}\nsecond: {<<: *f}\n")[
        "second"
    ] == {"x": 2}
    with pytest.raises(ValueError):
        strict_safe_load("merged: {<<: {x: 1, x: 2}}\n")


@pytest.mark.parametrize("source", ["value: &cycle {<<: *cycle}", "value: &a {<<: &b {<<: *a}}"])
def test_reject_cyclic_aliases_even_if_merge_erases_them(source):
    from nomos.config.strict_yaml import strict_safe_load

    with pytest.raises(ValueError):
        strict_safe_load(source)


@pytest.mark.parametrize(
    "assertion,good,bad",
    [
        ({"oneOf": [{"type": "integer"}, {"const": "exact"}]}, "exact", "other"),
        ({"anyOf": [{"const": 1}, {"const": 2}]}, 2, 3),
        ({"not": {"const": "blocked"}}, "ok", "blocked"),
        ({"type": "array", "prefixItems": [{"const": 1}], "items": False}, [1], [1, 2]),
        ({"type": "array", "contains": {"const": "x"}, "minContains": 1}, ["x"], ["y"]),
        (
            {"type": "object", "properties": {"x": False}, "additionalProperties": False},
            {},
            {"x": 1},
        ),
    ],
)
def test_standard_composition_and_arrays(assertion, good, bad):
    from nomos.level_0.tool_schema import ToolSchemaError, validate_tool_arguments

    schema = {"type": "object", "properties": {"value": assertion}, "required": ["value"]}
    validate_tool_arguments(schema, {"value": good})
    with pytest.raises(ToolSchemaError):
        validate_tool_arguments(schema, {"value": bad})


def test_offline_validation_never_uses_runtime_io(monkeypatch):
    import builtins
    import urllib.request

    from nomos.level_0.tool_schema import (
        ToolSchemaError,
        validate_tool_arguments,
        validate_tool_schema,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("schema validation attempted runtime I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    schema = {
        "type": "object",
        "$defs": {"item": {"type": "integer"}},
        "properties": {"n": {"$ref": "#/$defs/item"}},
    }
    validate_tool_schema(schema)
    validate_tool_arguments(schema, {"n": 1})
    with pytest.raises(ToolSchemaError):
        validate_tool_schema({"type": "object", "$ref": "https://example.test/no-fetch"})


def test_resolver_rejects_non_json_mutation_instead_of_coercing():
    from nomos.level_0.tool_catalog import ToolCatalogError, resolve_tool_review_context
    from nomos.level_0.types.tool_call import ToolCallRequest

    tools = CouncilSettings(tools=[definition(parameters={"type": "object"})]).tools
    call = ToolCallRequest(tool_name="warehouse.delete_rows", arguments={}, subject="tenant")
    call.arguments["mutated"] = (1, 2)
    with pytest.raises(ToolCatalogError):
        resolve_tool_review_context(council_id="A", tools=tools, tool_call=call)


def test_standard_vocabulary_declaration_and_local_dynamic_reference():
    from nomos.level_0.tool_schema import (
        ToolSchemaError,
        validate_tool_arguments,
        validate_tool_schema,
    )

    schema = {
        "type": "object",
        "$vocabulary": {"https://json-schema.org/draft/2020-12/vocab/validation": True},
        "$defs": {"integer": {"$dynamicAnchor": "integer", "type": "integer"}},
        "properties": {"n": {"$dynamicRef": "#integer"}},
    }
    validate_tool_schema(schema)
    validate_tool_arguments(schema, {"n": 1})
    with pytest.raises(ToolSchemaError):
        validate_tool_arguments(schema, {"n": "1"})
