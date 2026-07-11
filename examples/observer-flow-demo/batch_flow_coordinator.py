#!/usr/bin/env python3
"""Run the synthetic mixed row and batch Observer Flow example."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

from batch_synthetic_data import build_rows
from demo_runtime import (
    DemoRuntime,
    HERE,
    REPO,
    REUSABLE,
    build_plan_id,
    canonical_hash,
    connect,
    emit,
    graph_event,
    load_node,
    load_source_rows,
    matching_result,
    node_input_hash,
    ordered_nodes,
    restore_cached_result,
    result_map,
    row_data,
    unit_route,
)


TABLE = "websites"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic single + batch Observer Flow demo")
    parser.add_argument("--flow", default=str(HERE / "batch_pipeline.flow.json"))
    parser.add_argument("--state-dir", default=str(HERE / ".observer"))
    parser.add_argument("--session", default="batch-flow-demo")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--state-name", default="synthetic-homepage-batch-labeling")
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--provider-rate", type=float, default=20.0)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--full-run", action="store_true")
    return parser.parse_args()


def connect_state(path: Path) -> sqlite3.Connection:
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


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def website_row_status(db: sqlite3.Connection, row_key: str) -> tuple[str, str]:
    results = result_map(db, row_key)
    failures = [result["error"] for result in results.values() if result["status"] == "failed"]
    if failures:
        return "failed", failures[-1]
    if row_data(db, row_key).get("export_status") in {"planned", "simulated append"}:
        return "complete", ""
    if any(result["status"] == "held" for result in results.values()):
        return "held", ""
    return "running", ""


def save_batch_response(db: sqlite3.Connection, batch_id: str, response: dict) -> None:
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


def execute_batch_node(
    runtime: DemoRuntime,
    node: dict,
    rows: list[dict],
    *,
    requested_batch_size: int,
    provider_rate: float,
    delay: float,
    throttle,
) -> int:
    db, run, ledger = runtime.db, runtime.run, runtime.ledger
    batch_size = max(
        1,
        min(requested_batch_size, int(node.get("batch", {}).get("max_items", requested_batch_size))),
    )
    ready_keys: list[str] = []
    cached_keys: list[str] = []
    member_hashes: dict[str, str] = {}
    cached_results: dict[str, dict] = {}

    runtime.emit_node(node, len(rows), "running", provider_calls=0)
    for source_row in rows:
        key = str(source_row["domain"])
        current = row_data(db, key)
        dependencies = result_map(db, key)
        input_hash = node_input_hash(node, current, run.dry_run, dependencies)
        member_hashes[key] = input_hash
        prior = matching_result(db, key, node["id"], input_hash)
        if prior and prior["status"] in REUSABLE:
            cached_results[key] = dict(prior)
            cached_keys.append(key)
            restore_cached_result(db, key, prior)
            emit(
                ledger,
                run,
                "flow_unit",
                node_id=node["id"],
                node_label=node.get("label", node["id"]),
                table=TABLE,
                key=key,
                status="cached",
                reason="reused durable batch member result",
                node_version=prior["node_version"],
                input_hash=input_hash,
                unit_attempt=prior["attempt"],
                duration_ms=prior["duration_ms"],
                spend_units=0,
            )
            runtime.emit_record(key, node)
            # Cache hits still form a durable row boundary — do not skip
            # operator pause/stop (same contract as the non-batch path).
            run.checkpoint(node["id"], key)
            run.check_controls(after_record=True)
            continue

        route, reason = unit_route(node, current, dependencies)
        if route == "execute":
            ready_keys.append(key)
            continue
        runtime.persist_terminal(
            key,
            node,
            status=route,
            reason=reason,
            input_hash=input_hash,
        )
        run.checkpoint(node["id"], key)
        run.check_controls(after_record=True)

    cached_groups = chunks(cached_keys, batch_size)
    pending_groups = chunks(ready_keys, batch_size)
    total_batches = len(cached_groups) + len(pending_groups)
    batches_completed = provider_calls = 0

    for keys in cached_groups:
        batches_completed += 1
        digest = canonical_hash({
            "node_id": node["id"],
            "keys": keys,
            "input_hashes": [member_hashes[key] for key in keys],
        }).split(":", 1)[1][:16]
        original_spend = sum(float(cached_results[key]["spend_units"]) for key in keys)
        emit(
            ledger,
            run,
            "flow_batch",
            node_id=node["id"],
            node_label=node.get("label", node["id"]),
            batch_id=f"{node['id']}-cached-{batches_completed:02d}-{digest}",
            position=batches_completed,
            total_batches=total_batches,
            status="cached",
            items=len(keys),
            spend_units=0,
            original_spend_units=round(original_spend, 4),
            saved_units=round(original_spend, 4),
            reused_response=True,
            provider_called=False,
        )
        runtime.emit_node(
            node,
            len(rows),
            "running",
            batches_completed=batches_completed,
            batches_total=total_batches,
            provider_calls=provider_calls,
        )

    batch_run = load_node(node)
    for keys in pending_groups:
        batches_completed += 1
        digest = canonical_hash({
            "node_id": node["id"],
            "keys": keys,
            "input_hashes": [member_hashes[key] for key in keys],
        }).split(":", 1)[1][:16]
        batch_id = f"{node['id']}-{batches_completed:02d}-{digest}"
        with db:
            db.execute(
                """
                INSERT OR IGNORE INTO batch_calls
                  (batch_id,node_id,position,total_batches,status,row_keys_json,request_id,
                   response_json,spend_units,individual_equivalent_units,error,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    batch_id, node["id"], batches_completed, total_batches, "planned",
                    json.dumps(keys), "", "", 0, len(keys), "", time.time(),
                ),
            )
        emit(
            ledger,
            run,
            "flow_batch",
            node_id=node["id"],
            node_label=node.get("label", node["id"]),
            batch_id=batch_id,
            position=batches_completed,
            total_batches=total_batches,
            status="running",
            items=len(keys),
            row_keys=keys,
        )
        for key in keys:
            emit(
                ledger,
                run,
                "flow_unit",
                node_id=node["id"],
                node_label=node.get("label", node["id"]),
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
            throttle(f"demo-{node.get('spend', {}).get('provider', node['id'])}", provider_rate)
            time.sleep(max(0.0, delay * 4.0))
            response = batch_run([row_data(db, key) for key in keys], batch_id=batch_id)
            save_batch_response(db, batch_id, response)
            provider_calls += 1

        batch_started = time.monotonic()
        succeeded = failed = 0
        for key in keys:
            member = response.get("results", {}).get(key) or {
                "status": "failed",
                "fields": {"label_batch_id": batch_id, "label_status": "response missing"},
                "evidence": {"request_id": response.get("request_id", "")},
                "error": "Batch response omitted this member",
                "spend_units": 0,
            }
            status = member.get("status", "succeeded")
            succeeded += status == "succeeded"
            failed += status == "failed"
            runtime.persist_terminal(
                key,
                node,
                status=status,
                fields=dict(member.get("fields", {})),
                evidence=dict(member.get("evidence", {})),
                reason="batch member result committed" if status == "succeeded" else "batch member failed",
                error=str(member.get("error", "")),
                spend_units=float(member.get("spend_units", 0)),
                duration_ms=max(1, int((time.monotonic() - batch_started) * 1000)),
                batch_id=batch_id,
                input_hash=member_hashes[key],
            )
        with db:
            db.execute(
                "UPDATE batch_calls SET status = 'complete', updated_at = ? WHERE batch_id = ?",
                (time.time(), batch_id),
            )
        saved = round(
            float(response["individual_equivalent_units"]) - float(response["spend_units"]), 4
        )
        emit(
            ledger,
            run,
            "flow_batch",
            node_id=node["id"],
            node_label=node.get("label", node["id"]),
            batch_id=batch_id,
            position=batches_completed,
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
        runtime.emit_node(
            node,
            len(rows),
            "running",
            batches_completed=batches_completed,
            batches_total=total_batches,
            provider_calls=provider_calls,
        )
        run.checkpoint(node["id"], batch_id)
        run.check_controls(after_record=True)

    runtime.emit_node(
        node,
        len(rows),
        "complete",
        batches_completed=batches_completed,
        batches_total=total_batches,
        provider_calls=provider_calls,
    )
    return provider_calls


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RUNGUARD_STATE_DIR"] = str(state_dir)
    os.environ["RUNGUARD_SESSION"] = args.session
    sys.path.insert(0, str(REPO / "skills" / "observer-kit"))
    from runguard import RunPaused, ledger, start_observed_run, throttle

    flow = json.loads(Path(args.flow).expanduser().resolve().read_text(encoding="utf-8"))
    rows = build_rows(max(1, min(args.limit, len(build_rows()))))
    nodes = ordered_nodes(flow["nodes"])
    plan_id = build_plan_id(
        flow,
        batch_size=args.batch_size,
        provider_rate=args.provider_rate,
    )
    state_name = "".join(char if char.isalnum() or char in "-_" else "-" for char in args.state_name)
    db = connect_state(state_dir / f"{state_name}.flow.sqlite3")
    load_source_rows(db, rows, "domain")

    run = start_observed_run(
        "observer-flow-batch-demo",
        source=str(HERE / "batch_synthetic_data.py"),
        dry_run=args.dry_run,
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
    runtime = DemoRuntime(
        ledger,
        run,
        db,
        nodes,
        table=TABLE,
        hidden_fields={"fixture_kind", "index"},
        row_status=website_row_status,
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
            runtime.emit_record(row["domain"], source_node)
            time.sleep(max(0.0, args.delay * 0.04))

        for node in nodes:
            mode = node.get("mode")
            if mode == "batch":
                execute_batch_node(
                    runtime,
                    node,
                    rows,
                    requested_batch_size=args.batch_size,
                    provider_rate=args.provider_rate,
                    delay=args.delay,
                    throttle=throttle,
                )
                continue
            if mode not in {"map", "sink"}:
                raise ValueError(
                    f"compact batch demo supports map, batch, and sink nodes; "
                    f"{node['id']} uses {mode!r}"
                )
            node_run = load_node(node)
            runtime.emit_node(node, len(rows), "running", provider_calls=0)
            for position, row in enumerate(rows, start=1):
                runtime.persist_and_emit_row(
                    row["domain"],
                    node,
                    node_run,
                    provider_rate=args.provider_rate,
                    delay=args.delay * (0.45 if node.get("spend") else 0.08),
                    position=position,
                    total=len(rows),
                )
                calls = sum(
                    runtime.node_counts(node["id"])[name] for name in ("succeeded", "failed")
                )
                runtime.emit_node(
                    node,
                    len(rows),
                    "running",
                    provider_calls=calls if node.get("spend") else 0,
                    position=position,
                )
            runtime.emit_node(node, len(rows), "complete", provider_calls=0)

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
        failed = sum(website_row_status(db, row["domain"])[0] == "failed" for row in rows)
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
