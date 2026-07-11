#!/usr/bin/env python3
"""Run a durable synthetic Observer Flow graph and stream it to Observer Kit."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from synthetic_data import build_rows


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
NODES_DIR = (HERE / "nodes").resolve()
REUSABLE = {"succeeded", "skipped"}
CONDITION_OPS = {
    "eq", "ne", "present", "empty", "contains", "gt", "gte", "lt", "lte", "in"
}
RECIPE_STATUSES = {"candidate", "proven", "superseded"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live synthetic Observer Flow demo")
    parser.add_argument("--flow", default=str(HERE / "pipeline.flow.json"))
    parser.add_argument("--state-dir", default=str(HERE / ".runguard"))
    parser.add_argument("--session", default="live-flow-demo")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--provider-rate", type=float, default=12.0)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--full-run", action="store_true")
    return parser.parse_args()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    existing_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(node_results)")
    }
    required_columns = {
        "run_id", "table_name", "row_key", "node_id", "node_version",
        "input_hash", "status", "result_json", "fields_json", "evidence_json",
        "reason", "error", "spend_units", "duration_ms", "attempt",
        "created_at", "updated_at",
    }
    if existing_columns and not required_columns.issubset(existing_columns):
        # This is synthetic demo state, so an old unhashable cache is safer to
        # discard than to present as reusable evidence.
        with db:
            db.execute("DROP TABLE node_results")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS rows (
          row_key TEXT PRIMARY KEY,
          data_json TEXT NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS node_results (
          run_id TEXT NOT NULL,
          table_name TEXT NOT NULL,
          row_key TEXT NOT NULL,
          node_id TEXT NOT NULL,
          node_version TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          result_json TEXT NOT NULL,
          fields_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          error TEXT NOT NULL,
          spend_units REAL NOT NULL,
          duration_ms INTEGER NOT NULL,
          attempt INTEGER NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (run_id, table_name, row_key, node_id, input_hash)
        );
        CREATE INDEX IF NOT EXISTS node_results_latest
          ON node_results(row_key, node_id, updated_at);
        """
    )
    return db


def condition_value(condition: Optional[dict], row: dict) -> Optional[bool]:
    if not condition:
        return True
    if "all" in condition:
        values = [condition_value(item, row) for item in condition["all"]]
        if any(value is False for value in values):
            return False
        return True if all(value is True for value in values) else None
    if "any" in condition:
        values = [condition_value(item, row) for item in condition["any"]]
        if any(value is True for value in values):
            return True
        return False if all(value is False for value in values) else None
    field, op = condition.get("field"), condition.get("op")
    if field not in row:
        return None
    value, expected = row.get(field), condition.get("value")
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "present":
        return value not in (None, "", [], {})
    if op == "empty":
        return value in (None, "", [], {})
    if op == "contains":
        return str(expected) in str(value)
    try:
        if op == "gt":
            return value > expected
        if op == "gte":
            return value >= expected
        if op == "lt":
            return value < expected
        if op == "lte":
            return value <= expected
        if op == "in":
            return value in expected
    except TypeError as exc:
        raise ValueError(
            f"condition {field!r} {op} has incompatible values: {value!r}, {expected!r}"
        ) from exc
    raise ValueError(f"unsupported demo condition operator: {op}")


def resolve_node_script(node: dict) -> Path:
    """Resolve a demo node while keeping reads and imports inside nodes/."""
    script = (HERE / str(node["script"])).resolve()
    try:
        script.relative_to(NODES_DIR)
    except ValueError as exc:
        raise ValueError(
            f"node {node.get('id', '<unknown>')} script must stay under nodes/: "
            f"{node.get('script')!r}"
        ) from exc
    if script.suffix != ".py" or not script.is_file():
        raise ValueError(
            f"node {node.get('id', '<unknown>')} script is not a Python file: "
            f"{node.get('script')!r}"
        )
    return script


def load_node(node: dict):
    script = resolve_node_script(node)
    module_name = ".".join(script.relative_to(HERE).with_suffix("").parts)
    return importlib.import_module(module_name).run


def recipe_identity(node: dict) -> Optional[dict[str, str]]:
    recipe = node.get("recipe")
    if recipe is None:
        return None
    if not isinstance(recipe, dict):
        raise ValueError(f"node {node.get('id', '<unknown>')} recipe must be an object")
    identity = {key: str(recipe.get(key, "")) for key in ("id", "version", "status")}
    if not identity["id"] or not identity["version"] or identity["status"] not in RECIPE_STATUSES:
        raise ValueError(
            f"node {node.get('id', '<unknown>')} recipe requires id, version, and "
            f"status in {sorted(RECIPE_STATUSES)}"
        )
    return identity


def implementation_identity(node: dict) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "script_sha256": hashlib.sha256(resolve_node_script(node).read_bytes()).hexdigest(),
    }
    recipe = recipe_identity(node)
    if recipe:
        identity["recipe"] = recipe
    return identity


def build_plan_id(flow: dict, **runtime: Any) -> str:
    implementations = {
        node["id"]: implementation_identity(node) for node in flow["nodes"]
    }
    return canonical_hash({
        "flow": flow,
        "implementations": implementations,
        "runtime": runtime,
    })


def ordered_nodes(nodes: list[dict]) -> list[dict]:
    """Return dependency order without treating manifest array position as scheduling."""
    pending = {node["id"]: node for node in nodes}
    ordered: list[dict] = []
    complete: set[str] = set()
    while pending:
        ready = [
            node for node in nodes
            if node["id"] in pending and set(node.get("needs", [])).issubset(complete)
        ]
        if not ready:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"flow dependencies cannot be scheduled: {unresolved}")
        for node in ready:
            ordered.append(node)
            complete.add(node["id"])
            pending.pop(node["id"])
    return ordered


def unit_route(node: dict, row: dict, dependency_results: dict[str, dict]) -> tuple[str, str]:
    """Select execute, skip, or hold from committed fields and dependency results."""
    condition = condition_value(node.get("when"), row)
    if condition is False:
        return "skipped", "branch condition selected another route"
    unusable = [
        dependency
        for dependency in node.get("needs", [])
        if dependency_results.get(dependency, {}).get("status") != "succeeded"
    ]
    if unusable or condition is None:
        return "held", "waiting on a usable upstream result"
    return "execute", ""


def node_input_hash(
    node: dict, row: dict, dry_run: bool,
    dependency_results: Optional[dict[str, dict]] = None,
) -> str:
    """Hash the exact declared inputs and implementation for one node unit."""
    implementation = implementation_identity(node)
    payload = {
        "node_id": node["id"],
        "node_version": str(node.get("version", "1")),
        "implementation": implementation,
        "inputs": {field: row.get(field) for field in node.get("inputs", [])},
        "upstream_results": {
            dependency: {
                "node_version": (dependency_results or {}).get(dependency, {}).get("node_version"),
                "input_hash": (dependency_results or {}).get(dependency, {}).get("input_hash"),
                "status": (dependency_results or {}).get(dependency, {}).get("status"),
            }
            for dependency in node.get("needs", [])
        },
        "config": {
            key: value
            for key, value in node.items()
            if key not in {"label", "script"}
        },
    }
    if node.get("mode") == "sink" or node.get("side_effect"):
        payload["execution_mode"] = "dry_run" if dry_run else "full_run"
    return canonical_hash(payload)


def invoke_node(node_run, node: dict, row: dict, dry_run: bool) -> dict:
    if node.get("mode") == "sink" or node.get("side_effect"):
        return node_run(row, dry_run=dry_run)
    return node_run(row)


def result_map(db: sqlite3.Connection, row_key: str) -> dict[str, dict]:
    rows = db.execute(
        "SELECT * FROM node_results WHERE row_key = ? ORDER BY updated_at, rowid", (row_key,)
    ).fetchall()
    return {row["node_id"]: dict(row) for row in rows}


def matching_result(
    db: sqlite3.Connection, row_key: str, node_id: str, input_hash: str,
) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT rowid AS result_rowid, * FROM node_results
        WHERE row_key = ? AND node_id = ? AND input_hash = ?
        ORDER BY updated_at DESC, rowid DESC LIMIT 1
        """,
        (row_key, node_id, input_hash),
    ).fetchone()


def next_attempt(db: sqlite3.Connection, row_key: str, node_id: str) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(attempt),0) AS attempt FROM node_results WHERE row_key = ? AND node_id = ?",
        (row_key, node_id),
    ).fetchone()
    return int(row["attempt"]) + 1


def load_source_rows(db: sqlite3.Connection, rows: list[dict], key_field: str) -> None:
    """Refresh the active source set while retaining prior projected fields."""
    active_keys = [str(row[key_field]) for row in rows]
    placeholders = ",".join("?" for _ in active_keys)
    now = time.time()
    with db:
        db.execute(f"DELETE FROM rows WHERE row_key NOT IN ({placeholders})", active_keys)
        for source_row in rows:
            row_key = str(source_row[key_field])
            current = row_data(db, row_key)
            current.update(source_row)
            db.execute(
                """
                INSERT INTO rows(row_key,data_json,updated_at) VALUES (?,?,?)
                ON CONFLICT(row_key) DO UPDATE SET
                  data_json=excluded.data_json, updated_at=excluded.updated_at
                """,
                (row_key, json.dumps(current, sort_keys=True), now),
            )


def restore_cached_result(db: sqlite3.Connection, row_key: str, result) -> None:
    """Project a matching historical result and make it the current node state."""
    current = row_data(db, row_key)
    current.update(json.loads(result["fields_json"]))
    now = time.time()
    with db:
        db.execute(
            "UPDATE rows SET data_json = ?, updated_at = ? WHERE row_key = ?",
            (json.dumps(current, sort_keys=True), now, row_key),
        )
        db.execute(
            "UPDATE node_results SET updated_at = ? WHERE rowid = ?",
            (now, result["result_rowid"]),
        )


def row_data(db: sqlite3.Connection, row_key: str) -> dict:
    row = db.execute("SELECT data_json FROM rows WHERE row_key = ?", (row_key,)).fetchone()
    return json.loads(row["data_json"]) if row else {}


def flow_snapshot(db: sqlite3.Connection, row_key: str, nodes: list[dict]) -> dict:
    results = result_map(db, row_key)
    return {
        node["id"]: {
            "status": results.get(node["id"], {}).get("status", "pending"),
            "version": results.get(node["id"], {}).get("node_version", node.get("version", "1")),
            "input_hash": results.get(node["id"], {}).get("input_hash", ""),
            "attempt": results.get(node["id"], {}).get("attempt", 0),
            "reason": results.get(node["id"], {}).get("reason", ""),
            "duration_ms": results.get(node["id"], {}).get("duration_ms", 0),
            "spend_units": results.get(node["id"], {}).get("spend_units", 0),
        }
        for node in nodes
    }


def row_status(db: sqlite3.Connection, row_key: str) -> tuple[str, str]:
    results = result_map(db, row_key)
    failures = [result["error"] for result in results.values() if result["status"] == "failed"]
    if failures:
        return "failed", failures[-1]
    data = row_data(db, row_key)
    if data.get("review_status") == "queued":
        return "held", ""
    if data.get("sheet_status") or data.get("routing_status"):
        return "complete", ""
    if any(result["status"] == "held" for result in results.values()):
        return "held", ""
    return "running", ""


def persist_result(
    db: sqlite3.Connection,
    row_key: str,
    node_id: str,
    status: str,
    fields: dict,
    evidence: dict,
    reason: str,
    error: str,
    spend_units: float,
    duration_ms: int,
    *,
    run_id: str,
    table_name: str,
    node_version: str,
    input_hash: str,
    attempt: int,
) -> None:
    current = row_data(db, row_key)
    current.update(fields)
    now = time.time()
    result = {
        "status": status,
        "fields": fields,
        "evidence": evidence,
        "reason": reason,
        "error": error,
        "spend_units": spend_units,
        "duration_ms": duration_ms,
    }
    with db:
        db.execute(
            """
            INSERT INTO node_results
              (run_id,table_name,row_key,node_id,node_version,input_hash,status,result_json,
               fields_json,evidence_json,reason,error,spend_units,duration_ms,attempt,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,table_name,row_key,node_id,input_hash) DO UPDATE SET
              node_version=excluded.node_version, status=excluded.status,
              result_json=excluded.result_json, fields_json=excluded.fields_json,
              evidence_json=excluded.evidence_json, reason=excluded.reason,
              error=excluded.error, spend_units=excluded.spend_units,
              duration_ms=excluded.duration_ms, attempt=excluded.attempt,
              updated_at=excluded.updated_at
            """,
            (
                run_id,
                table_name,
                row_key,
                node_id,
                node_version,
                input_hash,
                status,
                json.dumps(result, sort_keys=True),
                json.dumps(fields, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                reason,
                error,
                spend_units,
                duration_ms,
                attempt,
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE rows SET data_json = ?, updated_at = ? WHERE row_key = ?",
            (json.dumps(current, sort_keys=True), now, row_key),
        )


def emit(ledger, run, event: str, **fields) -> None:
    ledger(run.scope, event, attempt=run.attempt, dry_run=run.dry_run, **fields)


def emit_record(ledger, run, db: sqlite3.Connection, row_key: str, node: dict, nodes: list[dict]) -> None:
    data = row_data(db, row_key)
    status, error = row_status(db, row_key)
    projection = {
        key: value
        for key, value in data.items()
        if key not in {"profile", "index"}
    }
    emit(
        ledger,
        run,
        "record",
        table="accounts",
        key=row_key,
        current_node=node.get("label", node["id"]),
        flow_status=status,
        flow_json=flow_snapshot(db, row_key, nodes),
        error=error,
        **projection,
    )


def aggregate_node_status(counts: dict[str, int], total: int, requested: str) -> str:
    if requested == "running" or sum(counts.values()) < total:
        return "running"
    if counts.get("failed"):
        return "failed"
    if counts.get("held"):
        return "held"
    return "complete"


def latest_node_results(db: sqlite3.Connection, node_id: str) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in db.execute(
        """
        SELECT node_results.* FROM node_results
        JOIN rows ON rows.row_key = node_results.row_key
        WHERE node_results.node_id = ?
        ORDER BY node_results.updated_at, node_results.rowid
        """,
        (node_id,),
    ):
        latest[row["row_key"]] = dict(row)
    return list(latest.values())


def emit_node(ledger, run, db: sqlite3.Connection, node: dict, total: int, _status: str) -> None:
    counts = {name: 0 for name in ("succeeded", "skipped", "held", "failed", "cached")}
    current_results = latest_node_results(db, node["id"])
    for row in current_results:
        if row["status"] in counts:
            counts[row["status"]] += 1
    completed = sum(counts.values())
    spend = sum(float(row["spend_units"]) for row in current_results)
    emit(
        ledger,
        run,
        "flow_node",
        node_id=node["id"],
        node_label=node.get("label", node["id"]),
        status=aggregate_node_status(counts, total, _status),
        total=total,
        completed=completed,
        spend_units=spend,
        **counts,
    )


def graph_event(flow: dict, plan_id: str, rows_total: int) -> dict:
    nodes = []
    edges = []
    for node in flow["nodes"]:
        nodes.append({
            key: node[key]
            for key in ("id", "label", "version", "kind", "mode", "script", "recipe", "needs", "inputs", "outputs", "when", "edge_label", "batch")
            if key in node
        })
        for dependency in node.get("needs", []):
            edges.append({
                "from": dependency,
                "to": node["id"],
                "label": node.get("edge_label", "then"),
            })
    return {
        "id": flow["graph"]["id"],
        "label": flow["graph"].get("label"),
        "description": flow["graph"].get("description"),
        "version": flow["graph"]["version"],
        "table": flow["source"]["table"],
        "plan_id": plan_id,
        "nodes": nodes,
        "edges": edges,
        "rows_total": rows_total,
    }


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RUNGUARD_STATE_DIR"] = str(state_dir)
    os.environ["RUNGUARD_SESSION"] = args.session
    sys.path.insert(0, str(REPO / "skills" / "observer-kit"))
    from runguard import ledger, start_observed_run, throttle

    flow_path = Path(args.flow).expanduser().resolve()
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    rows = build_rows(max(1, min(args.limit, len(build_rows()))))
    plan_id = build_plan_id(flow, provider_rate=args.provider_rate)
    db = connect(state_dir / "synthetic-account-routing.flow.sqlite3")
    load_source_rows(db, rows, "domain")

    run = start_observed_run(
        "observer-flow-live-demo",
        source=str(HERE / "synthetic_data.py"),
        dry_run=args.dry_run,
        description="Synthetic account routing with visible branches and durable node results",
        todo=len(rows),
        progress_table="accounts",
        destination="Synthetic Review Sheet",
        transform_version="observer-flow-demo-v1",
        script=str(Path(__file__).resolve()),
        config=flow,
        summary_metrics=[
            {"key": "qualified", "label": "qualified"},
            {"key": "review", "label": "needs review"},
            {"key": "out_of_scope", "label": "out of scope"},
            {"key": "failed", "label": "failed"},
        ],
    )
    try:
        emit(
            ledger,
            run,
            "flow_graph",
            graph_id=flow["graph"]["id"],
            plan_id=plan_id,
            rows_total=len(rows),
            graph=graph_event(flow, plan_id, len(rows)),
        )
        emit(ledger, run, "simulation", records=len(rows), fixture="synthetic account export")

        source_node = {"id": "source", "label": "Source loaded"}
        for row in rows:
            emit_record(ledger, run, db, row["domain"], source_node, flow["nodes"])
            time.sleep(max(0.0, args.delay * 0.08))

        for node in ordered_nodes(flow["nodes"]):
            emit_node(ledger, run, db, node, len(rows), "running")
            node_run = load_node(node)
            for row_number, source_row in enumerate(rows, start=1):
                row_key = source_row["domain"]
                current = row_data(db, row_key)
                dependency_results = result_map(db, row_key)
                input_hash = node_input_hash(node, current, run.dry_run, dependency_results)
                prior = matching_result(db, row_key, node["id"], input_hash)
                if prior and prior["status"] in REUSABLE:
                    restore_cached_result(db, row_key, prior)
                    emit(
                        ledger,
                        run,
                        "flow_unit",
                        node_id=node["id"],
                        node_label=node.get("label", node["id"]),
                        table="accounts",
                        key=row_key,
                        status="cached",
                        reason="reused durable node result",
                        node_version=prior["node_version"],
                        input_hash=input_hash,
                        unit_attempt=prior["attempt"],
                        duration_ms=prior["duration_ms"],
                        spend_units=0,
                    )
                    emit_record(ledger, run, db, row_key, node, flow["nodes"])
                    continue

                route, route_reason = unit_route(node, current, dependency_results)

                emit(
                    ledger,
                    run,
                    "flow_unit",
                    node_id=node["id"],
                    node_label=node.get("label", node["id"]),
                    table="accounts",
                    key=row_key,
                    status="running",
                    position=row_number,
                    total=len(rows),
                )
                started = time.monotonic()
                fields: dict[str, Any] = {}
                evidence: dict[str, Any] = {}
                spend_units = 0.0
                error = ""
                attempt = next_attempt(db, row_key, node["id"])
                if route == "skipped":
                    status = "skipped"
                    reason = route_reason
                elif route == "held":
                    status = "held"
                    reason = route_reason
                else:
                    try:
                        spend = node.get("spend")
                        if spend:
                            throttle(f"demo-{spend['provider']}", args.provider_rate)
                        result = invoke_node(node_run, node, current, run.dry_run)
                        fields = dict(result.get("fields", {}))
                        evidence = dict(result.get("evidence", {}))
                        spend_units = float(result.get("spend_units", 0))
                        status = "succeeded"
                        reason = "durable node result committed"
                    except Exception as exc:
                        status = "failed"
                        reason = "node execution failed"
                        error = str(exc)
                duration_ms = max(1, int((time.monotonic() - started) * 1000))
                persist_result(
                    db, row_key, node["id"], status, fields, evidence,
                    reason, error, spend_units, duration_ms,
                    run_id=run.scope,
                    table_name="accounts",
                    node_version=str(node.get("version", "1")),
                    input_hash=input_hash,
                    attempt=attempt,
                )
                emit(
                    ledger,
                    run,
                    "flow_unit",
                    node_id=node["id"],
                    node_label=node.get("label", node["id"]),
                    table="accounts",
                    key=row_key,
                    status=status,
                    reason=reason,
                    error=error,
                    node_version=str(node.get("version", "1")),
                    input_hash=input_hash,
                    unit_attempt=attempt,
                    duration_ms=duration_ms,
                    spend_units=spend_units,
                )
                emit_record(ledger, run, db, row_key, node, flow["nodes"])
                emit_node(ledger, run, db, node, len(rows), "running")
                run.checkpoint(node["id"], row_key)
                run.check_controls(after_record=True)
                time.sleep(max(0.0, args.delay))
            emit_node(ledger, run, db, node, len(rows), "complete")

        final_rows = [row_data(db, row["domain"]) for row in rows]
        outcomes = {
            "qualified": sum(row.get("qualification") == "qualified" for row in final_rows),
            "review": sum(row.get("qualification") == "review" for row in final_rows),
            "out_of_scope": sum(row.get("qualification") == "not_software" for row in final_rows),
            "failed": sum(row_status(db, row["domain"])[0] == "failed" for row in rows),
        }
        for metric, value in outcomes.items():
            run.count(metric, value)
        spend_total = db.execute("SELECT COALESCE(SUM(spend_units),0) AS total FROM node_results").fetchone()["total"]
        run.success(
            rows=len(rows), synthetic_spend=spend_total, plan_id=plan_id,
            execution_mode="dry_run" if run.dry_run else "full_run",
        )
        return 0
    except BaseException as exc:
        run.fail(exc)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
