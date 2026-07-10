#!/usr/bin/env python3
"""Validate an Observer Flow JSON manifest with standard-library tooling."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MODES = {"map", "expand", "join", "reduce", "batch", "sink"}
DURABLE_UNITS = {"row", "child", "batch", "aggregate"}
CONDITION_GROUPS = {"all", "any"}
CONDITION_OPS = {
    "eq", "ne", "present", "empty", "contains", "gt", "gte", "lt", "lte", "in"
}


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_text(item) for item in value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _condition_fields(condition: Any, path: str, errors: list[str]) -> set[str]:
    if not isinstance(condition, dict) or not condition:
        errors.append(f"{path} must be a non-empty object")
        return set()

    groups = CONDITION_GROUPS.intersection(condition)
    if groups:
        if len(groups) != 1 or len(condition) != 1:
            errors.append(f"{path} must contain exactly one condition group")
            return set()
        group = next(iter(groups))
        children = condition[group]
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.{group} must be a non-empty list")
            return set()
        fields: set[str] = set()
        for index, child in enumerate(children):
            fields.update(_condition_fields(child, f"{path}.{group}[{index}]", errors))
        return fields

    field = condition.get("field")
    op = condition.get("op")
    if not _is_text(field):
        errors.append(f"{path}.field must be a non-empty string")
    if op not in CONDITION_OPS:
        errors.append(f"{path}.op must be one of {sorted(CONDITION_OPS)}")
    allowed = {"field", "op", "value"}
    extra = sorted(set(condition) - allowed)
    if extra:
        errors.append(f"{path} has unsupported keys: {', '.join(extra)}")
    if op in {"eq", "ne", "contains", "gt", "gte", "lt", "lte", "in"} and "value" not in condition:
        errors.append(f"{path}.value is required for op={op}")
    return {field} if _is_text(field) else set()


def _ancestors(node_id: str, needs_by_id: dict[str, list[str]], memo: dict[str, set[str]]) -> set[str]:
    if node_id in memo:
        return memo[node_id]
    result: set[str] = set()
    for dependency in needs_by_id.get(node_id, []):
        if dependency not in needs_by_id:
            continue
        result.add(dependency)
        result.update(_ancestors(dependency, needs_by_id, memo))
    memo[node_id] = result
    return result


def validate_manifest(document: Any) -> list[str]:
    """Return human-readable structural errors for a parsed flow manifest."""
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["manifest root must be an object"]

    if document.get("format_version") != 1:
        errors.append("format_version must equal 1")

    graph = document.get("graph")
    if not isinstance(graph, dict):
        errors.append("graph must be an object")
    else:
        if not _is_text(graph.get("id")) or not ID_RE.match(str(graph.get("id", ""))):
            errors.append("graph.id must use lowercase letters, digits, hyphens, or underscores")
        if not _is_text(graph.get("version")):
            errors.append("graph.version must be a non-empty string")

    source = document.get("source")
    source_fields: set[str] = set()
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        for field in ("identity", "snapshot", "table", "key"):
            if not _is_text(source.get(field)):
                errors.append(f"source.{field} must be a non-empty string")
        fields = source.get("fields")
        if not _is_string_list(fields) or not fields:
            errors.append("source.fields must be a non-empty list of strings")
        else:
            source_fields = set(fields)
            if len(source_fields) != len(fields):
                errors.append("source.fields must be unique")
            if source.get("key") not in source_fields:
                errors.append("source.key must appear in source.fields")

    state = document.get("state")
    if not isinstance(state, dict) or not _is_text(state.get("store")):
        errors.append("state.store must be a non-empty string")

    nodes = document.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes must be a non-empty list")
        return errors

    node_by_id: dict[str, dict[str, Any]] = {}
    needs_by_id: dict[str, list[str]] = {}
    output_owner: dict[str, str] = {}
    condition_fields_by_id: dict[str, set[str]] = {}

    for index, raw_node in enumerate(nodes):
        path = f"nodes[{index}]"
        if not isinstance(raw_node, dict):
            errors.append(f"{path} must be an object")
            continue

        node_id = raw_node.get("id")
        if not _is_text(node_id) or not ID_RE.match(str(node_id or "")):
            errors.append(f"{path}.id must use lowercase letters, digits, hyphens, or underscores")
            continue
        if node_id in node_by_id:
            errors.append(f"duplicate node id: {node_id}")
            continue
        node_by_id[node_id] = raw_node

        if not _is_text(raw_node.get("version")):
            errors.append(f"{path}.version must be a non-empty string")
        if raw_node.get("mode") not in MODES:
            errors.append(f"{path}.mode must be one of {sorted(MODES)}")
        if not _is_text(raw_node.get("script")):
            errors.append(f"{path}.script must be a non-empty string")

        for list_field in ("needs", "inputs", "outputs"):
            value = raw_node.get(list_field)
            if not _is_string_list(value):
                errors.append(f"{path}.{list_field} must be a list of strings")
            elif len(set(value)) != len(value):
                errors.append(f"{path}.{list_field} must be unique")

        needs = raw_node.get("needs") if _is_string_list(raw_node.get("needs")) else []
        needs_by_id[node_id] = list(needs)

        outputs = raw_node.get("outputs") if _is_string_list(raw_node.get("outputs")) else []
        updates_value = raw_node.get("updates", [])
        if not _is_string_list(updates_value):
            errors.append(f"{path}.updates must be a list of strings")
            updates: set[str] = set()
        else:
            updates = set(updates_value)
            if len(updates) != len(updates_value):
                errors.append(f"{path}.updates must be unique")
            for update in sorted(updates - set(outputs)):
                errors.append(f"{path}.updates field {update!r} must appear in outputs")
            for update in sorted(updates - source_fields):
                errors.append(f"{path}.updates field {update!r} must be a declared source field")
        if not outputs:
            errors.append(f"{path}.outputs must contain at least one field")
        for output in outputs:
            if output in source_fields and output not in updates:
                errors.append(f"output field {output!r} overlaps a source field")
            prior_owner = output_owner.get(output)
            if prior_owner:
                errors.append(f"output field {output!r} is owned by both {prior_owner} and {node_id}")
            else:
                output_owner[output] = node_id

        if raw_node.get("durable_unit") not in DURABLE_UNITS:
            errors.append(f"{path}.durable_unit must be one of {sorted(DURABLE_UNITS)}")
        for integer_field in ("concurrency", "max_attempts"):
            if not _positive_int(raw_node.get(integer_field)):
                errors.append(f"{path}.{integer_field} must be a positive integer")

        if "when" in raw_node:
            condition_fields_by_id[node_id] = _condition_fields(raw_node["when"], f"{path}.when", errors)
        else:
            condition_fields_by_id[node_id] = set()

        spend = raw_node.get("spend")
        if spend is not None:
            if not isinstance(spend, dict):
                errors.append(f"{path}.spend must be an object")
            else:
                if not _is_text(spend.get("provider")):
                    errors.append(f"{path}.spend.provider must be a non-empty string")
                for integer_field in ("units_per_call", "max_units"):
                    if not _positive_int(spend.get(integer_field)):
                        errors.append(f"{path}.spend.{integer_field} must be a positive integer")

        side_effect = raw_node.get("side_effect")
        if raw_node.get("mode") == "sink":
            if not isinstance(side_effect, dict):
                errors.append(f"{path}.side_effect must describe the sink destination")
            else:
                for field in ("type", "identity", "confirmation"):
                    if not _is_text(side_effect.get(field)):
                        errors.append(f"{path}.side_effect.{field} must be a non-empty string")
        elif side_effect is not None:
            errors.append(f"{path}.side_effect belongs on a sink node")

    known_ids = set(node_by_id)
    for node_id, needs in needs_by_id.items():
        for dependency in needs:
            if dependency == node_id:
                errors.append(f"node {node_id} depends on itself")
            elif dependency not in known_ids:
                errors.append(f"node {node_id} needs unknown node {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str, trail: list[str]) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            cycle_start = trail.index(node_id) if node_id in trail else 0
            cycle = trail[cycle_start:] + [node_id]
            errors.append(f"graph cycle: {' -> '.join(cycle)}")
            return
        visiting.add(node_id)
        for dependency in needs_by_id.get(node_id, []):
            if dependency in known_ids:
                visit(dependency, trail + [node_id])
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in known_ids:
        visit(node_id, [])

    if not any(error.startswith("graph cycle:") for error in errors):
        memo: dict[str, set[str]] = {}
        for node_id, node in node_by_id.items():
            available = set(source_fields)
            for ancestor in _ancestors(node_id, needs_by_id, memo):
                ancestor_node = node_by_id[ancestor]
                available.update(ancestor_node.get("outputs", []))
            requested = set(node.get("inputs", [])) | condition_fields_by_id.get(node_id, set())
            missing = sorted(requested - available)
            if missing:
                errors.append(
                    f"node {node_id} uses fields outside its source or ancestor dependencies: "
                    f"{', '.join(missing)}"
                )

    limits = document.get("limits")
    if not isinstance(limits, dict):
        errors.append("limits must be an object")
    else:
        if not _positive_int(limits.get("sample_rows")):
            errors.append("limits.sample_rows must be a positive integer")
        if not _positive_int(limits.get("max_in_flight")):
            errors.append("limits.max_in_flight must be a positive integer")
        for optional_limit in ("max_provider_calls", "max_writes"):
            if optional_limit in limits and not _positive_int(limits[optional_limit]):
                errors.append(f"limits.{optional_limit} must be a positive integer")

    return errors


def manifest_hash(document: Any) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an Observer Flow JSON manifest and print its structural hash."
    )
    parser.add_argument("manifest", help="path to pipeline.flow.json")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)

    path = Path(args.manifest).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result = {"valid": False, "path": str(path), "errors": [str(exc)]}
        print(json.dumps(result, indent=2) if args.json else f"INVALID {path}\n  - {exc}")
        return 1

    errors = validate_manifest(document)
    result = {
        "valid": not errors,
        "path": str(path),
        "nodes": len(document.get("nodes", [])) if isinstance(document, dict) else 0,
        "manifest_sha256": manifest_hash(document),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif errors:
        print(f"INVALID {path}")
        for error in errors:
            print(f"  - {error}")
    else:
        print(
            f"VALID {path} nodes={result['nodes']} "
            f"manifest_sha256={result['manifest_sha256']}"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
