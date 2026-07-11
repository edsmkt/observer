#!/usr/bin/env python3
"""Behavioral acceptance tests for the compact Observer Flow coordinators."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from flow_coordinator import (
    CONDITION_OPS,
    build_plan_id,
    condition_value,
    implementation_identity,
)


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PRIMARY = HERE / "flow_coordinator.py"
BATCH = HERE / "batch_flow_coordinator.py"
passed = failed = 0


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" + (f" - {detail}" if detail and not condition else ""))
    if condition:
        passed += 1
    else:
        failed += 1


def command(script: Path, state: Path, session: str, mode: str, *extra: str):
    return subprocess.run(
        [
            sys.executable, "-B", str(script),
            "--state-dir", str(state),
            "--session", session,
            "--delay", "0",
            "--provider-rate", "1000000",
            mode,
            *extra,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )


def events(state: Path) -> list[dict]:
    ledgers = [
        path for path in state.glob("*.jsonl")
        if path.name not in {"chat.jsonl", "controls.jsonl"}
    ]
    assert len(ledgers) == 1, ledgers
    return [json.loads(line) for line in ledgers[0].read_text(encoding="utf-8").splitlines()]


def attempts(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    starts = [index for index, row in enumerate(rows) if row.get("event") == "run_started"]
    assert len(starts) == 2, starts
    return rows[starts[0]:starts[1]], rows[starts[1]:]


def latest_record_value(rows: list[dict], key: str, field: str):
    values = [
        row[field] for row in rows
        if row.get("event") == "record" and row.get("key") == key and field in row
    ]
    return values[-1] if values else None


print("Testing Observer Flow demo coordinators\n")

condition_cases = {
    "eq": ({"field": "value", "op": "eq", "value": 7}, True),
    "ne": ({"field": "value", "op": "ne", "value": 8}, True),
    "present": ({"field": "value", "op": "present"}, True),
    "empty": ({"field": "blank", "op": "empty"}, True),
    "contains": ({"field": "text", "op": "contains", "value": "flow"}, True),
    "gt": ({"field": "value", "op": "gt", "value": 6}, True),
    "gte": ({"field": "value", "op": "gte", "value": 7}, True),
    "lt": ({"field": "value", "op": "lt", "value": 8}, True),
    "lte": ({"field": "value", "op": "lte", "value": 7}, True),
    "in": ({"field": "value", "op": "in", "value": [6, 7, 8]}, True),
}
condition_row = {"value": 7, "blank": "", "text": "observer flow"}
ok("runtime implements every validated condition operator",
   set(condition_cases) == CONDITION_OPS and all(
       condition_value(predicate, condition_row) is expected
       for predicate, expected in condition_cases.values()
   ))

recipe_flow = json.loads((HERE / "pipeline.flow.json").read_text(encoding="utf-8"))
recipe_flow["nodes"][0]["recipe"] = {
    "id": "inspect-profile", "version": "1", "status": "candidate"
}
candidate_plan = build_plan_id(recipe_flow, provider_rate=12.0)
candidate_identity = implementation_identity(recipe_flow["nodes"][0])
recipe_flow["nodes"][0]["recipe"]["status"] = "proven"
proven_plan = build_plan_id(recipe_flow, provider_rate=12.0)
ok("recipe ID, version, and status participate in plan identity",
   candidate_identity.get("recipe") == {
       "id": "inspect-profile", "version": "1", "status": "candidate"
   } and candidate_plan != proven_plan)

for script in (PRIMARY, BATCH):
    missing_mode = subprocess.run(
        [sys.executable, "-B", str(script)], cwd=REPO,
        capture_output=True, text=True, timeout=10,
    )
    conflicting_mode = subprocess.run(
        [sys.executable, "-B", str(script), "--dry-run", "--full-run"], cwd=REPO,
        capture_output=True, text=True, timeout=10,
    )
    ok(f"{script.stem} requires an explicit execution mode",
       missing_mode.returncode == 2 and conflicting_mode.returncode == 2,
       missing_mode.stderr + conflicting_mode.stderr)

with tempfile.TemporaryDirectory(prefix="observer-flow-script-path-") as tmp:
    state = Path(tmp)
    unsafe_flow = json.loads((HERE / "pipeline.flow.json").read_text(encoding="utf-8"))
    unsafe_flow["nodes"][0]["script"] = "nodes/../../../../../../etc/passwd"
    unsafe_path = state / "unsafe.flow.json"
    unsafe_path.write_text(json.dumps(unsafe_flow), encoding="utf-8")
    unsafe = command(
        PRIMARY, state, "unsafe-script-qa", "--dry-run",
        "--limit", "1", "--flow", str(unsafe_path),
    )
    ok("runtime refuses node script traversal before starting a run",
       unsafe.returncode != 0 and "script must stay under nodes/" in unsafe.stderr,
       unsafe.stdout + unsafe.stderr)

with tempfile.TemporaryDirectory(prefix="observer-flow-demo-") as tmp:
    state = Path(tmp)
    dry = command(PRIMARY, state, "flow-contract-qa", "--dry-run", "--limit", "24")
    full = command(PRIMARY, state, "flow-contract-qa", "--full-run", "--limit", "24")
    ok("primary dry and full modes execute", dry.returncode == 0 and full.returncode == 0,
       dry.stdout + dry.stderr + full.stdout + full.stderr)

    dry_events, full_events = attempts(events(state))
    ok("run ledger records the selected mode",
       dry_events[0].get("dry_run") is True and full_events[0].get("dry_run") is False)
    ok("dry preview advances the same sink field during full-run simulation",
       latest_record_value(dry_events, "northstar-systems.test", "sheet_status") == "planned" and
       latest_record_value(full_events, "northstar-systems.test", "sheet_status") == "simulated append")
    ok("matching upstream work is cached while the mode-bound sink recomputes",
       any(row.get("event") == "flow_unit" and row.get("node_id") == "inspect_profile" and
           row.get("key") == "northstar-systems.test" and row.get("status") == "cached"
           for row in full_events) and
       any(row.get("event") == "flow_unit" and row.get("node_id") == "prepare_sheet" and
           row.get("key") == "northstar-systems.test" and row.get("status") == "succeeded"
           for row in full_events))
    ok("failed units remain retryable rather than becoming cache hits",
       any(row.get("event") == "flow_unit" and row.get("node_id") == "inspect_profile" and
           row.get("key") == "broken-envelope.test" and row.get("status") == "failed"
           for row in full_events) and
       not any(row.get("event") == "flow_unit" and row.get("node_id") == "inspect_profile" and
               row.get("key") == "broken-envelope.test" and row.get("status") == "cached"
               for row in full_events))

    latest_nodes = {
        row["node_id"]: row["status"]
        for row in full_events if row.get("event") == "flow_node"
    }
    ok("terminal node cards expose failed and held aggregate states",
       latest_nodes.get("inspect_profile") == "failed" and
       latest_nodes.get("qualify_account") == "held" and
       latest_nodes.get("find_contact") == "failed" and
       latest_nodes.get("prepare_sheet") == "held",
       str(latest_nodes))

    db = sqlite3.connect(state / "synthetic-account-routing.flow.sqlite3")
    columns = {row[1] for row in db.execute("PRAGMA table_info(node_results)")}
    required = {"run_id", "table_name", "node_version", "input_hash", "attempt", "result_json"}
    sink_hashes = db.execute(
        "SELECT COUNT(DISTINCT input_hash) FROM node_results "
        "WHERE node_id = 'prepare_sheet' AND row_key = 'northstar-systems.test'"
    ).fetchone()[0]
    upstream_hashes = db.execute(
        "SELECT COUNT(DISTINCT input_hash) FROM node_results "
        "WHERE node_id = 'inspect_profile' AND row_key = 'northstar-systems.test'"
    ).fetchone()[0]
    failed_attempt = db.execute(
        "SELECT MAX(attempt) FROM node_results "
        "WHERE node_id = 'inspect_profile' AND row_key = 'broken-envelope.test'"
    ).fetchone()[0]
    db.close()
    ok("SQLite records versioned input hashes, result envelopes, and attempts",
       required <= columns and sink_hashes == 2 and upstream_hashes == 1 and failed_attempt == 2,
       f"columns={sorted(columns)}, sink={sink_hashes}, upstream={upstream_hashes}, attempt={failed_attempt}")

    smaller = command(PRIMARY, state, "flow-contract-qa", "--full-run", "--limit", "3")
    all_events = events(state)
    starts = [index for index, row in enumerate(all_events) if row.get("event") == "run_started"]
    smaller_events = all_events[starts[-1]:]
    smaller_nodes = {
        row["node_id"]: row
        for row in smaller_events if row.get("event") == "flow_node"
    }
    ok("a smaller rerun scopes aggregate cards to its active source rows",
       smaller.returncode == 0 and all(
           row.get("status") == "complete" and row.get("completed") == 3 and row.get("total") == 3
           for row in smaller_nodes.values()
       ), str(smaller_nodes))

    changed_flow = json.loads((HERE / "pipeline.flow.json").read_text(encoding="utf-8"))
    next(node for node in changed_flow["nodes"] if node["id"] == "qualify_account")["version"] = "2"
    changed_path = state / "changed.flow.json"
    changed_path.write_text(json.dumps(changed_flow), encoding="utf-8")
    changed = command(
        PRIMARY, state, "flow-contract-qa", "--dry-run",
        "--limit", "3", "--flow", str(changed_path),
    )
    changed_events = events(state)
    changed_starts = [
        index for index, row in enumerate(changed_events) if row.get("event") == "run_started"
    ]
    changed_attempt = changed_events[changed_starts[-1]:]
    ok("a changed node version invalidates that node and descendants only",
       changed.returncode == 0 and
       any(row.get("event") == "flow_unit" and row.get("node_id") == "inspect_profile" and
           row.get("key") == "northstar-systems.test" and row.get("status") == "cached"
           for row in changed_attempt) and
       any(row.get("event") == "flow_unit" and row.get("node_id") == "qualify_account" and
           row.get("key") == "northstar-systems.test" and row.get("status") == "succeeded"
           for row in changed_attempt) and
       any(row.get("event") == "flow_unit" and row.get("node_id") == "find_contact" and
           row.get("key") == "northstar-systems.test" and row.get("status") == "succeeded"
           for row in changed_attempt) and
       not any(row.get("event") == "flow_unit" and row.get("node_id") in {"qualify_account", "find_contact"} and
               row.get("key") == "northstar-systems.test" and row.get("status") == "cached"
               for row in changed_attempt),
       changed.stdout + changed.stderr)

with tempfile.TemporaryDirectory(prefix="observer-batch-demo-") as tmp:
    state = Path(tmp)
    dry = command(BATCH, state, "batch-contract-qa", "--dry-run",
                  "--limit", "6", "--batch-size", "3")
    full = command(BATCH, state, "batch-contract-qa", "--full-run",
                   "--limit", "6", "--batch-size", "3")
    ok("batch dry and full modes execute", dry.returncode == 0 and full.returncode == 0,
       dry.stdout + dry.stderr + full.stdout + full.stderr)
    dry_events, full_events = attempts(events(state))
    ok("batch responses are reused while the export sink recomputes",
       latest_record_value(dry_events, "alpine-desk.test", "export_status") == "planned" and
       latest_record_value(full_events, "alpine-desk.test", "export_status") == "simulated append" and
       any(row.get("event") == "flow_batch" and row.get("status") == "cached"
           for row in full_events) and
       any(row.get("event") == "flow_unit" and row.get("node_id") == "scrape_homepage" and
           row.get("status") == "cached" for row in full_events))

with tempfile.TemporaryDirectory(prefix="observer-batch-condition-") as tmp:
    state = Path(tmp)
    conditional_flow = json.loads((HERE / "batch_pipeline.flow.json").read_text(encoding="utf-8"))
    batch_node = next(
        node for node in conditional_flow["nodes"] if node["id"] == "batch_label_homepages"
    )
    batch_node["when"] = {
        "all": [{"field": "country", "op": "eq", "value": "DE"}]
    }
    conditional_flow["nodes"] = list(reversed(conditional_flow["nodes"]))
    conditional_path = state / "conditional-reordered.flow.json"
    conditional_path.write_text(json.dumps(conditional_flow), encoding="utf-8")
    conditional = command(
        BATCH, state, "batch-condition-qa", "--dry-run",
        "--limit", "3", "--batch-size", "3", "--flow", str(conditional_path),
    )
    conditional_events = events(state)
    latest_batch_units = {
        row["key"]: row["status"]
        for row in conditional_events
        if row.get("event") == "flow_unit" and row.get("node_id") == "batch_label_homepages"
        and row.get("status") != "running"
    }
    complete_batches = [
        row for row in conditional_events
        if row.get("event") == "flow_batch" and row.get("status") == "complete"
    ]
    ok("batch coordinator schedules by dependencies rather than manifest array order",
       conditional.returncode == 0 and
       latest_record_value(conditional_events, "alpine-desk.test", "export_status") == "planned",
       conditional.stdout + conditional.stderr)
    ok("batch conditions dispatch only matching rows and visibly skip the rest",
       latest_batch_units == {
           "alpine-desk.test": "succeeded",
           "parcel-bloom.test": "skipped",
           "cedar-counsel.test": "skipped",
       } and len(complete_batches) == 1 and complete_batches[0].get("items") == 1,
       f"units={latest_batch_units}, batches={complete_batches}")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
