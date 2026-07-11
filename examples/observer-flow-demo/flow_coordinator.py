#!/usr/bin/env python3
"""Run the synthetic row-oriented Observer Flow example."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from demo_runtime import (
    DemoRuntime,
    HERE,
    REPO,
    build_plan_id,
    connect,
    emit,
    graph_event,
    load_node,
    load_source_rows,
    ordered_nodes,
    result_map,
    row_data,
)
from synthetic_data import build_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live synthetic Observer Flow demo")
    parser.add_argument("--flow", default=str(HERE / "pipeline.flow.json"))
    parser.add_argument("--state-dir", default=str(HERE / ".observer"))
    parser.add_argument("--session", default="live-flow-demo")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--provider-rate", type=float, default=12.0)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--full-run", action="store_true")
    return parser.parse_args()


def account_row_status(db: sqlite3.Connection, row_key: str) -> tuple[str, str]:
    results = result_map(db, row_key)
    failures = [result["error"] for result in results.values() if result["status"] == "failed"]
    if failures:
        return "failed", failures[-1]
    data = row_data(db, row_key)
    # Successfully triaged review rows are complete work, not blocked holds.
    if data.get("review_status") == "queued":
        return "complete", ""
    if data.get("sheet_status") or data.get("routing_status"):
        return "complete", ""
    if any(result["status"] == "held" for result in results.values()):
        return "held", ""
    return "running", ""


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RUNGUARD_STATE_DIR"] = str(state_dir)
    os.environ["RUNGUARD_SESSION"] = args.session
    sys.path.insert(0, str(REPO / "skills" / "observer-kit"))
    from runguard import RunPaused, ledger, start_observed_run

    flow = json.loads(Path(args.flow).expanduser().resolve().read_text(encoding="utf-8"))
    rows = build_rows(max(1, min(args.limit, len(build_rows()))))
    nodes = ordered_nodes(flow["nodes"])
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
    runtime = DemoRuntime(
        ledger,
        run,
        db,
        nodes,
        table="accounts",
        hidden_fields={"profile", "index"},
        row_status=account_row_status,
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
            runtime.emit_record(row["domain"], source_node)
            time.sleep(max(0.0, args.delay * 0.08))

        for node in nodes:
            if node.get("mode") not in {"map", "sink"}:
                raise ValueError(
                    f"row demo supports map and sink nodes; {node['id']} uses {node.get('mode')!r}"
                )
            runtime.emit_node(node, len(rows), "running")
            node_run = load_node(node)
            for position, source_row in enumerate(rows, start=1):
                runtime.persist_and_emit_row(
                    source_row["domain"],
                    node,
                    node_run,
                    provider_rate=args.provider_rate,
                    delay=args.delay,
                    position=position,
                    total=len(rows),
                )
                runtime.emit_node(node, len(rows), "running")
            runtime.emit_node(node, len(rows), "complete")

        final_rows = [row_data(db, row["domain"]) for row in rows]
        outcomes = {
            "qualified": sum(row.get("qualification") == "qualified" for row in final_rows),
            "review": sum(row.get("qualification") == "review" for row in final_rows),
            "out_of_scope": sum(row.get("qualification") == "not_software" for row in final_rows),
            "failed": sum(account_row_status(db, row["domain"])[0] == "failed" for row in rows),
        }
        for metric, value in outcomes.items():
            run.count(metric, value)
        spend_total = db.execute(
            "SELECT COALESCE(SUM(spend_units),0) AS total FROM node_results"
        ).fetchone()["total"]
        run.success(
            rows=len(rows),
            synthetic_spend=spend_total,
            plan_id=plan_id,
            execution_mode="dry_run" if run.dry_run else "full_run",
        )
        return 0
    except Exception as exc:
        # RunPaused already closed the attempt with run_paused; do not fail it.
        if isinstance(exc, RunPaused):
            raise
        run.fail(exc)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
