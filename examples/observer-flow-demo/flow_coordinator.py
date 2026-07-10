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
TERMINAL = {"succeeded", "skipped", "held", "failed", "cached"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live synthetic Observer Flow demo")
    parser.add_argument("--flow", default=str(HERE / "pipeline.flow.json"))
    parser.add_argument("--state-dir", default=str(HERE / ".runguard"))
    parser.add_argument("--session", default="live-flow-demo")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--provider-rate", type=float, default=12.0)
    parser.add_argument("--dry-run", action="store_true", default=True)
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
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS rows (
          row_key TEXT PRIMARY KEY,
          data_json TEXT NOT NULL,
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS node_results (
          row_key TEXT NOT NULL,
          node_id TEXT NOT NULL,
          status TEXT NOT NULL,
          fields_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          error TEXT NOT NULL,
          spend_units REAL NOT NULL,
          duration_ms INTEGER NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (row_key, node_id)
        );
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
    raise ValueError(f"unsupported demo condition operator: {op}")


def load_node(node: dict):
    module_name = Path(node["script"]).stem
    return importlib.import_module(f"nodes.{module_name}").run


def result_map(db: sqlite3.Connection, row_key: str) -> dict[str, dict]:
    rows = db.execute(
        "SELECT * FROM node_results WHERE row_key = ? ORDER BY updated_at", (row_key,)
    ).fetchall()
    return {row["node_id"]: dict(row) for row in rows}


def row_data(db: sqlite3.Connection, row_key: str) -> dict:
    row = db.execute("SELECT data_json FROM rows WHERE row_key = ?", (row_key,)).fetchone()
    return json.loads(row["data_json"]) if row else {}


def flow_snapshot(db: sqlite3.Connection, row_key: str, nodes: list[dict]) -> dict:
    results = result_map(db, row_key)
    return {
        node["id"]: {
            "status": results.get(node["id"], {}).get("status", "pending"),
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
) -> None:
    current = row_data(db, row_key)
    current.update(fields)
    now = time.time()
    with db:
        db.execute(
            """
            INSERT INTO node_results
              (row_key,node_id,status,fields_json,evidence_json,reason,error,spend_units,duration_ms,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(row_key,node_id) DO UPDATE SET
              status=excluded.status, fields_json=excluded.fields_json,
              evidence_json=excluded.evidence_json, reason=excluded.reason,
              error=excluded.error, spend_units=excluded.spend_units,
              duration_ms=excluded.duration_ms, updated_at=excluded.updated_at
            """,
            (
                row_key,
                node_id,
                status,
                json.dumps(fields, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                reason,
                error,
                spend_units,
                duration_ms,
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


def emit_node(ledger, run, db: sqlite3.Connection, node: dict, total: int, status: str) -> None:
    counts = {name: 0 for name in ("succeeded", "skipped", "held", "failed", "cached")}
    for row in db.execute(
        "SELECT status, COUNT(*) AS count FROM node_results WHERE node_id = ? GROUP BY status",
        (node["id"],),
    ):
        if row["status"] in counts:
            counts[row["status"]] = row["count"]
    completed = sum(counts.values())
    spend = db.execute(
        "SELECT COALESCE(SUM(spend_units),0) AS total FROM node_results WHERE node_id = ?",
        (node["id"],),
    ).fetchone()["total"]
    emit(
        ledger,
        run,
        "flow_node",
        node_id=node["id"],
        node_label=node.get("label", node["id"]),
        status=status,
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
            for key in ("id", "label", "version", "kind", "mode", "script", "needs", "inputs", "outputs", "when", "edge_label", "batch")
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
    plan_id = canonical_hash({"flow": flow, "scripts": {
        node["id"]: hashlib.sha256((HERE / node["script"]).read_bytes()).hexdigest()
        for node in flow["nodes"]
    }})
    db = connect(state_dir / "synthetic-account-routing.flow.sqlite3")
    now = time.time()
    with db:
        for row in rows:
            db.execute(
                "INSERT OR IGNORE INTO rows(row_key,data_json,updated_at) VALUES (?,?,?)",
                (row["domain"], json.dumps(row, sort_keys=True), now),
            )

    run = start_observed_run(
        "observer-flow-live-demo",
        source=str(HERE / "synthetic_data.py"),
        dry_run=True,
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

        for node in flow["nodes"]:
            emit_node(ledger, run, db, node, len(rows), "running")
            node_run = load_node(node)
            for row_number, source_row in enumerate(rows, start=1):
                row_key = source_row["domain"]
                prior = db.execute(
                    "SELECT * FROM node_results WHERE row_key = ? AND node_id = ?",
                    (row_key, node["id"]),
                ).fetchone()
                if prior and prior["status"] in TERMINAL:
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
                        duration_ms=prior["duration_ms"],
                        spend_units=0,
                    )
                    emit_record(ledger, run, db, row_key, node, flow["nodes"])
                    continue

                current = row_data(db, row_key)
                condition = condition_value(node.get("when"), current)
                dependency_results = result_map(db, row_key)
                blocked = [
                    dependency
                    for dependency in node.get("needs", [])
                    if dependency_results.get(dependency, {}).get("status") in {"failed", "held"}
                ]

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
                if condition is False:
                    status = "skipped"
                    reason = "branch condition selected another route"
                elif blocked or condition is None:
                    status = "held"
                    reason = "waiting on a usable upstream result"
                else:
                    try:
                        spend = node.get("spend")
                        if spend:
                            throttle(f"demo-{spend['provider']}", args.provider_rate)
                        result = node_run(current)
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
        run.success(rows=len(rows), synthetic_spend=spend_total, plan_id=plan_id)
        return 0
    except BaseException as exc:
        run.fail(exc)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
