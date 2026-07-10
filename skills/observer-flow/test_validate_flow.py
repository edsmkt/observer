#!/usr/bin/env python3
"""Acceptance tests for the Observer Flow manifest validator."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


passed = failed = 0
HERE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
VALIDATOR = HERE / "scripts" / "validate_flow.py"
EXAMPLE = HERE / "examples" / "website-qualification.flow.json"


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" +
          (f" - {detail}" if detail and not condition else ""))
    if condition:
        passed += 1
    else:
        failed += 1


spec = importlib.util.spec_from_file_location("observer_flow_validate", VALIDATOR)
if spec is None or spec.loader is None:
    raise RuntimeError(f"unable to import {VALIDATOR}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

base = json.loads(EXAMPLE.read_text(encoding="utf-8"))

print(f"Testing Observer Flow validator at {VALIDATOR}\n")

errors = module.validate_manifest(base)
ok("bundled example validates", errors == [], str(errors))
ok("manifest hash is deterministic",
   module.manifest_hash(base) == module.manifest_hash(copy.deepcopy(base)))
ok("one node may own several fields",
   len(base["nodes"][0]["outputs"]) > 1 and not errors)

cycle = copy.deepcopy(base)
cycle["nodes"][0]["needs"] = ["deliver_sheet"]
cycle_errors = module.validate_manifest(cycle)
ok("dependency cycle is rejected",
   any(error.startswith("graph cycle:") for error in cycle_errors), str(cycle_errors))

unknown_dependency = copy.deepcopy(base)
unknown_dependency["nodes"][1]["needs"] = ["missing_node"]
unknown_errors = module.validate_manifest(unknown_dependency)
ok("unknown dependency is rejected",
   any("needs unknown node missing_node" in error for error in unknown_errors),
   str(unknown_errors))

duplicate_output = copy.deepcopy(base)
duplicate_output["nodes"][1]["outputs"].append("social_links")
duplicate_errors = module.validate_manifest(duplicate_output)
ok("two nodes cannot own the same field",
   any("owned by both inspect_website and qualify_saas" in error
       for error in duplicate_errors), str(duplicate_errors))

source_overlap = copy.deepcopy(base)
source_overlap["nodes"][0]["outputs"].append("website")
source_errors = module.validate_manifest(source_overlap)
ok("node output cannot overwrite a source field",
   any("overlaps a source field" in error for error in source_errors),
   str(source_errors))

declared_update = copy.deepcopy(base)
declared_update["source"]["fields"].append("sheet_status")
declared_update["nodes"][3]["updates"] = ["sheet_status"]
update_errors = module.validate_manifest(declared_update)
ok("declared source-field update is accepted", update_errors == [], str(update_errors))

unknown_update = copy.deepcopy(base)
unknown_update["nodes"][3]["updates"] = ["sheet_status"]
unknown_update_errors = module.validate_manifest(unknown_update)
ok("updated field must be declared by the source",
   any("must be a declared source field" in error for error in unknown_update_errors),
   str(unknown_update_errors))

missing_input_edge = copy.deepcopy(base)
missing_input_edge["nodes"][3]["needs"] = ["enrich_email"]
missing_input_edge["nodes"][3]["inputs"].append("unowned_field")
input_errors = module.validate_manifest(missing_input_edge)
ok("input requires a source or ancestor owner",
   any("unowned_field" in error and "outside its source or ancestor" in error
       for error in input_errors), str(input_errors))

missing_ancestor = copy.deepcopy(base)
missing_ancestor["nodes"][3]["needs"] = []
ancestor_errors = module.validate_manifest(missing_ancestor)
ok("input owner must be an ancestor",
   any("node deliver_sheet uses fields outside" in error for error in ancestor_errors),
   str(ancestor_errors))

bad_condition = copy.deepcopy(base)
bad_condition["nodes"][1]["when"] = {
    "all": [{"field": "description", "op": "mystery"}]
}
condition_errors = module.validate_manifest(bad_condition)
ok("condition operator is validated",
   any("must be one of" in error and ".op" in error for error in condition_errors),
   str(condition_errors))

condition_without_edge = copy.deepcopy(base)
condition_without_edge["nodes"][1]["when"] = {
    "all": [{"field": "email", "op": "present"}]
}
condition_edge_errors = module.validate_manifest(condition_without_edge)
ok("condition field requires a source or ancestor owner",
   any("node qualify_saas uses fields outside" in error and "email" in error
       for error in condition_edge_errors), str(condition_edge_errors))

sink_without_destination = copy.deepcopy(base)
sink_without_destination["nodes"][3].pop("side_effect")
sink_errors = module.validate_manifest(sink_without_destination)
ok("sink requires destination confirmation contract",
   any("side_effect must describe" in error for error in sink_errors), str(sink_errors))

side_effect_on_map = copy.deepcopy(base)
side_effect_on_map["nodes"][0]["side_effect"] = {
    "type": "api", "identity": "example", "confirmation": "receipt"
}
side_effect_errors = module.validate_manifest(side_effect_on_map)
ok("external side effect belongs on a sink",
   any("side_effect belongs on a sink node" in error for error in side_effect_errors),
   str(side_effect_errors))

bad_limits = copy.deepcopy(base)
bad_limits["limits"]["sample_rows"] = 0
limit_errors = module.validate_manifest(bad_limits)
ok("sample limit must be positive",
   "limits.sample_rows must be a positive integer" in limit_errors,
   str(limit_errors))

with tempfile.TemporaryDirectory() as tmp:
    manifest = Path(tmp) / "pipeline.flow.json"
    manifest.write_text(json.dumps(base), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-B", str(VALIDATOR), str(manifest), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    ok("validator CLI returns machine-readable success",
       proc.returncode == 0 and payload["valid"] is True and payload["nodes"] == 4,
       proc.stdout + proc.stderr)

    manifest.write_text("{}", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-B", str(VALIDATOR), str(manifest)],
        check=False,
        capture_output=True,
        text=True,
    )
    ok("validator CLI returns nonzero for invalid manifest",
       proc.returncode == 1 and proc.stdout.startswith("INVALID "),
       proc.stdout + proc.stderr)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
