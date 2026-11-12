# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Safe operator YAML with duplicate mapping keys and alias cycles rejected."""

from typing import Any

import yaml  # type: ignore[import-untyped]


class _StrictSafeLoader(yaml.SafeLoader):
    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._checked_mappings: set[int] = set()

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if id(node) in self._checked_mappings:
            return
        self._checked_mappings.add(id(node))
        # Check explicit entries before flattening. YAML merge inheritance is
        # preserved, including an explicit override of an inherited key.
        keys: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                key = "<<"
            else:
                key = self.construct_object(key_node, deep=False)
            try:
                if key in keys:
                    mark = key_node.start_mark
                    raise ValueError(
                        f"duplicate YAML mapping key at line {mark.line + 1}, "
                        f"column {mark.column + 1}"
                    ) from None
                keys.add(key)
            except TypeError:
                raise ValueError("invalid YAML mapping key") from None
        super().flatten_mapping(node)


def strict_safe_load(stream: Any) -> Any:
    """Load safe YAML, normalizing parser errors and rejecting cyclic aliases."""
    try:
        loader = _StrictSafeLoader(stream)
        try:
            node = loader.get_single_node()
            active: set[int] = set()
            seen: set[int] = set()

            def check_cycles(item: yaml.Node) -> None:
                identity = id(item)
                if identity in active:
                    raise ValueError("cyclic YAML aliases are not allowed")
                if identity in seen:
                    return
                active.add(identity)
                if isinstance(item, yaml.MappingNode):
                    for key, value in item.value:
                        check_cycles(key)
                        check_cycles(value)
                elif isinstance(item, yaml.SequenceNode):
                    for child in item.value:
                        check_cycles(child)
                active.remove(identity)
                seen.add(identity)

            if node is None:
                return None
            # Check the representation graph before merge flattening can erase
            # a cyclic alias. This also avoids arbitrary-object constructors.
            check_cycles(node)
            return loader.construct_document(node)
        finally:
            loader.dispose()
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark if error.problem_mark is not None else error.context_mark
        if mark is not None:
            raise ValueError(
                f"invalid operator YAML at line {mark.line + 1}, column {mark.column + 1}"
            ) from None
        raise ValueError("invalid operator YAML") from None
    except (yaml.YAMLError, RecursionError):
        raise ValueError("invalid operator YAML") from None
