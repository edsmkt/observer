#!/usr/bin/env python3
"""Browser-level acceptance test for the shipped Observer dashboard."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen


SKILL_DIR = Path(__file__).resolve().parent
RUN_DASHBOARD = SKILL_DIR / "run_dashboard.py"
REQUIRE_BROWSER = os.environ.get("OBSERVER_REQUIRE_BROWSER_TEST") == "1"

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    message = "Playwright is unavailable; install observer-kit[browser-test] to run browser acceptance."
    if REQUIRE_BROWSER:
        print(f"FAIL {message}")
        raise SystemExit(1)
    print(f"SKIP {message}")
    raise SystemExit(0)


passed = 0


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if not condition:
        raise AssertionError(f"{name}" + (f": {detail}" if detail else ""))
    passed += 1
    print(f"  PASS {name}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def wait_for(predicate, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def wait_for_server(port: int) -> bool:
    def ready() -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=0.25) as response:
                return response.status == 200
        except OSError:
            return False

    return wait_for(ready)


def launch_browser(playwright):
    failures = []
    for options in ({}, {"channel": "chrome"}):
        try:
            return playwright.chromium.launch(headless=True, **options)
        except PlaywrightError as exc:
            failures.append(str(exc).splitlines()[0])
    message = "No Chromium browser is available: " + "; ".join(failures)
    if REQUIRE_BROWSER:
        raise RuntimeError(message)
    print(f"SKIP {message}")
    return None


def record(index: int) -> dict:
    event = {
        "ts": "2026-07-11T12:00:01Z",
        "event": "record",
        "table": "items",
        "key": f"row-{index:03d}",
        "name": f"Synthetic item {index:03d}",
        "segment": "priority" if index % 2 else "standard",
        "score": index,
        "active": index % 2 == 0,
        "response_json": {
            "source": "browser fixture",
            "rank": index,
            "tags": ["synthetic", "reviewable"],
        },
        "destination": "appended" if index % 3 else "held",
        "status": "done",
        "owner": f"queue-{index % 5}",
        "note": f"Rendered row {index:03d}",
    }
    if index == 13:
        event["error"] = "synthetic failure for attention QA"
    return event


print("Testing Observer dashboard in a real browser\n")

with tempfile.TemporaryDirectory(prefix="observer-browser-") as tmp:
    state = Path(tmp)
    ledger = state / "browser-smoke.jsonl"
    chat_file = state / "chat.jsonl"
    control_file = state / "controls.jsonl"
    run_id = "runguard:browser-smoke.jsonl"
    append_event(ledger, {
        "ts": "2026-07-11T12:00:00Z",
        "event": "run_started",
        "description": "Generic browser acceptance fixture",
        "todo": 80,
        "dry_run": True,
        "progress_table": "items",
        "summary_metrics": [{"key": "processed", "label": "processed"}],
    })
    append_event(ledger, {
        "ts": "2026-07-11T12:00:00Z",
        "event": "flow_graph",
        "rows_total": 80,
        "plan_id": "browser-smoke-v1",
        "graph": {
            "id": "browser-smoke",
            "label": "Browser smoke flow",
            "description": "A generic source and destination",
            "table": "items",
            "nodes": [
                {"id": "load", "label": "Load source", "kind": "source", "version": "1"},
                {"id": "write", "label": "Write destination", "kind": "sink", "version": "1"},
                {"id": "derive", "label": "Derive fallback counts", "kind": "map", "version": "1"},
            ],
            "edges": [
                {"from": "load", "to": "write", "label": "ready"},
                {"from": "write", "to": "derive", "label": "confirmed"},
            ],
        },
    })
    append_event(ledger, {
        "ts": "2026-07-11T12:00:00Z", "event": "flow_node",
        "node_id": "load", "node_label": "Load source", "status": "complete",
        "completed": 80, "total": 80, "succeeded": 80,
    })
    append_event(ledger, {
        "ts": "2026-07-11T12:00:00Z", "event": "flow_node",
        "node_id": "write", "node_label": "Write destination", "status": "running",
        "completed": 79, "total": 80, "succeeded": 79,
    })
    for node_id, status in (("load", "succeeded"), ("write", "succeeded")):
        append_event(ledger, {
            "ts": "2026-07-11T12:00:00Z", "event": "flow_unit",
            "node_id": node_id, "key": "row-000", "status": status,
        })
    for key, status in (("row-000", "succeeded"), ("row-001", "failed")):
        append_event(ledger, {
            "ts": "2026-07-11T12:00:00Z", "event": "flow_unit",
            "node_id": "derive", "key": key, "status": status,
        })
    for index in range(80):
        append_event(ledger, record(index))

    quoted_ledger = state / "quoted-'run.jsonl"
    append_event(quoted_ledger, {
        "ts": "2026-07-11T12:00:00Z",
        "event": "run_started",
        "description": "Quoted identifier fixture",
        "todo": 0,
        "dry_run": True,
    })
    append_event(quoted_ledger, {
        "ts": "2026-07-11T12:00:01Z", "event": "run_finished",
    })

    # A live lock exposes cooperative pause/stop controls in the browser.
    (state / "browser-smoke.lock").write_text(json.dumps({
        "pid": os.getpid(),
        "scope": "browser-smoke-source",
        "started": "2026-07-11T12:00:00Z",
    }), encoding="utf-8")

    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-B", str(RUN_DASHBOARD), str(state), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ok("dashboard server starts", wait_for_server(port))
        with sync_playwright() as playwright:
            browser = launch_browser(playwright)
            if browser is None:
                raise SystemExit(0)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                browser_errors: list[str] = []
                page.on("console", lambda msg: browser_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
                page.goto(
                    f"http://127.0.0.1:{port}/#{quote(run_id, safe='')}",
                    wait_until="domcontentloaded",
                )

                rows = page.locator("#content tbody tr")
                expect(rows).to_have_count(80, timeout=15_000)
                headers = page.locator("#content thead").inner_text().lower()
                ok("generic records and workflow-defined columns render",
                   all(name in headers for name in ("name", "segment", "score", "active", "response_json")),
                   headers)
                ok("stable row ordinals render", rows.first.locator("td.rownum").inner_text() == "1")

                page.locator("tr[data-co='row-000'] .jsonOpen").click()
                expect(page.locator("#cellmodal")).to_have_class("show")
                json_text = page.locator("#cellmodalbody").inner_text()
                ok("structured response JSON opens with full decoded content",
                   '"source": "browser fixture"' in json_text and '"rank": 0' in json_text,
                   json_text)
                page.locator("#cellmodalactions button").click()

                page.get_by_role("button", name="Filter columns").click()
                panel = page.locator(".filterPanel")
                panel.locator("select").nth(0).select_option("score")
                panel.locator("select").nth(1).select_option("gte")
                panel.locator("input[type=number]").first.fill("70")
                page.get_by_role("button", name="Add filter").click()
                expect(rows).to_have_count(10)
                ok("typed numeric filter narrows rendered rows", rows.count() == 10)
                page.locator(".filterChip button[title='Remove filter']").click()
                expect(rows).to_have_count(80)

                page.locator("#tabAttention").click()
                expect(rows).to_have_count(1)
                ok("Attention renders only rows with explicit errors",
                   "synthetic failure for attention QA" in rows.first.inner_text())

                page.locator("#tabFlow").click()
                expect(page.locator(".flowNode")).to_have_count(3)
                expect(page.locator("#flowEdges path[marker-end]")).to_have_count(2)
                edge_path = page.locator("#flowEdges path[marker-end]").first.get_attribute("d") or ""
                ok("flow nodes and their SVG dependency edge execute in the DOM",
                   bool(edge_path) and "Load source" in page.locator(".flowShell").inner_text() and
                   "Write destination" in page.locator(".flowShell").inner_text(), edge_path)
                derived_card = page.locator(".flowNode[data-node-id='derive']")
                derived_text = derived_card.inner_text()
                ok("flow cards derive counts when aggregate events omit them",
                   "1\nsucceeded" in derived_text and "1\nfailed" in derived_text,
                   derived_text)

                page.get_by_role("button", name="Message agent").click()
                page.locator("#chatinput").fill("Please review the browser fixture.")
                page.locator("#chatSend").click()
                ok("dashboard chat POST persists an operator message", wait_for(lambda: any(
                    item.get("text") == "Please review the browser fixture."
                    for item in read_jsonl(chat_file)
                )))
                page.locator("#chatpop button", has_text="Close").click()

                page.locator(".controlBtn[aria-label='Request pause']").click()
                expect(page.locator("#chatpop")).to_be_visible()
                expect(page.locator("#chatpopHead")).to_contain_text("Pause")
                ok("pause control POST reaches the durable control channel", wait_for(lambda: any(
                    item.get("run") == run_id and item.get("kind") == "pause"
                    for item in read_jsonl(control_file)
                )))
                page.locator("#chatinput").fill("Pause so I can inspect the newest rows.")
                page.locator("#chatSend").click()
                ok("pause context is delivered through operator chat", wait_for(lambda: any(
                    item.get("text") == "Pause: Pause so I can inspect the newest rows."
                    for item in read_jsonl(chat_file)
                )))

                page.locator("#tabRecords").click()
                expect(rows).to_have_count(80)
                shell = page.locator(".recordshell")
                before = shell.evaluate("""element => {
                    element.scrollTop = Math.min(900, element.scrollHeight - element.clientHeight);
                    element.scrollLeft = Math.min(500, element.scrollWidth - element.clientWidth);
                    return {top: element.scrollTop, left: element.scrollLeft,
                            maxTop: element.scrollHeight - element.clientHeight,
                            maxLeft: element.scrollWidth - element.clientWidth};
                }""")
                ok("fixture provides a genuinely scrollable live table",
                   before["top"] > 100 and before["left"] > 100, str(before))
                append_event(ledger, record(80))
                expect(rows).to_have_count(81, timeout=8_000)
                page.wait_for_timeout(200)
                after = shell.evaluate("element => ({top: element.scrollTop, left: element.scrollLeft})")
                ok("live append preserves vertical and horizontal table position",
                   abs(after["top"] - before["top"]) <= 3 and
                   abs(after["left"] - before["left"]) <= 3,
                   f"before={before}, after={after}")
                quoted_run = page.locator("#runs .run").filter(has_text="quoted-'run")
                expect(quoted_run).to_have_count(1)
                ok("quoted run IDs are preserved in escaped data attributes",
                   quoted_run.get_attribute("data-run-id") == "runguard:quoted-'run.jsonl")
                quoted_run.click()
                expect(page.locator("#runs .run.sel")).to_contain_text("quoted-'run")
                page.locator("#runs .run").filter(has_text="browser-smoke").click()
                expect(rows).to_have_count(81)
                ok("quoted run identifiers remain selectable without inline JavaScript",
                   "browser-smoke" in page.locator("#runs .run.sel").inner_text())
                ok("browser execution reports no JavaScript errors", not browser_errors,
                   "; ".join(browser_errors))
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)

print(f"\n{passed} passed, 0 failed")
