#!/usr/bin/env python3
"""Run the mixed single-request and batch-request Observer Flow demo."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from batch_synthetic_data import build_rows
from flow_coordinator import (
    TERMINAL,
    canonical_hash,
    connect,
    emit,
    flow_snapshot,
    graph_event,
    persist_result,
    result_map,
    row_data,
)


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TABLE = "websites"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic single + batch Observer Flow demo")
    parser.add_argument("--flow", default=str(HERE / "batch_pipeline.flow.json"))
    parser.add_argument("--state-dir", default=str(HERE / ".runguard"))
    parser.add_argument("--session", default="batch-flow-demo")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--state-name", default="synthetic-homepage-batch-labeling")
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--provider-rate", type=float, default=20.0)
    return parser.parse_args()


def connect_state(path: Path):
    db = connect(path)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS batch_calls (
          batch_id TEXT PRIMARY KEY,
          node_id TEXT NOT NULL,
          position INTEGER NOT NULL,
          total_batches INTEGER NOT NULL,
          status TEXT NOT NULL,
          row_keys_json TEXT NOT NULL,
          request_id TEXT NOT NULL,
          response_json TEXT NOT NULL,
          spend_units REAL NOT NULL,
          individual_equivalent_units REAL NOT NULL,
          error TEXT NOT NULL,
          updated_at REAL NOT NULL
        );
        """
    )
    return db


def load_node(node: dict):
    module_name = Path(node["script"]).stem
    return importlib.import_module(f"nodes.{module_name}").run


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def row_status(db, row_key: str) -> tuple[str, str]:
    results = result_map(db, row_key)
    failures = [result["error"] for result in results.values() if result["status"] == "failed"]
    if failures:
        return "failed", failures[-1]
    if row_data(db, row_key).get("export_status") == "ready":
        return "complete", ""
    if any(result["status"] == "held" for result in results.values()):
        return "held", ""
    return "running", ""


def emit_record(ledger, run, db, row_key: str, node: dict, nodes: list[dict]) -> None:
    data = row_data(db, row_key)
    status, error = row_status(db, row_key)
    projection = {
        key: value
        for key, value in data.items()
        if key not in {"fixture_kind", "index"}
    }
    emit(
        ledger,
        run,
        "record",
        table=TABLE,
        key=row_key,
        current_node=node.get("label", node["id"]),
        flow_status=status,
        flow_json=flow_snapshot(db, row_key, nodes),
        error=error,
        **projection,
    )


def node_counts(db, node_id: str) -> dict[str, int]:
    counts = {name: 0 for name in ("succeeded", "skipped", "held", "failed", "cached")}
    for row in db.execute(
        "SELECT status, COUNT(*) AS count FROM node_results WHERE node_id = ? GROUP BY status",
        (node_id,),
    ):
        if row["status"] in counts:
            counts[row["status"]] = row["count"]
    return counts


def emit_node(ledger, run, db, node: dict, total: int, status: str, **extra: Any) -> None:
    counts = node_counts(db, node["id"])
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
        completed=sum(counts.values()),
        spend_units=round(float(spend), 4),
        **counts,
        **extra,
    )


def emit_terminal(
    ledger,
    run,
    db,
    row_key: str,
    node: dict,
    nodes: list[dict],
    *,
    status: str,
    fields: dict | None = None,
    evidence: dict | None = None,
    reason: str,
    error: str = "",
    spend_units: float = 0,
    duration_ms: int = 1,
    batch_id: str = "",
) -> None:
    persist_result(
        db,
        row_key,
        node["id"],
        status,
        fields or {},
        evidence or {},
        reason,
        error,
        spend_units,
        duration_ms,
    )
    event = {
        "node_id": node["id"],
        "node_label": node.get("label", node["id"]),
        "table": TABLE,
        "key": row_key,
        "status": status,
        "reason": reason,
        "error": error,
        "duration_ms": duration_ms,
        "spend_units": spend_units,
    }
    if batch_id:
        event["batch_id"] = batch_id
    emit(ledger, run, "flow_unit", **event)
    emit_record(ledger, run, db, row_key, node, nodes)


def execute_map_unit(
    ledger,
    run,
    db,
    row_key: str,
    node: dict,
    nodes: list[dict],
    node_run,
    *,
    provider_rate: float,
    delay: float,
) -> str:
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
            table=TABLE,
            key=row_key,
            status="cached",
            reason="reused durable node result",
            duration_ms=prior["duration_ms"],
            spend_units=0,
        )
        emit_record(ledger, run, db, row_key, node, nodes)
        return prior["status"]

    dependencies = result_map(db, row_key)
    blocked = [
        dependency
        for dependency in node.get("needs", [])
        if dependencies.get(dependency, {}).get("status") in {"failed", "held"}
    ]
    emit(
        ledger,
        run,
        "flow_unit",
        node_id=node["id"],
        node_label=node.get("label", node["id"]),
        table=TABLE,
        key=row_key,
        status="running",
    )
    started = time.monotonic()
    if blocked:
        status = "held"
        fields: dict[str, Any] = {}
        evidence: dict[str, Any] = {}
        reason = "waiting on a usable upstream result"
        error = ""
        spend_units = 0.0
    else:
        try:
            spend = node.get("spend")
            if spend:
                from runguard import throttle
                throttle(f"demo-{spend['provider']}", provider_rate)
            result = node_run(row_data(db, row_key))
            status = "succeeded"
            fields = dict(result.get("fields", {}))
            evidence = dict(result.get("evidence", {}))
            reason = "durable node result committed"
            error = ""
            spend_units = float(result.get("spend_units", 0))
        except Exception as exc:
            status = "failed"
            fields = {}
            evidence = {}
            reason = "node execution failed"
            error = str(exc)
            spend_units = float(node.get("spend", {}).get("units_per_call", 0))
    duration_ms = max(1, int((time.monotonic() - started) * 1000))
    emit_terminal(
        ledger,
        run,
        db,
        row_key,
        node,
        nodes,
        status=status,
        fields=fields,
        evidence=evidence,
        reason=reason,
        error=error,
        spend_units=spend_units,
        duration_ms=duration_ms,
    )
    run.checkpoint(node["id"], row_key)
    run.check_controls(after_record=True)
    time.sleep(max(0.0, delay))
    return status


def save_batch_response(db, batch_id: str, response: dict) -> None:
    with db:
        db.execute(
            """
            UPDATE batch_calls SET status = 'returned', request_id = ?, response_json = ?,
              spend_units = ?, individual_equivalent_units = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (
                response["request_id"],
                json.dumps(response, sort_keys=True),
                float(response["spend_units"]),
                float(response["individual_equivalent_units"]),
                time.time(),
                batch_id,
            ),
        )


def complete_batch(db, batch_id: str) -> None:
    with db:
        db.execute(
            "UPDATE batch_calls SET status = 'complete', updated_at = ? WHERE batch_id = ?",
            (time.time(), batch_id),
        )


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
    nodes = flow["nodes"]
    scripts = {
        node["id"]: hashlib.sha256((HERE / node["script"]).read_bytes()).hexdigest()
        for node in nodes
    }
    plan_id = canonical_hash({"flow": flow, "scripts": scripts, "batch_size": args.batch_size})
    state_name = "".join(char if char.isalnum() or char in "-_" else "-" for char in args.state_name)
    db = connect_state(state_dir / f"{state_name}.flow.sqlite3")
    now = time.time()
    with db:
        for row in rows:
            db.execute(
                "INSERT OR IGNORE INTO rows(row_key,data_json,updated_at) VALUES (?,?,?)",
                (row["domain"], json.dumps(row, sort_keys=True), now),
            )

    run = start_observed_run(
        "observer-flow-batch-demo",
        source=str(HERE / "batch_synthetic_data.py"),
        dry_run=True,
        description="Individual homepage scraping followed by discounted batch labeling",
        todo=len(rows),
        progress_table=TABLE,
        destination="Synthetic Labelled Website Table",
        transform_version="observer-flow-batch-demo-v1",
        script=str(Path(__file__).resolve()),
        config={**flow, "runtime_batch_size": args.batch_size},
        summary_metrics=[
            {"key": "scraped", "label": "homepages scraped"},
            {"key": "labelled", "label": "rows labelled"},
            {"key": "batch_calls", "label": "batch calls"},
            {"key": "units_saved", "label": "label units saved"},
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
        emit(ledger, run, "simulation", records=len(rows), fixture="synthetic website list")

        source_node = {"id": "source", "label": "Source loaded"}
        for row in rows:
            emit_record(ledger, run, db, row["domain"], source_node, nodes)
            time.sleep(max(0.0, args.delay * 0.04))

        scrape_node, batch_node, export_node = nodes
        scrape_run = load_node(scrape_node)
        export_run = load_node(export_node)

        emit_node(ledger, run, db, scrape_node, len(rows), "running", provider_calls=0)
        for position, row in enumerate(rows, start=1):
            execute_map_unit(
                ledger,
                run,
                db,
                row["domain"],
                scrape_node,
                nodes,
                scrape_run,
                provider_rate=args.provider_rate,
                delay=args.delay * 0.45,
            )
            calls = sum(node_counts(db, scrape_node["id"])[name] for name in ("succeeded", "failed"))
            emit_node(
                ledger,
                run,
                db,
                scrape_node,
                len(rows),
                "running",
                provider_calls=calls,
                position=position,
            )
        emit_node(ledger, run, db, scrape_node, len(rows), "complete", provider_calls=len(rows))

        blocked_keys: list[str] = []
        ready_keys: list[str] = []
        for row in rows:
            key = row["domain"]
            status = result_map(db, key).get(scrape_node["id"], {}).get("status")
            (ready_keys if status == "succeeded" else blocked_keys).append(key)

        batch_size = max(1, min(args.batch_size, int(batch_node.get("batch", {}).get("max_items", args.batch_size))))
        groups = chunks(ready_keys, batch_size)
        total_batches = len(groups)
        emit_node(
            ledger,
            run,
            db,
            batch_node,
            len(rows),
            "running",
            batches_completed=0,
            batches_total=total_batches,
            provider_calls=0,
        )

        for key in blocked_keys:
            emit_terminal(
                ledger,
                run,
                db,
                key,
                batch_node,
                nodes,
                status="held",
                reason="waiting on a usable homepage",
            )
            emit_terminal(
                ledger,
                run,
                db,
                key,
                export_node,
                nodes,
                status="held",
                reason="waiting on a usable batch label",
            )

        batch_run = load_node(batch_node)
        for position, keys in enumerate(groups, start=1):
            digest = canonical_hash(keys).split(":", 1)[1][:7]
            batch_id = f"label-{position:02d}-{digest}"
            with db:
                db.execute(
                    """
                    INSERT OR IGNORE INTO batch_calls
                      (batch_id,node_id,position,total_batches,status,row_keys_json,request_id,
                       response_json,spend_units,individual_equivalent_units,error,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id,
                        batch_node["id"],
                        position,
                        total_batches,
                        "planned",
                        json.dumps(keys),
                        "",
                        "",
                        0,
                        len(keys),
                        "",
                        time.time(),
                    ),
                )
            batch_record = db.execute(
                "SELECT * FROM batch_calls WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            prior_results = {
                key: result_map(db, key).get(batch_node["id"], {})
                for key in keys
            }
            fully_cached = (
                batch_record["status"] == "complete"
                and all(prior_results[key].get("status") in TERMINAL for key in keys)
            )
            if fully_cached:
                original_spend = float(batch_record["spend_units"])
                individual = float(batch_record["individual_equivalent_units"])
                emit(
                    ledger,
                    run,
                    "flow_batch",
                    node_id=batch_node["id"],
                    node_label=batch_node["label"],
                    batch_id=batch_id,
                    position=position,
                    total_batches=total_batches,
                    status="cached",
                    items=len(keys),
                    spend_units=0,
                    original_spend_units=original_spend,
                    saved_units=round(individual - original_spend, 4),
                    reused_response=True,
                    provider_called=False,
                )
                for key in keys:
                    emit(
                        ledger,
                        run,
                        "flow_unit",
                        node_id=batch_node["id"],
                        node_label=batch_node["label"],
                        table=TABLE,
                        key=key,
                        status="cached",
                        reason="reused durable batch member result",
                        batch_id=batch_id,
                        spend_units=0,
                    )
                    emit_record(ledger, run, db, key, batch_node, nodes)
                    execute_map_unit(
                        ledger,
                        run,
                        db,
                        key,
                        export_node,
                        nodes,
                        export_run,
                        provider_rate=args.provider_rate,
                        delay=0,
                    )
                emit_node(
                    ledger,
                    run,
                    db,
                    batch_node,
                    len(rows),
                    "running",
                    batches_completed=position,
                    batches_total=total_batches,
                    provider_calls=position,
                )
                emit_node(ledger, run, db, export_node, len(rows), "running", provider_calls=0)
                run.checkpoint(batch_node["id"], batch_id)
                run.check_controls(after_record=True)
                continue
            emit(
                ledger,
                run,
                "flow_batch",
                node_id=batch_node["id"],
                node_label=batch_node["label"],
                batch_id=batch_id,
                position=position,
                total_batches=total_batches,
                status="running",
                items=len(keys),
                row_keys=keys,
            )
            for key in keys:
                if prior_results[key].get("status") in TERMINAL:
                    emit(
                        ledger,
                        run,
                        "flow_unit",
                        node_id=batch_node["id"],
                        node_label=batch_node["label"],
                        table=TABLE,
                        key=key,
                        status="cached",
                        reason="reused durable batch member result",
                        batch_id=batch_id,
                    )
                else:
                    emit(
                        ledger,
                        run,
                        "flow_unit",
                        node_id=batch_node["id"],
                        node_label=batch_node["label"],
                        table=TABLE,
                        key=key,
                        status="running",
                        batch_id=batch_id,
                    )

            stored = db.execute(
                "SELECT response_json FROM batch_calls WHERE batch_id = ?", (batch_id,)
            ).fetchone()["response_json"]
            reused_response = bool(stored)
            if stored:
                response = json.loads(stored)
            else:
                throttle("demo-synthetic_batch_label_api", args.provider_rate)
                time.sleep(max(0.0, args.delay * 4.0))
                response = batch_run([row_data(db, key) for key in keys], batch_id=batch_id)
                save_batch_response(db, batch_id, response)

            batch_started = time.monotonic()
            succeeded = failed = 0
            for key in keys:
                if prior_results[key].get("status") in TERMINAL:
                    succeeded += prior_results[key].get("status") == "succeeded"
                    failed += prior_results[key].get("status") == "failed"
                    emit_record(ledger, run, db, key, batch_node, nodes)
                    continue
                member = response.get("results", {}).get(key)
                if member is None:
                    member = {
                        "status": "failed",
                        "fields": {"label_batch_id": batch_id, "label_status": "response missing"},
                        "evidence": {"request_id": response.get("request_id", "")},
                        "error": "Batch response omitted this member",
                        "spend_units": 0,
                    }
                status = member.get("status", "succeeded")
                succeeded += status == "succeeded"
                failed += status == "failed"
                emit_terminal(
                    ledger,
                    run,
                    db,
                    key,
                    batch_node,
                    nodes,
                    status=status,
                    fields=dict(member.get("fields", {})),
                    evidence=dict(member.get("evidence", {})),
                    reason="batch member result committed" if status == "succeeded" else "batch member failed",
                    error=str(member.get("error", "")),
                    spend_units=float(member.get("spend_units", 0)),
                    duration_ms=max(1, int((time.monotonic() - batch_started) * 1000)),
                    batch_id=batch_id,
                )
            complete_batch(db, batch_id)
            saved = round(float(response["individual_equivalent_units"]) - float(response["spend_units"]), 4)
            emit(
                ledger,
                run,
                "flow_batch",
                node_id=batch_node["id"],
                node_label=batch_node["label"],
                batch_id=batch_id,
                position=position,
                total_batches=total_batches,
                status="complete",
                items=len(keys),
                succeeded=succeeded,
                failed=failed,
                request_id=response["request_id"],
                spend_units=float(response["spend_units"]),
                individual_equivalent_units=float(response["individual_equivalent_units"]),
                saved_units=saved,
                reused_response=reused_response,
                provider_called=not reused_response,
            )
            emit_node(
                ledger,
                run,
                db,
                batch_node,
                len(rows),
                "running",
                batches_completed=position,
                batches_total=total_batches,
                provider_calls=position,
            )

            for key in keys:
                execute_map_unit(
                    ledger,
                    run,
                    db,
                    key,
                    export_node,
                    nodes,
                    export_run,
                    provider_rate=args.provider_rate,
                    delay=args.delay * 0.08,
                )
            emit_node(ledger, run, db, export_node, len(rows), "running", provider_calls=0)
            run.checkpoint(batch_node["id"], batch_id)
            run.check_controls(after_record=True)

        emit_node(
            ledger,
            run,
            db,
            batch_node,
            len(rows),
            "complete",
            batches_completed=total_batches,
            batches_total=total_batches,
            provider_calls=total_batches,
        )
        emit_node(ledger, run, db, export_node, len(rows), "complete", provider_calls=0)

        final_rows = [row_data(db, row["domain"]) for row in rows]
        label_counts = Counter(row.get("label") for row in final_rows if row.get("label"))
        scraped = sum(row.get("scrape_status") == "scraped" for row in final_rows)
        labelled = sum(row.get("label_status") == "labelled" for row in final_rows)
        batch_totals = db.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(spend_units),0) AS spent,
                   COALESCE(SUM(individual_equivalent_units),0) AS individual
            FROM batch_calls WHERE status = 'complete'
            """
        ).fetchone()
        units_saved = round(float(batch_totals["individual"]) - float(batch_totals["spent"]), 4)
        failed = sum(row_status(db, row["domain"])[0] == "failed" for row in rows)
        provider_calls = len(rows) + int(batch_totals["calls"])
        for metric, value in (
            ("scraped", scraped),
            ("labelled", labelled),
            ("batch_calls", int(batch_totals["calls"])),
            ("units_saved", units_saved),
            ("failed", failed),
        ):
            run.count(metric, value)
        run.success(
            rows=len(rows),
            scraped=scraped,
            labelled=labelled,
            batch_calls=int(batch_totals["calls"]),
            provider_calls=provider_calls,
            batch_label_units=float(batch_totals["spent"]),
            individual_equivalent_units=float(batch_totals["individual"]),
            units_saved=units_saved,
            labels=dict(sorted(label_counts.items())),
            failed=failed,
            plan_id=plan_id,
        )
        return 0
    except BaseException as exc:
        run.fail(exc)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
