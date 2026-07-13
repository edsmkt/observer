#!/usr/bin/env python3
"""Lint an agent-written batch script for common Observer Kit contract gaps:

    buffering all provider results in memory and emitting `record` ledger
    rows only in a final flush block (instead of as work lands).

    reporting progress while the actual result remains memory-only until a
    final write. A live dashboard is not a durable resume point.

    reporting progress (or metric/count heartbeats) from a repeated slow
    phase while its record/table surface stays empty until a terminal
    preview flush — the operator watches an empty dashboard table for the
    entire discovery phase.

    satisfying table liveness with synthetic phase rows while discovered
    business entities remain invisible until completion.

    applying a dry-run limit only to terminal preview rows instead of source
    queries/pages, or mutating a canary before its business row is visible.

    reading free-form dashboard chat as worker control instead of consuming the
    structured, acknowledged control channel.

    starting an observed run with no explicit summary_metrics selection, which
    leaves useful terminal totals outside the intended headline surface.

This defeats live dashboard visibility and loses everything if the process
crashes mid-run. Run it on any script before the full run:

    python3 references/lint_emit.py path/to/script.py
Exit code 0 = no common violation detected, 1 = violation found. A zero result
still needs the forced crash/resume proof required by SKILL.md.

Heuristic (intentionally simple, stdlib-only):
  A script is SUSPECT if it calls ledger(... 'record' ...) but NONE of those
  calls are statically inside a per-item loop (for/while whose body or a called
  function emits record events). We treat a record-emit as "inside the loop" if
  the emit call's enclosing function is invoked from a loop, OR the emit call
  is lexically inside a for/while that ranges over the work items.

Because agents write many shapes, we also look for the canonical smell:
  - a results dict/list is populated inside a loop, AND
  - the only ledger('record') calls are in a later block that ranges over the
    same items (a flush), with no emit inside the loop.
  - a results container is populated in a work loop but there is no apparent
    durable sink call in that loop or completion callback. Progress/metric
    ledger calls alone do not count as persistence.
"""
import argparse
import ast
import sys

RECORD_EVENTS = {'record'}
LOOP_TYPES = (ast.For, ast.AsyncFor, ast.While)
COLLECTION_MUTATIONS = {'append', 'extend', 'update', 'add'}
OBSERVABILITY_CALLS = {'ledger', 'progress', 'count', 'checkpoint', 'metric', 'step'}
DURABLE_WORDS = ('write', 'append', 'insert', 'upsert', 'persist', 'save',
                 'commit', 'receipt', 'checkpoint', 'dump', 'store', 'execute')
READ_DERIVATION_CALLS = {'load', 'loads', 'get', 'items', 'values', 'keys',
                         'strip', 'split', 'decode', 'copy'}
PHASE_TABLES = {'phase', 'phases', 'progress', 'status', 'run_status'}
SOURCE_CALL_WORDS = ('build', 'fetch', 'read', 'search', 'query', 'scan', 'page',
                     'request', 'urlopen', 'select', 'load', 'source', 'islice')
MUTATION_CALL_WORDS = ('patch', 'write', 'insert', 'upsert', 'update', 'delete',
                       'send', 'post', 'put')


def _is_ledger_event_call(node, event):
    """Return True when `node` emits the requested dashboard event."""
    if not isinstance(node, ast.Call):
        return False
    # ledger(...) and runguard.ledger(...)
    is_ledger = (
        isinstance(node.func, ast.Name) and node.func.id == 'ledger'
    ) or (
        isinstance(node.func, ast.Attribute) and node.func.attr in ('ledger', '_ledger')
    )
    if is_ledger:
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            return node.args[1].value == event
    # Demo/runtime helper: emit(ledger, run, "record", table=..., key=...)
    if isinstance(node.func, ast.Name) and node.func.id == 'emit':
        if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
            return node.args[2].value == event
        for keyword in node.keywords:
            if (keyword.arg == 'event' and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == event):
                return True
    if isinstance(node.func, ast.Attribute):
        # ObservedRun.step() always emits stable record rows.
        # Demo helpers stream Data-table rows under other names.
        if event == 'record' and node.func.attr in (
                'step', 'record', 'emit_record', 'persist_and_emit_row'):
            return True
        if event == 'progress' and node.func.attr == 'progress':
            return True
        if event == 'metric' and node.func.attr == 'metric':
            return True
        if event == 'metric' and node.func.attr == 'count':
            # run.count() emits metric events; list/str .count() must not match.
            return _is_observed_receiver(node.func.value)
        if node.func.attr == 'step':
            for kw in node.keywords:
                if (kw.arg == 'event' and isinstance(kw.value, ast.Constant)
                        and kw.value.value == event):
                    return True
    return False


def _is_ledger_record_call(node):
    return _is_ledger_event_call(node, 'record')


HEARTBEAT_METHOD_ATTRS = frozenset({'progress', 'count', 'metric'})
# Unresolved Name() calls with these stems are treated as heartbeats so imported
# helpers like tick()/emit_progress() cannot hide a progress-only discovery loop.
HEARTBEAT_NAME_STEMS = ('progress', 'heartbeat', 'metric', 'tick', 'pace')
# Receivers that look like ObservedRun / runguard — avoid list/str .count() FPs.
OBSERVED_RECEIVER_NAMES = frozenset({
    'run', 'runguard', 'rg', 'self', 'observed', 'guard', 'obs',
})


def _is_observed_receiver(node):
    """True when the attribute base is likely an ObservedRun / runguard handle."""
    if isinstance(node, ast.Name):
        name = node.id
        return (
            name in OBSERVED_RECEIVER_NAMES
            or name.endswith('_run')
            or name.endswith('Run')
            or name.startswith('run_')
        )
    if isinstance(node, ast.Attribute):
        return (
            node.attr in OBSERVED_RECEIVER_NAMES
            or node.attr.endswith('_run')
            or _is_observed_receiver(node.value)
        )
    return False


def _getattr_heartbeat_parts(call):
    """If ``call`` is ``getattr(recv, 'progress'|'count'|'metric')``, return parts."""
    if not isinstance(call, ast.Call) or _called_name(call) != 'getattr':
        return None, None
    if len(call.args) < 2:
        return None, None
    attr_node = call.args[1]
    if not isinstance(attr_node, ast.Constant) or not isinstance(attr_node.value, str):
        return None, None
    attr = attr_node.value
    if attr not in HEARTBEAT_METHOD_ATTRS:
        return None, None
    return call.args[0], attr


def _expr_is_heartbeat_method(value):
    """True when value is run.count / run.progress / getattr(run, 'count') etc."""
    if isinstance(value, ast.Attribute) and value.attr in HEARTBEAT_METHOD_ATTRS:
        if value.attr == 'count':
            return _is_observed_receiver(value.value)
        return True
    recv, attr = _getattr_heartbeat_parts(value) if isinstance(value, ast.Call) else (None, None)
    if attr is None:
        return False
    if attr == 'count':
        return _is_observed_receiver(recv)
    return True


def _subtree_has_heartbeat_call(root, functions, aliases, attr_aliases=None):
    """True if any Call under root is a heartbeat (used for lambda bodies)."""
    for node in ast.walk(root):
        if isinstance(node, ast.Call) and _is_heartbeat_call(
                node, aliases=aliases, functions=functions,
                attr_aliases=attr_aliases):
            return True
    return False


def _heartbeat_aliases(tree):
    """Return (name_aliases, attr_aliases) for heartbeat callables.

    ``name_aliases``: ``beat = run.count`` then ``beat(...)``.
    ``attr_aliases``: ``h.beat = run.progress`` then ``h.beat()`` as (recv, attr).
    """
    functions = _function_defs(tree)
    name_aliases = set()
    attr_aliases = set()  # (receiver_name, attr)

    def _register_targets(targets, is_hb, aliases_for_lambda):
        nonlocal name_aliases, attr_aliases
        if not is_hb:
            return False
        grew = False
        for target in targets:
            if isinstance(target, ast.Name):
                if target.id not in name_aliases:
                    name_aliases.add(target.id)
                    grew = True
            elif (isinstance(target, ast.Attribute)
                  and isinstance(target.value, ast.Name)):
                key = (target.value.id, target.attr)
                if key not in attr_aliases:
                    attr_aliases.add(key)
                    grew = True
        return grew

    # Fixpoint: lambda bodies may call other aliases.
    for _ in range(4):
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            else:
                continue
            is_hb = _expr_is_heartbeat_method(value)
            if not is_hb and isinstance(value, ast.Lambda):
                is_hb = _subtree_has_heartbeat_call(
                    value, functions, name_aliases, attr_aliases)
            if _register_targets(targets, is_hb, name_aliases):
                grew = True
        if not grew:
            break
    return name_aliases, attr_aliases


def _is_heartbeat_call(node, aliases=None, functions=None, attr_aliases=None):
    """Progress/metric/count signals that advance without filling data tables.

    Covers:
    - ledger('progress'|'metric', ...)
    - run.progress() / run.count() / run.metric() (ObservedRun-like receivers)
    - getattr(run, 'count')('pages') and Name/Attribute aliases of those methods
    - lambda/Name aliases that call the above
    - unresolved helpers named like tick/emit_progress

    ``list.count`` / ``str.count`` are excluded.
    """
    if not isinstance(node, ast.Call):
        return False
    aliases = aliases or set()
    attr_aliases = attr_aliases or set()
    functions = functions or {}
    if _is_ledger_event_call(node, 'progress') or _is_ledger_event_call(node, 'metric'):
        return True
    # getattr(run, 'count')('pages') — outer call, func is the getattr(...) call.
    if isinstance(node.func, ast.Call):
        recv, attr = _getattr_heartbeat_parts(node.func)
        if attr is not None:
            if attr == 'count':
                return _is_observed_receiver(recv)
            return True
    if isinstance(node.func, ast.Attribute):
        # h.beat() where h.beat was assigned run.progress
        if isinstance(node.func.value, ast.Name):
            if (node.func.value.id, node.func.attr) in attr_aliases:
                return True
        if node.func.attr in HEARTBEAT_METHOD_ATTRS:
            attr = node.func.attr
            if attr in ('progress', 'metric'):
                return True
            return _is_observed_receiver(node.func.value)
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if name in aliases:
            return True
        if name not in functions:
            low = name.lower()
            if any(stem in low for stem in HEARTBEAT_NAME_STEMS):
                return True
    return False


def _summary_metric_violations(tree):
    """Find explicit run starts that omit the dashboard headline contract."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_start = _called_name(node) == 'start_observed_run'
        is_raw_start = _is_ledger_event_call(node, 'run_started')
        if not (is_start or is_raw_start):
            continue
        # A **kwargs expansion may carry summary_metrics, so leave that shape to
        # the behavioral sample instead of producing a speculative warning.
        if any(keyword.arg is None for keyword in node.keywords):
            continue
        if not any(keyword.arg == 'summary_metrics' for keyword in node.keywords):
            violations.append(('SUMMARY_METRICS_MISSING', node.lineno))
    return violations


def _contains_limit(node, names):
    return any(
        (isinstance(item, ast.Attribute) and item.attr == 'limit')
        or (isinstance(item, ast.Name) and item.id in names)
        for item in ast.walk(node)
    )


def _sample_limit_violations(tree):
    declarations = []
    has_dry_run = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) != 'add_argument':
            continue
        values = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        declarations.extend(node for value in values if value == '--limit')
        has_dry_run = has_dry_run or '--dry-run' in values
    if not declarations or not has_dry_run:
        return []

    names = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _contains_limit(node.value, names):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            before = len(names)
            for target in targets:
                names.update(_target_names(target))
            changed = changed or len(names) != before

    for call in [node for node in ast.walk(tree) if isinstance(node, ast.Call)]:
        name = (_called_name(call) or '').lower()
        values = list(call.args) + [keyword.value for keyword in call.keywords]
        if (any(word in name for word in SOURCE_CALL_WORDS)
                and any(_contains_limit(value, names) for value in values)):
            return []
        if name in ('range', 'islice') and any(_contains_limit(value, names) for value in values):
            return []

    for node in ast.walk(tree):
        test = node.test if isinstance(node, (ast.If, ast.While)) else None
        if test is None or not _contains_limit(test, names):
            continue
        if any(isinstance(call, ast.Call)
               and any(word in ((_called_name(call) or '').lower()) for word in SOURCE_CALL_WORDS)
               for call in ast.walk(node)):
            return []
    return [('SAMPLE_LIMIT_LATE', declarations[0].lineno)]


def _record_table_kind(call):
    if not _is_ledger_record_call(call):
        return None
    for keyword in call.keywords:
        if keyword.arg == 'table':
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return 'phase' if keyword.value.value.lower() in PHASE_TABLES else 'business'
            return 'business'
    return 'business'


def _function_record_kinds(fn, functions, seen=None):
    seen = set(seen or ())
    if fn.name in seen:
        return set()
    seen.add(fn.name)
    kinds = set()
    for node in _body_nodes(fn):
        if not isinstance(node, ast.Call):
            continue
        kind = _record_table_kind(node)
        if kind:
            kinds.add(kind)
        helper = functions.get(_called_name(node))
        if helper:
            kinds.update(_function_record_kinds(helper, functions, seen))
    return kinds


def _loop_record_kinds(loop, functions):
    kinds = set()
    for node in _body_nodes(loop):
        if not isinstance(node, ast.Call):
            continue
        kind = _record_table_kind(node)
        if kind:
            kinds.add(kind)
        helper = functions.get(_called_name(node))
        if helper:
            kinds.update(_function_record_kinds(helper, functions))
    return kinds


def _name_referenced_in(expr, name):
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(expr))


def _looks_like_counter_rebind(value, name):
    """``n = n + 1`` style counters are not entity accumulation."""
    if not isinstance(value, ast.BinOp) or not isinstance(value.op, (ast.Add, ast.Sub)):
        return False
    left, right = value.left, value.right
    if isinstance(left, ast.Name) and left.id == name and isinstance(right, ast.Constant):
        return isinstance(right.value, (int, float))
    if isinstance(right, ast.Name) and right.id == name and isinstance(left, ast.Constant):
        return isinstance(left.value, (int, float))
    return False


_COUNTER_NAMES = frozenset({'progress', 'metrics', 'counts', 'counters', 'stats'})
_PARALLEL_MAP_ATTRS = frozenset({'map', 'imap', 'imap_unordered'})


def _collection_method_aliases(tree):
    """Names bound to collection mutators: ``add = results.append``."""
    aliases = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Attribute)
                and value.attr in COLLECTION_MUTATIONS):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _node_produces_entities(node, functions, method_aliases=None, seen=None):
    """True when a single statement accumulates business entities."""
    counter_names = _COUNTER_NAMES
    method_aliases = method_aliases or set()
    seen = set(seen or ())
    if isinstance(node, (ast.Yield, ast.YieldFrom)):
        return True
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Subscript) for target in targets):
            return True
        # found = found + [...] / t = {**t, **chunk} — self-merging rebinds.
        if isinstance(node, ast.Assign) and node.value is not None:
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id in counter_names:
                    continue
                if _looks_like_counter_rebind(node.value, target.id):
                    continue
                if _name_referenced_in(node.value, target.id):
                    return True
        # targets += [row] / targets += chunk — AugAssign accumulation.
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            target = node.target
            if isinstance(target, ast.Name) and target.id not in counter_names:
                if isinstance(node.value, ast.Constant) and isinstance(
                        node.value.value, (int, float)):
                    pass  # n += 1 counter
                else:
                    return True
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                roots = _root_names(target)
                if roots and not roots <= counter_names:
                    return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in COLLECTION_MUTATIONS):
        roots = _root_names(node.func.value)
        if roots and not roots <= counter_names:
            return True
    # Bound-method alias: add = results.append; add(item)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in method_aliases):
        return True
    # Helper-mediated accumulation (params, free globals, nested helpers).
    if isinstance(node, ast.Call) and functions:
        helper = functions.get(_called_name(node))
        if helper:
            bindings = _call_bindings(node, helper)
            for parameter in _mutated_parameters(helper, functions):
                roots = bindings.get(parameter, set())
                if roots and not roots <= counter_names:
                    return True
            if _function_produces_entities(helper, functions, method_aliases, seen):
                return True
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return True
    return False


def _function_produces_entities(fn, functions, method_aliases=None, seen=None):
    """True when a helper accumulates entities (including free-name buffers)."""
    seen = set(seen or ())
    if fn.name in seen:
        return False
    seen.add(fn.name)
    method_aliases = method_aliases if method_aliases is not None else set()
    for node in _body_nodes(fn):
        if _node_produces_entities(node, functions, method_aliases, seen):
            return True
    return False


def _loop_produces_entities(loop, functions=None, method_aliases=None):
    """True when a loop accumulates business entities (not mere counters).

    Detects subscript writes, collection mutations, self-merge rebinds,
    ``buf += [...]`` / ``buf += chunk``, bound-method aliases
    (``add = buf.append``), helper param mutations, free-global buffers
    mutated through nested helpers, and nested multi-source comps.
    """
    functions = functions if functions is not None else {}
    method_aliases = method_aliases if method_aliases is not None else set()
    for node in _body_nodes(loop):
        if _node_produces_entities(node, functions, method_aliases):
            return True
    return False


def _looks_like_source_iter_name(name):
    lower = str(name or '').lower()
    return any(token in lower for token in (
        'source', 'page', 'item', 'row', 'batch', 'input', 'lead', 'record',
        'result', 'company', 'data', 'feed', 'cursor', 'query',
    ))


def _comprehension_is_discovery(value):
    """True for comps that fully scan a source before any record can stream.

    Multi-generator comps always count. Single-generator comps count when the
    iterator is a call (``fetch_page(...)``) or a source-like name
    (``source_items``), which agents use for silent map-then-dump shapes.
    """
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        gens = value.generators
    else:
        return False
    if len(gens) >= 2:
        return True
    for gen in gens:
        it = gen.iter
        if isinstance(it, ast.Call):
            return True
        if isinstance(it, ast.Name) and _looks_like_source_iter_name(it.id):
            return True
        if isinstance(it, ast.Attribute) and _looks_like_source_iter_name(it.attr):
            return True
    return False


def _function_comprehension_discovery(fn):
    """True when a helper builds a collection via discovery-shaped comprehension.

    Agents often write ``return [row for page in pages for row in page]``,
    ``return [transform(r) for r in source_items]``, or
    ``return list(worker(x) for x in source_items)`` with no explicit For loop;
    a terminal dump then leaves the Data table empty during discovery.
    """
    for node in _body_nodes(fn):
        if not isinstance(node, (ast.Return, ast.Assign, ast.AnnAssign)):
            continue
        value = getattr(node, 'value', None)
        if _comprehension_is_discovery(value):
            return True
        if (_is_bulk_materialize_call(value) or _is_starred_map_list(value)
                or _is_fold_call(value)):
            return True
    return False


def _is_fold_call(node):
    """``reduce(fn, it, [])`` / ``accumulate(...)`` style folds."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name) and node.func.id in ('reduce', 'accumulate'):
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr in ('reduce', 'accumulate'):
        return True
    return False


def _is_map_family_call(node):
    """``map(fn, it)`` / ``starmap(fn, it)`` / ``itertools.starmap(...)``."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name) and node.func.id in (
            'map', 'starmap', 'filter', 'filterfalse'):
        return len(node.args) >= 2 or (
            node.func.id in ('map', 'starmap') and len(node.args) >= 1)
    if isinstance(node.func, ast.Attribute) and node.func.attr in (
            'map', 'starmap', 'filter', 'filterfalse'):
        return True
    return False


def _is_materialize_wrapper_name(name):
    return name in (
        'list', 'tuple', 'set', 'dict', 'sorted', 'reversed', 'deque', 'frozenset',
    )


def _call_looks_like_source_fetch(call):
    """``fetch_all()`` / ``load_pages()`` / ``get_source_items()`` style calls."""
    if not isinstance(call, ast.Call):
        return False
    name = (_called_name(call) or '').lower()
    if not name:
        if isinstance(call.func, ast.Attribute):
            name = str(call.func.attr or '').lower()
    if _looks_like_source_iter_name(name):
        return True
    return any(token in name for token in (
        'fetch', 'load', 'query', 'scan', 'page', 'read', 'source',
        'get_all', 'list_', 'pull', 'download', 'search', 'crawl',
    ))


def _call_arg_is_discovery_iterable(arg, functions=None, method_aliases=None):
    """True when a call argument is a discovery-shaped iterable expression."""
    functions = functions or {}
    method_aliases = method_aliases or set()
    if isinstance(arg, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
        return _comprehension_is_discovery(arg)
    # Bare source buffer: list(source_items) / sorted(source_pages, key=...)
    if isinstance(arg, ast.Name) and _looks_like_source_iter_name(arg.id):
        return True
    if isinstance(arg, ast.Attribute) and _looks_like_source_iter_name(arg.attr):
        return True
    if _call_looks_like_source_fetch(arg):
        return True
    if _is_map_family_call(arg) or _is_bulk_materialize_call(arg, functions, method_aliases):
        return True
    if _is_fold_call(arg) and _fold_builds_entity_buffer(arg, functions, method_aliases):
        return True
    return False


def _is_bulk_materialize_call(node, functions=None, method_aliases=None):
    """``list(chain.from_iterable(...))`` / ``list(map(...))`` / ``list(genexp)``."""
    functions = functions or {}
    method_aliases = method_aliases or set()
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == 'from_iterable':
        return True
    # dict.fromkeys(source_items) materializes keys then dumps.
    if isinstance(node.func, ast.Attribute) and node.func.attr == 'fromkeys':
        return any(
            _call_arg_is_discovery_iterable(arg, functions, method_aliases)
            for arg in node.args
        )
    if isinstance(node.func, ast.Attribute) and node.func.attr in (
            'deque', 'sorted'):
        return any(
            _call_arg_is_discovery_iterable(arg, functions, method_aliases)
            for arg in node.args
        )
    if _is_map_family_call(node):
        return True
    # list/tuple/set/sorted/deque(... discovery iterable ...)
    if isinstance(node.func, ast.Name) and _is_materialize_wrapper_name(node.func.id):
        return any(
            _call_arg_is_discovery_iterable(arg, functions, method_aliases)
            for arg in node.args
        )
    return False


def _is_starred_map_list(node):
    """``[*map(fn, items)]`` / ``[*starmap(...)]`` list display."""
    if not isinstance(node, ast.List) or len(node.elts) != 1:
        return False
    el = node.elts[0]
    return isinstance(el, ast.Starred) and _is_map_family_call(el.value)


def _fold_builds_entity_buffer(call, functions, method_aliases):
    """Heuristic: fold builds a collection of entities, not a scalar total."""
    if not _is_fold_call(call):
        return False
    # initial=[] / {} / set()
    if len(call.args) >= 3:
        init = call.args[2]
        if isinstance(init, (ast.List, ast.Dict, ast.Set)):
            return True
        if isinstance(init, ast.Constant) and init.value in ([], (), None):
            return True
        if isinstance(init, ast.Call) and isinstance(init.func, ast.Name) and init.func.id in (
                'list', 'dict', 'set'):
            return True
    for kw in call.keywords:
        if kw.arg in ('initial', 'initializer'):
            return True
    # reducer accumulates entities
    if call.args:
        reducer = call.args[0]
        if isinstance(reducer, ast.Lambda):
            return True
        if isinstance(reducer, ast.Name):
            helper = functions.get(reducer.id)
            if helper and _function_produces_entities(helper, functions, method_aliases):
                return True
    # Default: reduce over a multi-arg iterable is discovery-shaped when records
    # are deferred (callers still require outside records to fire).
    return len(call.args) >= 2


def _find_functional_accumulation_violations(tree, functions, method_aliases):
    """Flag reduce/accumulate/chain/map/genexp materialize then terminal dump."""
    if not _tree_has_record_events(tree, functions):
        return []
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        lineno = getattr(node, 'lineno', 0) or 0
        if _is_fold_call(value) and _fold_builds_entity_buffer(
                value, functions, method_aliases):
            violations.append(('ROW_LIVENESS_MISSING', lineno))
            continue
        if _is_bulk_materialize_call(value, functions, method_aliases):
            violations.append(('ROW_LIVENESS_MISSING', lineno))
            continue
        if _is_starred_map_list(value):
            violations.append(('ROW_LIVENESS_MISSING', lineno))
            continue
        # Bare discovery genexp/listcomp assigned then flushed later.
        if isinstance(value, (ast.GeneratorExp, ast.ListComp, ast.SetComp,
                              ast.DictComp)) and _comprehension_is_discovery(value):
            violations.append(('ROW_LIVENESS_MISSING', lineno))
    return violations


def _canary_visibility_violations(tree):
    violations = []
    for fn in [node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and 'canary' in node.name.lower()]:
        calls = sorted((node for node in ast.walk(fn) if isinstance(node, ast.Call)),
                       key=lambda node: (node.lineno, node.col_offset))
        mutations = [call for call in calls
                     if any((_called_name(call) or '').lower().startswith(word)
                            for word in MUTATION_CALL_WORDS)
                     and (_called_name(call) or '').lower() not in ('write_meta',)]
        if not mutations:
            continue
        first_mutation = mutations[0]
        records = [call for call in calls if _is_ledger_record_call(call)]
        step_before = any(isinstance(call.func, ast.Attribute) and call.func.attr == 'step'
                          and call.lineno <= first_mutation.lineno for call in calls)
        before = any(call.lineno < first_mutation.lineno for call in records)
        after = any(call.lineno > first_mutation.lineno for call in records)
        if not step_before and not (before and after):
            violations.append(('CANARY_VISIBILITY_MISSING', first_mutation.lineno))
    return violations


def _chat_control_violations(tree):
    return [
        ('CHAT_CONTROL_MISUSE', node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == 'read_chat'
    ]


def _loop_ranges_over_work(node, work_names):
    """Heuristic: is this for/while looping over something that looks like the
    work set (todo / items / companies / results.values() / futures)?"""
    it = None
    if isinstance(node, (ast.For, ast.AsyncFor)):
        it = node.iter
    elif isinstance(node, ast.While):
        return False  # while loops don't range over a known collection
    if it is None:
        return False
    src = ast.dump(it)
    for w in work_names:
        if w in src:
            return True
    # common iterables: results_by_vat.values(), todo, items, companies
    if any(x in src for x in ('.values()', 'as_completed', 'futures', 'results')):
        return True
    return False


def analyze(path):
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)

    work_names = {'todo', 'items', 'companies', 'contacts', 'rows', 'batch',
                  'results', 'futures', 'todo_list', 'work'}

    record_emit_sites = []  # (func_name, node)
    for node in ast.walk(tree):
        if _is_ledger_record_call(node):
            fn = _enclosing_function(tree, node)
            fname = fn.name if fn else '<module>'
            record_emit_sites.append((fname, node))

    parents = _parent_map(tree)

    # Find the loop(s) that mutate a results container (the "work" loop)
    work_entries = _find_result_mutating_loops(tree, work_names, parents)

    # Heartbeats (progress/metric/count) are a companion surface. Repeated
    # heartbeats from a slow loop must advance at least one stable entity or
    # phase row in that same loop path — not only a terminal record dump.
    row_liveness_violations = _find_row_liveness_violations(tree, parents)
    summary_violations = _summary_metric_violations(tree)
    limit_violations = _sample_limit_violations(tree)
    canary_violations = _canary_visibility_violations(tree)
    chat_control_violations = _chat_control_violations(tree)
    contract_violations = (row_liveness_violations + summary_violations +
                           limit_violations + canary_violations + chat_control_violations)

    # A result held only in memory after a provider phase is neither resumable
    # nor durable, even if the script emits lively progress heartbeats. Require
    # an apparent sink call from that loop (or a helper it invokes).
    durability_violations = [
        ('DURABILITY_MISSING', loop.lineno)
        for loop, buffers in work_entries
        if not _loop_has_durable_write(loop, tree, buffers, parents)
    ]

    if not record_emit_sites:
        return durability_violations + contract_violations

    # A record emit is VALID only if it is inside a work loop (the same loop that
    # mutates results), or inside a function called from a work loop.
    violations = []
    for fname, node in record_emit_sites:
        if 'canary' in fname.lower():
            continue
        # Buffer durability is checked per result-producing loop above. Record
        # placement is local: any repeated item loop (or helper it calls) streams.
        if _emit_in_any_work_loop(node, tree, work_names):
            continue
        violations.append((fname, node.lineno))

    return violations + durability_violations + contract_violations


def _enclosing_function(tree, node):
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if _node_in_func(fn, node):
            return fn
    return None


def _node_in_func(func, node):
    for n in ast.walk(func):
        if n is node:
            return True
    return False


def _find_result_mutating_loops(tree, work_names, parents):
    """Return every work loop paired with its mutated result-buffer names."""
    functions = _function_defs(tree)
    found = []
    for loop in [n for n in ast.walk(tree) if isinstance(n, LOOP_TYPES)]:
        if not _loop_ranges_over_work(loop, work_names):
            continue
        buffers = _result_buffers_mutated_in(loop, functions)
        if buffers and not _is_read_only_replay_loop(loop, buffers, parents):
            found.append((loop, buffers))
    return found


def _function_defs(tree):
    return {
        fn.name: fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _parent_map(tree):
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _body_nodes(node):
    """Walk a loop/function body while keeping nested control flow visible."""
    stack = list(getattr(node, 'body', [])) + list(getattr(node, 'orelse', []))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _function_emits_event(fn, event, functions, seen=None, aliases=None,
                          attr_aliases=None):
    seen = set(seen or ())
    if fn.name in seen:
        return False
    seen.add(fn.name)
    aliases = aliases or set()
    attr_aliases = attr_aliases or set()
    for node in _body_nodes(fn):
        if not isinstance(node, ast.Call):
            continue
        if event == 'heartbeat':
            if _is_heartbeat_call(
                    node, aliases=aliases, functions=functions,
                    attr_aliases=attr_aliases):
                return True
        elif _is_ledger_event_call(node, event):
            return True
        helper = functions.get(_called_name(node))
        if helper and _function_emits_event(
                helper, event, functions, seen, aliases=aliases,
                attr_aliases=attr_aliases):
            return True
    return False


def _loop_emits_event(loop, event, functions, aliases=None, attr_aliases=None):
    aliases = aliases or set()
    attr_aliases = attr_aliases or set()
    for node in _body_nodes(loop):
        if not isinstance(node, ast.Call):
            continue
        if event == 'heartbeat':
            if _is_heartbeat_call(
                    node, aliases=aliases, functions=functions,
                    attr_aliases=attr_aliases):
                return True
        elif _is_ledger_event_call(node, event):
            return True
        helper = functions.get(_called_name(node))
        if helper and _function_emits_event(
                helper, event, functions, aliases=aliases,
                attr_aliases=attr_aliases):
            return True
    return False


def _ancestor_loop_has_record_kinds(loop, parents, functions):
    """True when an enclosing loop already streams record rows (nested pagination)."""
    current = parents.get(loop)
    while current is not None:
        if isinstance(current, LOOP_TYPES):
            if _loop_record_kinds(current, functions):
                return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            break
        current = parents.get(current)
    return False


def _tree_has_record_events(tree, functions):
    """True when any ledger record emit exists (module or function body)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_ledger_record_call(node):
            return True
    for fn in functions.values():
        if _function_record_kinds(fn, functions):
            return True
    return False


def _is_parallel_map_call(node):
    """``map(fn, items)`` / ``executor.map(fn, items)`` / ``pool.imap(...)``."""
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return False
    if isinstance(node.func, ast.Name) and node.func.id == 'map':
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr in _PARALLEL_MAP_ATTRS:
        return True
    return False


def _parallel_map_worker(node, functions):
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Name):
        return functions.get(first.id)
    return None


def _find_row_liveness_violations(tree, parents=None):
    """Flag slow loops that leave the Data table empty until a later dump.

    Triggers when a loop:
    - emits heartbeats (progress/metric/count, aliases, getattr, tick helpers)
      without a record-row path, or
    - accumulates entities with no record path and no enclosing loop that
      already emits records (silent discovery → terminal planned dump).

    Nested pagination under a parent that records, and read-only file replay
    loops, are exempt from the silent-accumulation rule.

    Also flags multi-source comprehension builders and parallel map/executor
    fan-out that accumulate without streaming records.
    """
    parents = parents or _parent_map(tree)
    functions = _function_defs(tree)
    aliases, attr_aliases = _heartbeat_aliases(tree)
    method_aliases = _collection_method_aliases(tree)
    violations = []
    for loop in ast.walk(tree):
        if not isinstance(loop, LOOP_TYPES):
            continue
        kinds = _loop_record_kinds(loop, functions)
        has_heartbeat = _loop_emits_event(
            loop, 'heartbeat', functions, aliases=aliases,
            attr_aliases=attr_aliases)
        produces = _loop_produces_entities(loop, functions, method_aliases)
        if has_heartbeat:
            if not kinds:
                violations.append(('ROW_LIVENESS_MISSING', loop.lineno))
            elif kinds == {'phase'} and produces:
                violations.append(('BUSINESS_ROW_LIVENESS_MISSING', loop.lineno))
        elif produces and not kinds:
            if _ancestor_loop_has_record_kinds(loop, parents, functions):
                continue
            if _read_source_loop(loop, parents) is not None:
                continue
            violations.append(('ROW_LIVENESS_MISSING', loop.lineno))
    # Comprehension-only builders: no For/While, but discovery is still silent
    # when records are dumped later (including module-level flush loops).
    if _tree_has_record_events(tree, functions):
        for fn in functions.values():
            if not _function_comprehension_discovery(fn):
                continue
            if _function_record_kinds(fn, functions):
                continue
            violations.append(('ROW_LIVENESS_MISSING', fn.lineno))
        # Module-level discovery comps assigned then flushed.
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            # Skip assigns inside functions (handled above via function body).
            parent = parents.get(node)
            in_fn = False
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    in_fn = True
                    break
                if isinstance(parent, ast.Module):
                    break
                parent = parents.get(parent)
            if in_fn:
                continue
            value = node.value
            if _comprehension_is_discovery(value):
                violations.append(('ROW_LIVENESS_MISSING', node.lineno))
    # Parallel map/executor fan-out: no for/while, silent accumulation in worker.
    if _tree_has_record_events(tree, functions):
        for node in ast.walk(tree):
            if not _is_parallel_map_call(node):
                continue
            worker = _parallel_map_worker(node, functions)
            if worker is None:
                continue
            if _function_record_kinds(worker, functions):
                continue
            if _function_produces_entities(worker, functions, method_aliases):
                violations.append(('ROW_LIVENESS_MISSING', node.lineno))
    # Functional folds / bulk materialize then terminal dump (reduce, chain, …).
    violations.extend(
        _find_functional_accumulation_violations(tree, functions, method_aliases)
    )
    return violations


def _root_names(node):
    """Return base names for receivers such as results[key].append(...)."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _root_names(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _root_names(node.func.value)
    return set()


def _target_names(node):
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names = set()
        for item in node.elts:
            names.update(_target_names(item))
        return names
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return set()


def _is_read_only_open(call):
    if not isinstance(call, ast.Call) or _called_name(call) != 'open':
        return False
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for keyword in call.keywords:
        if keyword.arg == 'mode':
            mode = keyword.value
    if mode is None:
        return True
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return False
    return not any(flag in mode.value for flag in ('w', 'a', 'x', '+'))


def _read_source_loop(loop, parents):
    current = loop
    while current is not None:
        if isinstance(current, (ast.For, ast.AsyncFor)):
            if any(_is_read_only_open(node) for node in ast.walk(current.iter)):
                return current
        current = parents.get(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda, ast.ClassDef)):
            break
    return None


def _is_read_derived_expr(node, names):
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Attribute):
        return _is_read_derived_expr(node.value, names)
    if isinstance(node, ast.Subscript):
        return (_is_read_derived_expr(node.value, names)
                and _is_read_derived_expr(node.slice, names))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_read_derived_expr(item, names) for item in node.elts)
    if isinstance(node, ast.Dict):
        values = [item for item in node.keys + node.values if item is not None]
        return all(_is_read_derived_expr(item, names) for item in values)
    if isinstance(node, (ast.BoolOp, ast.BinOp, ast.Compare)):
        return all(_is_read_derived_expr(child, names)
                   for child in ast.iter_child_nodes(node)
                   if isinstance(child, ast.expr))
    if isinstance(node, ast.UnaryOp):
        return _is_read_derived_expr(node.operand, names)
    if isinstance(node, ast.IfExp):
        return all(_is_read_derived_expr(item, names)
                   for item in (node.test, node.body, node.orelse))
    if isinstance(node, ast.Call):
        if (_called_name(node) or '').lower() not in READ_DERIVATION_CALLS:
            return False
        values = list(node.args) + [kw.value for kw in node.keywords]
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        if receiver is not None and _is_read_derived_expr(receiver, names):
            return all(_is_read_derived_expr(value, names) for value in values)
        return bool(values) and all(_is_read_derived_expr(value, names)
                                    for value in values)
    return False


def _read_derived_names(read_loop, loop, parents):
    loop_chain = []
    current = loop
    while current is not None:
        if isinstance(current, (ast.For, ast.AsyncFor)):
            loop_chain.append(current)
        if current is read_loop:
            break
        current = parents.get(current)
    if not loop_chain or loop_chain[-1] is not read_loop:
        return set()
    loop_chain.reverse()

    names = _target_names(read_loop.target)

    changed = True
    while changed:
        changed = False
        for node in _body_nodes(read_loop):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if _is_read_derived_expr(value, names):
                    before = len(names)
                    for target in targets:
                        names.update(_target_names(target))
                    changed = changed or len(names) != before
        for nested_loop in loop_chain[1:]:
            if _is_read_derived_expr(nested_loop.iter, names):
                before = len(names)
                names.update(_target_names(nested_loop.target))
                changed = changed or len(names) != before
    return names


def _buffer_mutation_values(loop, buffers):
    values = []
    for node in _body_nodes(loop):
        if isinstance(node, ast.Assign):
            if any(_root_names(target) & buffers for target in node.targets):
                values.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if _root_names(node.target) & buffers and node.value is not None:
                values.append(node.value)
        elif isinstance(node, ast.AugAssign):
            if _root_names(node.target) & buffers:
                values.append(node.value)
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr in COLLECTION_MUTATIONS
              and _root_names(node.func.value) & buffers):
            values.extend(node.args)
            values.extend(keyword.value for keyword in node.keywords)
    return values


def _is_read_only_replay_loop(loop, buffers, parents):
    """Recognize a loop that only restores values from a read-only file.

    The exemption is deliberately narrow: the loop must sit under a read-mode
    open(), and every value copied into the result buffer must derive from that
    read. A provider/transform result assigned to a new local does not qualify.
    """
    read_loop = _read_source_loop(loop, parents)
    if read_loop is None:
        return False
    names = _read_derived_names(read_loop, loop, parents)
    values = _buffer_mutation_values(loop, set(buffers))
    return bool(values) and all(_is_read_derived_expr(value, names)
                                for value in values)


def _is_result_buffer(name):
    return 'result' in name.lower()


def _assignment_roots(node):
    targets = []
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    roots = set()
    for target in targets:
        if isinstance(target, ast.Subscript):
            roots.update(_root_names(target.value))
    return roots


def _function_parameters(fn):
    return [arg.arg for arg in (list(fn.args.posonlyargs) + list(fn.args.args) +
                                list(fn.args.kwonlyargs))]


def _call_bindings(call, fn):
    params = _function_parameters(fn)
    bindings = {}
    for index, value in enumerate(call.args):
        if index < len(params):
            bindings[params[index]] = _root_names(value)
    for keyword in call.keywords:
        if keyword.arg:
            bindings[keyword.arg] = _root_names(keyword.value)
    return bindings


def _mutated_parameters(fn, functions, seen=None):
    """Find helper parameters that ultimately receive collection mutations."""
    seen = set(seen or ())
    if fn.name in seen:
        return set()
    seen.add(fn.name)
    params = set(_function_parameters(fn))
    mutated = set()
    for node in _body_nodes(fn):
        mutated.update(_assignment_roots(node) & params)
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in COLLECTION_MUTATIONS:
            mutated.update(_root_names(node.func.value) & params)
        called = _called_name(node)
        child = functions.get(called)
        if child:
            bindings = _call_bindings(node, child)
            for child_param in _mutated_parameters(child, functions, seen):
                mutated.update(bindings.get(child_param, set()) & params)
    return mutated


def _result_buffers_mutated_in(loop, functions):
    buffers = set()
    for node in _body_nodes(loop):
        buffers.update(name for name in _assignment_roots(node) if _is_result_buffer(name))
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in COLLECTION_MUTATIONS:
            buffers.update(name for name in _root_names(node.func.value)
                           if _is_result_buffer(name))
        called = _called_name(node)
        helper = functions.get(called)
        if helper:
            bindings = _call_bindings(node, helper)
            for parameter in _mutated_parameters(helper, functions):
                buffers.update(name for name in bindings.get(parameter, set())
                               if _is_result_buffer(name))
    return buffers


def _emit_in_any_work_loop(node, tree, _work_names):
    """Without a detected buffer, any repeated loop is an incremental emit."""
    for loop in [n for n in ast.walk(tree) if isinstance(n, LOOP_TYPES)]:
        if _node_in_loop_body(loop, node):
            return True
    fn = _enclosing_function(tree, node)
    if fn:
        for loop in [n for n in ast.walk(tree) if isinstance(n, LOOP_TYPES)]:
            if _func_called_from_loop_body(loop, fn.name):
                return True
    return False


def _loop_has_durable_write(loop, tree, buffers, parents):
    """Return whether a result-mutating loop appears to persist its result.

    This is intentionally a conservative static heuristic. A helper such as
    append_result(), save_row(), write_to_sheet(), or a direct file/database
    write is enough to pass. A nested pagination loop may use a later write in
    its enclosing item/chunk iteration. Progress(), count(), checkpoint(), and
    ledger() are observability only and deliberately do not count.
    """
    functions = _function_defs(tree)
    for call in [n for n in _body_nodes(loop) if isinstance(n, ast.Call)]:
        if _call_reaches_durable_write(call, functions, buffers):
            return True
    for statement in _later_statements_in_enclosing_iteration(loop, parents):
        for call in [n for n in _executable_nodes(statement)
                     if isinstance(n, ast.Call)]:
            if _call_reaches_durable_write(call, functions, buffers):
                return True
    return False


def _call_reaches_durable_write(call, functions, buffers):
    helper = functions.get(_called_name(call))
    if helper:
        return _helper_has_durable_write(helper, functions)
    return _is_durable_write_call(call, buffers)


def _executable_nodes(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _has_enclosing_iteration(node, parents):
    current = node
    while current is not None:
        if isinstance(current, LOOP_TYPES):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda, ast.ClassDef, ast.Module)):
            return False
        current = parents.get(current)
    return False


def _later_statements_in_enclosing_iteration(node, parents):
    """Yield later siblings while execution remains in an outer loop iteration.

    This lets an inner page/result loop use the chunk-level persist that follows
    it, while stopping before function/module siblings such as a final flush.
    """
    current = node
    while current is not None:
        parent = parents.get(current)
        if parent is None or isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                         ast.ClassDef, ast.Module)):
            return
        if _has_enclosing_iteration(parent, parents):
            for _field, value in ast.iter_fields(parent):
                if isinstance(value, list) and current in value:
                    index = value.index(current)
                    for statement in value[index + 1:]:
                        if isinstance(statement, ast.AST):
                            yield statement
        current = parent


def _called_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_durable_write_call(call, buffers=frozenset()):
    name = (_called_name(call) or '').lower()
    if name in OBSERVABILITY_CALLS:
        return False
    if isinstance(call.func, ast.Attribute):
        receiver_roots = _root_names(call.func.value)
        if receiver_roots & set(buffers):
            return False
        # Python collection methods describe memory mutation. A durable helper
        # such as append_jsonl(...) remains detectable as a named function.
        if name in COLLECTION_MUTATIONS:
            return False
    return any(word in name for word in DURABLE_WORDS)


def _helper_has_durable_write(fn, functions, seen=None):
    seen = set(seen or ())
    if fn.name in seen:
        return False
    seen.add(fn.name)
    buffer_params = _mutated_parameters(fn, functions)
    for call in [n for n in _body_nodes(fn) if isinstance(n, ast.Call)]:
        called = _called_name(call)
        child = functions.get(called)
        if child and _helper_has_durable_write(child, functions, seen):
            return True
        if child is None and _is_durable_write_call(call, buffer_params):
            return True
    return False


def _node_in_loop_body(loop, node):
    iterator = getattr(loop, 'iter', None)
    for child in ast.iter_child_nodes(loop):
        if child is iterator:
            continue
        for n in ast.walk(child):
            if n is node:
                return True
    return False


def _func_called_from_loop_body(loop, fname):
    for call in [n for n in ast.walk(loop) if isinstance(n, ast.Call)]:
        called = None
        if isinstance(call.func, ast.Name):
            called = call.func.id
        elif isinstance(call.func, ast.Attribute):
            called = call.func.attr
        if called == fname:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(
        description='Lint for buffered output, row liveness, headline summaries, and durable work-loop writes')
    ap.add_argument('script')
    args = ap.parse_args()

    try:
        violations = analyze(args.script)
    except SyntaxError as e:
        print(f'SYNTAX ERROR in {args.script}: {e}')
        sys.exit(2)

    if not violations:
        print(f'OK - {args.script}: No common buffered-output, ROW_LIVENESS_MISSING, SUMMARY_METRICS_MISSING, or DURABILITY_MISSING violation detected.')
        print('  Static analysis is heuristic; confirm the durable boundary with a forced crash/resume sample.')
        sys.exit(0)

    print(f'VIOLATION in {args.script}: an observability or durability contract is missing.')
    if any(kind == 'DURABILITY_MISSING' for kind, _ in violations):
        print('  DURABILITY MISSING: a results container is populated in a work loop,')
        print('  but no durable result write is visible there. Progress events do not')
        print('  protect paid work from a crash or make --resume skip it.')
    if any(kind == 'ROW_LIVENESS_MISSING' for kind, _ in violations):
        print('  ROW LIVENESS MISSING: a repeated loop emits progress/metric/count')
        print('  heartbeats while its record/table surface stays unchanged. A terminal')
        print('  preview dump leaves the operator with an empty table during the')
        print('  entire slow discovery phase (often many minutes).')
    if any(kind == 'BUSINESS_ROW_LIVENESS_MISSING' for kind, _ in violations):
        print('  BUSINESS ROW LIVENESS MISSING: a repeated loop discovers or')
        print('  accumulates entities, but only a synthetic phase row advances.')
        print('  The operator cannot inspect representative business data live.')
    if any(kind == 'SAMPLE_LIMIT_LATE' for kind, _ in violations):
        print('  SAMPLE LIMIT LATE: --limit has no apparent path into a source')
        print('  query, page, batch, iterator, or source-loop stop condition.')
    if any(kind == 'CANARY_VISIBILITY_MISSING' for kind, _ in violations):
        print('  CANARY VISIBILITY MISSING: a canary mutates before a business')
        print('  row becomes visible and does not show the full verification transition.')
    if any(kind == 'CHAT_CONTROL_MISUSE' for kind, _ in violations):
        print('  CHAT CONTROL MISUSE: the worker reads free-form dashboard chat.')
        print('  Worker control belongs on the structured controls channel.')
    if any(kind == 'SUMMARY_METRICS_MISSING' for kind, _ in violations):
        print('  SUMMARY METRICS MISSING: the run start has no explicit headline')
        print('  metric selection, so useful terminal totals can remain outside the')
        print('  dashboard summary strip.')
    named_contracts = ('DURABILITY_MISSING', 'ROW_LIVENESS_MISSING',
                       'BUSINESS_ROW_LIVENESS_MISSING', 'SUMMARY_METRICS_MISSING',
                       'SAMPLE_LIMIT_LATE', 'CANARY_VISIBILITY_MISSING',
                       'CHAT_CONTROL_MISUSE')
    if any(kind not in named_contracts
           for kind, _ in violations):
        print('  RECORD EMIT MISSING: record ledger events are outside the work loop.')
    print()
    if any(kind == 'DURABILITY_MISSING' for kind, _ in violations):
        print('  Durability fix: persist the result and emit its record in the same item')
        print('  loop or completion callback, then checkpoint after that boundary.')
    if any(kind == 'ROW_LIVENESS_MISSING' for kind, _ in violations):
        print('  Liveness fix: emit a stable entity or phase record from each slow')
        print('  discovery/progress loop (especially dry-run planned rows), then')
        print('  update that same table/key as later fields become known. Do not')
        print('  wait until build_targets() returns to flush every planned record.')
    if any(kind == 'BUSINESS_ROW_LIVENESS_MISSING' for kind, _ in violations):
        print('  Business-row fix: emit representative entity rows as discovery or')
        print('  classification lands; reserve phase rows for phases with no entity yet.')
    if any(kind == 'SAMPLE_LIMIT_LATE' for kind, _ in violations):
        print('  Sample fix: thread --limit into the earliest source query/page/batch')
        print('  and stop discovery when the representative sample reaches its budget.')
    if any(kind == 'CANARY_VISIBILITY_MISSING' for kind, _ in violations):
        print('  Canary fix: update one stable business row through selected, writing,')
        print('  verifying, and verified or failed states around the mutation.')
    if any(kind == 'CHAT_CONTROL_MISUSE' for kind, _ in violations):
        print('  Control fix: use run.check_controls() at durable boundaries and leave')
        print('  chat messages for the active agent session.')
    if any(kind == 'SUMMARY_METRICS_MISSING' for kind, _ in violations):
        print('  Summary fix: select three to five summary_metrics at run start and')
        print('  emit matching scalar numeric fields on the terminal event.')
    if any(kind not in named_contracts
           for kind, _ in violations):
        print('  Record fix: emit each record from its item loop or completion callback.')
    print()
    for fname, lineno in violations:
        if fname == 'ROW_LIVENESS_MISSING':
            print(f'  - heartbeat loop at line {lineno} has no record-row path')
        elif fname == 'BUSINESS_ROW_LIVENESS_MISSING':
            print(f'  - entity-producing progress loop at line {lineno} emits only phase rows')
        elif fname == 'SUMMARY_METRICS_MISSING':
            print(f'  - run start at line {lineno} has no summary_metrics selection')
        elif fname == 'SAMPLE_LIMIT_LATE':
            print(f'  - --limit declared at line {lineno} does not bound apparent source work')
        elif fname == 'CANARY_VISIBILITY_MISSING':
            print(f'  - canary mutation at line {lineno} lacks a visible row transition')
        elif fname == 'CHAT_CONTROL_MISUSE':
            print(f'  - free-form chat is read as worker input at line {lineno}')
        elif fname != 'DURABILITY_MISSING' and isinstance(lineno, int):
            print(f'  - record emit in {fname}() at line {lineno} is outside any work loop')
    print()
    print('  See SKILL.md > "4. Wire The Harness" and "5. Prove The Sample".')
    sys.exit(1)


if __name__ == '__main__':
    main()
