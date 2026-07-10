#!/usr/bin/env python3
"""Lint an agent-written batch script for two observer-kit violations:

    buffering all provider results in memory and emitting `record` ledger
    rows only in a final flush block (instead of as work lands).

    reporting progress while the actual result remains memory-only until a
    final write. A live dashboard is not a durable resume point.

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


def _is_ledger_record_call(node):
    """Return True if `node` is a call that emits a 'record' ledger event."""
    if not isinstance(node, ast.Call):
        return False
    # ledger(scope, 'record', ...)
    if isinstance(node.func, ast.Name) and node.func.id == 'ledger':
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            return node.args[1].value in RECORD_EVENTS
    # run.step(..., event='record') or run.step('record', ...)
    if isinstance(node.func, ast.Attribute) and node.func.attr in ('step', 'record'):
        # check positional or keyword for 'record'
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in RECORD_EVENTS:
            return True
        for kw in node.keywords:
            if kw.arg in ('event', 'table') and isinstance(kw.value, ast.Constant) and kw.value.value in RECORD_EVENTS:
                return True
    return False


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
    work_loops = [loop for loop, _buffers in work_entries]

    # A result held only in memory after a provider phase is neither resumable
    # nor durable, even if the script emits lively progress heartbeats. Require
    # an apparent sink call from that loop (or a helper it invokes).
    durability_violations = [
        ('DURABILITY_MISSING', loop.lineno)
        for loop, buffers in work_entries
        if not _loop_has_durable_write(loop, tree, buffers, parents)
    ]

    if not record_emit_sites:
        return durability_violations

    # A record emit is VALID only if it is inside a work loop (the same loop that
    # mutates results), or inside a function called from a work loop.
    violations = []
    for fname, node in record_emit_sites:
        if not work_loops:
            # No buffering detected — emit-in-any-work-loop is the correct pattern.
            if _emit_in_any_work_loop(node, tree, work_names):
                continue
        else:
            if _emit_in_work_loop(node, tree, work_loops, work_names):
                continue
        violations.append((fname, node.lineno))

    return violations + durability_violations


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


def _emit_in_work_loop(node, tree, work_loops, work_names):
    """Emit is valid if it is lexically inside a work loop, or inside a function
    called from a work loop."""
    for loop in work_loops:
        if _node_in_loop_body(loop, node):
            return True
    fn = _enclosing_function(tree, node)
    if fn:
        for loop in work_loops:
            if _func_called_from_loop_body(loop, fn.name):
                return True
    return False


def _emit_in_any_work_loop(node, tree, work_names):
    """Emit is valid if it is lexically inside any loop that ranges over work
    items (no buffering detected, so live emit from the work loop is correct)."""
    for loop in [n for n in ast.walk(tree) if isinstance(n, LOOP_TYPES)]:
        if _node_in_loop_body(loop, node) and _loop_ranges_over_work(loop, work_names):
            return True
    fn = _enclosing_function(tree, node)
    if fn:
        for loop in [n for n in ast.walk(tree) if isinstance(n, LOOP_TYPES)]:
            if _loop_ranges_over_work(loop, work_names) and _func_called_from_loop_body(loop, fn.name):
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
    for child in ast.iter_child_nodes(loop):
        if child is loop.iter:
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
        description='Lint for buffered output and missing durable work-loop writes')
    ap.add_argument('script')
    args = ap.parse_args()

    try:
        violations = analyze(args.script)
    except SyntaxError as e:
        print(f'SYNTAX ERROR in {args.script}: {e}')
        sys.exit(2)

    if not violations:
        print(f'OK - {args.script}: No common buffered-output violation detected.')
        print('  Static analysis is heuristic; confirm the durable boundary with a forced crash/resume sample.')
        sys.exit(0)

    print(f'VIOLATION in {args.script}: incremental observability or durability is missing.')
    if any(kind == 'DURABILITY_MISSING' for kind, _ in violations):
        print('  DURABILITY MISSING: a results container is populated in a work loop,')
        print('  but no durable result write is visible there. Progress events do not')
        print('  protect paid work from a crash or make --resume skip it.')
    if any(kind != 'DURABILITY_MISSING' for kind, _ in violations):
        print('  RECORD EMIT MISSING: record ledger events are outside the work loop.')
    print()
    print('  Fix: persist the result and emit its record in the same item loop or')
    print('  completion callback, then checkpoint only after that durable boundary.')
    print()
    for fname, lineno in violations:
        if fname != 'DURABILITY_MISSING' and isinstance(lineno, int):
            print(f'  - record emit in {fname}() at line {lineno} is outside any work loop')
    print()
    print('  See SKILL.md > "4. Wire The Harness" and "5. Prove The Sample".')
    sys.exit(1)


if __name__ == '__main__':
    main()
