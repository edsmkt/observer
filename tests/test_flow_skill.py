#!/usr/bin/env python3
"""Cold-start contract tests for the Observer Flow agent skill."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


passed = failed = 0
HERE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
SKILL = HERE / "SKILL.md"
CONTRACT = HERE / "references" / "flow-contract.md"
COOKBOOK = HERE / "references" / "cookbook-contract.md"
EXAMPLE = HERE / "examples" / "website-qualification.flow.json"
# Structural validator is package runtime (not the skill tree).
VALIDATOR = Path(__file__).resolve().parents[1] / "observer_kit" / "validate_flow.py"
METADATA = HERE / "agents" / "openai.yaml"
OBSERVER_SKILL = HERE.parent / "observer-kit" / "SKILL.md"
OBSERVER_PATTERN = HERE.parent / "observer-kit" / "references" / "pattern.md"


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" +
          (f" - {detail}" if detail and not condition else ""))
    if condition:
        passed += 1
    else:
        failed += 1


def prose(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return "\n".join(lines)


print(f"Testing Observer Flow skill contract at {HERE}\n")

skill = SKILL.read_text(encoding="utf-8")
contract = CONTRACT.read_text(encoding="utf-8")
cookbook = COOKBOOK.read_text(encoding="utf-8")
metadata = METADATA.read_text(encoding="utf-8")
example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
skill_words = " ".join(skill.split())
contract_words = " ".join(contract.split())
description_match = re.search(r"^description:\s*(.+)$", skill, re.MULTILINE)
description = description_match.group(1) if description_match else ""

steps = re.findall(r"^## ([1-7])\. ", skill, re.MULTILINE)
criteria = re.findall(r"^\*\*Complete when:\*\*", skill, re.MULTILINE)
ok("cold start has seven ordered steps", steps == list("1234567"), str(steps))
ok("every step has a completion gate", len(criteria) == len(steps) == 7,
   f"steps={len(steps)} criteria={len(criteria)}")
ok("primary skill stays compact", len(skill.splitlines()) <= 250,
   f"{len(skill.splitlines())} lines")

ok("description targets multi-stage orchestration",
   all(term in description for term in (
       "multi-stage", "chaining scripts", "fields depend", "DAG",
       "conditionally enriching", "rerunning affected steps")), description)
ok("description leaves ordinary harness work to Observer Kit",
   "Observer Kit operations" in description and "Observer Flow" not in description)

paths = (CONTRACT, COOKBOOK, EXAMPLE, VALIDATOR, METADATA, OBSERVER_SKILL, OBSERVER_PATTERN)
ok("every bundled and sibling context pointer resolves",
   all(path.is_file() for path in paths), ", ".join(str(path) for path in paths))
ok("skill explicitly loads Observer Kit for execution guarantees",
   "[`../observer-kit/SKILL.md`](../observer-kit/SKILL.md)" in skill and
   "[`../observer-kit/references/pattern.md`](../observer-kit/references/pattern.md)" in skill and
   all(term in skill_words for term in (
       "source lock, live rows, durable boundaries",
       "sample proof, and explicit full-run approval")))
ok("flow contract and example have clear load reasons",
   "Read [`references/flow-contract.md`](references/flow-contract.md) in full" in skill and
   "when a concrete conditional-enrichment graph helps" in skill)
ok("cookbook contract has a clear conditional load reason",
   "[`references/cookbook-contract.md`](references/cookbook-contract.md)" in skill and
   "reusable code or repeated node clusters" in skill and
   "create and maintain the user's project cookbook" in skill)

negative_steering = re.compile(
    r"\b(?:do not|don't|never|avoid|without|unless|not)\b|anti-pattern",
    re.IGNORECASE,
)
skill_negations = negative_steering.findall(prose(skill))
contract_negations = negative_steering.findall(prose(contract))
cookbook_negations = negative_steering.findall(prose(cookbook))
ok("skill uses positive steering", not skill_negations, str(skill_negations))
ok("production contract uses positive steering", not contract_negations,
   str(contract_negations[:10]))
ok("cookbook contract uses positive steering", not cookbook_negations,
   str(cookbook_negations[:10]))

ok("skill uses the CLI surface that exists today",
   "observer-kit run --state-dir .observer" in skill and
   "observer-kit dashboard .observer" in skill and
   "observer-kit flow" not in skill)
ok("one node can own several coherent fields",
   "A node may produce several related fields from one call" in skill and
   all(field in example["nodes"][0]["outputs"] for field in (
       "website_title", "description", "social_links")))
ok("graph model has stable rows, fields, nodes, edges, and plan identity",
   all(term in skill_words for term in (
       "**Row**: one entity with a stable key",
       "**Field**: a visible value on that row",
       "**Node**: one coherent transform",
       "**Edge**: a declared dependency",
       "Build a plan ID from the canonical manifest")))
ok("cold-start discovery asks evidence-backed operator questions",
   all(term in skill_words for term in (
       "perform bounded read-only discovery through the real client and query shape",
       "Present an evidence-backed initial proposal",
       "ask two to five concise questions")))
ok("manifest covers dependencies, conditions, limits, and destinations",
   all(term in skill_words for term in (
       "needs`, `inputs`, `outputs`, and optional `when` condition",
       "spend and concurrency ceilings",
       "side-effect destination and confirmation contract")) and
   any(node.get("when") for node in example["nodes"]) and
   any(node.get("side_effect") for node in example["nodes"]))

ok("one scheduler owns durable graph execution",
   all(term in skill_words for term in (
       "Use one coordinator as the execution authority",
       "SQLite as the authoritative queue and result index",
       "Observer Kit JSONL as the append-only audit and dashboard projection")) and
   all(term in contract_words for term in (
       "transactional outbox", "Use one coordinator for one graph run",
       "Startup flushes pending outbox rows first")))
ok("business rows evolve in place with compact graph detail",
   all(term in skill_words for term in (
       "updates the same dashboard row with newly produced fields",
       "Declare an intentional source-field update with the node's `updates` list",
       "Keep business fields primary in the dashboard",
       "place detailed execution metadata in clickable `flow_json`",
       "update destination fields on the same row")))
ok("cold-start coordinators emit the live visual flow contract",
   all(term in skill_words for term in (
       "`flow_graph`, `flow_node`, `flow_unit`, and bounded `flow_batch` events",
       "each node result appears on its business row and Flow view")) and
   all(term in contract_words for term in (
       "The Flow tab is a visual projection of committed coordinator state",
       "`flow_graph` once near the start of each attempt",
       "`flow_node` when a node starts and after each terminal unit",
       "`flow_unit` when a row enters a node and after its durable terminal commit",
       "`flow_batch` when a bounded batch request starts",
       "Follow it with the same-key `record` projection")))
ok("flow contract covers generic transformation shapes",
   all(heading in contract for heading in (
       "### Map", "### Expand", "### Join", "### Reduce", "### Batch", "### Sink")) and
   all(term in contract_words for term in (
       "deterministic keys", "aggregate partition", "result envelope per member")))
ok("cache and invalidation preserve unaffected work",
   all(term in skill_words for term in (
       "matching input hashes reuse cached results",
       "changed input recomputes the owning node and affected descendants",
       "Reuse matching upstream results")) and
   "Cache keys remain node-specific" in contract_words)
ok("failure recovery keeps completed fields and restores events",
   all(term in skill_words for term in (
       "forced crash resumes from committed node results",
       "node failure preserves completed upstream fields")) and
   "committed fields return to the dashboard through the outbox" in contract_words)

ok("approval binds exact graph behavior and scope",
   all(term in skill_words for term in (
       "approves this plan ID for the stated full scope and says to begin",
       "authorizes exactly that named operation",
       "creates a new plan ID and a new bounded sample")) and
   all(term in contract_words for term in (
       "source identity, snapshot, and full row scope",
       "operator action or quoted approval message",
       "approved mode: canary or full run")))
ok("additional enrichment updates current rows selectively",
   all(term in skill_words for term in (
       "add or revise the owning node",
       "update the same table and stable keys after approval",
       "recompute the changed node plus its descendants")))
ok("agent creates the user's cookbook from real project work",
   all(term in skill_words for term in (
       "Create or update the user's `flow-cookbook/`",
       "Search its catalog first",
       "append each distinct implementation occurrence to `observations.jsonl`",
       "counting code or graph locations rather than row executions",
       "ask whether to keep it separate or promote it",
       "promote it after its isolated tests and integrated sample proof pass")) and
   not (HERE / "cookbook").exists())
ok("cookbook supports reusable nodes, subflows, and adapters",
   all(term in cookbook for term in (
       "### Node Recipe", "### Subflow Recipe", "### Adapter Recipe")) and
   all(term in " ".join(cookbook.split()) for term in (
       "flow-cookbook/catalog.json",
       "The project cookbook remains the production source pinned by the graph plan",
       "candidate", "proven", "superseded")))
ok("cookbook recipes are versioned, tested, and plan-bound",
   all(term in " ".join(cookbook.split()) for term in (
       "typed input and output ports",
       "entrypoint, fixtures, and tests",
       "includes recipe files and configuration in node and plan hashes",
       "Sample the changed path",
       "Record the sample run, plan ID, verification time")))
ok("repeated mechanics become code nodes at coherent boundaries",
   all(term in " ".join(cookbook.split()) for term in (
       "the same mechanics repeated across nodes, branches, or workflows",
       "one retry, cache, and durable-result boundary",
       "intermediate details that fit clickable evidence",
       "Use a reusable subflow when intermediate steps provide independent value",
       "ordered provider fallback, paginated fetch plus normalization",
       "Every real API request inside a code node still acquires the shared provider throttle",
       "Combining code preserves these controls")))
ok("reuse observations drive a user consultation before promotion",
   all(term in " ".join(cookbook.split()) for term in (
       "flow-cookbook/observations.jsonl",
       "Count one occurrence for each distinct code location, node definition, subflow, or workflow adaptation",
       "Row count, retry count, and ordinary execution volume remain runtime metrics",
       "Ask the user for their preferred proposal threshold",
       "third distinct occurrence: recommend code-node, subflow, or adapter promotion",
       "Ask the user whether to promote it into a code node, subflow, or adapter",
       "Create a candidate recipe after the user selects promotion",
       "respects prior promotion decisions")))
ok("metadata describes the orchestration role",
   "display_name: \"Observer Flow\"" in metadata and
   "dependency-driven data workflows" in metadata and
   "$observer-flow" in metadata and "user's project cookbook" in metadata)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
