"""Running-time and design-choice questions.

Running time is asked only when a known pattern fires and nothing unknown sits in the loop
(docs/QUESTION_ENGINE.md, section 6): a growing list searched with `in`, two nested loops
over the same input, a halving search, a recursion that calls itself twice. Options are
tight bounds (Θ), so exactly one is true. Costs of built-in operations come from the
Python wiki's TimeComplexity page.

Every detector refuses code it can't fully account for: an early exit it didn't expect, a
loop variable that's reassigned, a call it doesn't know the cost of, a container with a
size limit. Fewer questions is acceptable; a question with a shaky key is not.

Design questions are counterfactuals: the agent's intent can't be observed, but what a
different choice would change can be derived. "Why a set?" becomes "what does the set buy
here, compared with a list?", and the answer follows from the same cost table.
"""
import ast

from pyfacts import (CORRECTNESS, DESIGN, RUNNING_TIME, add_spec, dotted, int_constant, short, src)

SCANNED = {"list", "deque", "tuple"}
HASHED = {"set", "frozenset", "dict", "defaultdict", "Counter"}
REITERABLE = {"list", "tuple", "set", "frozenset", "dict", "deque", "defaultdict", "Counter"}
ELEMENTARY = {"len", "abs", "int", "float", "bool", "round", "ord", "chr", "isinstance"}
SCALAR_ONLY = {"print", "str", "repr"}       # Θ(1) only for a single item, not for a container
ELEMENT_METHODS = {"lower", "upper", "strip", "lstrip", "rstrip", "casefold", "title", "startswith", "endswith",
                   "isdigit", "isalpha", "isalnum", "isspace", "split", "replace", "encode", "decode"}
APPENDS = {"append", "add", "appendleft"}    # amortized Θ(1) on another container
GROWTH = {"append", "add", "appendleft"}
SHRINK_OR_BULK = {"remove", "pop", "popleft", "discard", "clear", "insert", "extend", "update", "sort", "reverse"}
EMPTY_CALLS = {"list", "set", "dict", "deque", "collections.deque", "Counter", "collections.Counter", "frozenset"}
EXIT_CALLS = ("sys.exit", "exit", "quit", "os._exit")


def _is_empty(value):
    if isinstance(value, (ast.List, ast.Set, ast.Tuple)) and not value.elts:
        return True
    if isinstance(value, ast.Dict) and not value.keys:
        return True
    if isinstance(value, ast.Call) and not value.keywords:
        d = dotted(value.func)
        if d in EMPTY_CALLS and not value.args:
            return True
        if d in ("defaultdict", "collections.defaultdict") and len(value.args) == 1 and isinstance(value.args[0], ast.Name):
            return True
    return False


def _walk_body(stmts):
    return ast.walk(ast.Module(body=list(stmts), type_ignores=[]))


def _assigned_names(stmts):
    out = set()
    for n in _walk_body(stmts):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            out |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
        elif isinstance(n, ast.NamedExpr):
            out.add(n.target.id)
    return out


def _mutated(name, stmts):
    for n in _walk_body(stmts):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == name and n.func.attr in GROWTH | SHRINK_OR_BULK:
            return True
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) \
                and isinstance(n.value, ast.Name) and n.value.id == name:
            return True
    return False


def _is_exit(n):
    return isinstance(n, (ast.Return, ast.Break, ast.Continue, ast.Raise)) or (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and dotted(n.value.func) in EXIT_CALLS)


# ----------------------------------------------------------------- the "seen" loop

def _membership_loops(m, changed):
    """Loops of the form `for x in A: ... x in S ... S.append(x)` where S starts empty and
    grows by at most one item per pass, the only early exit is "found a repeat", and nothing
    else in the loop costs more than Θ(1)."""
    out = []
    for loop in (n for n in ast.walk(m.tree) if isinstance(n, ast.For)):
        if not (isinstance(loop.target, ast.Name) and isinstance(loop.iter, ast.Name)) or loop.orelse:
            continue
        if m.inside_loop(loop) is not None:
            continue                                   # an outer loop could make S grow across passes
        x, a = loop.target.id, loop.iter.id
        assigned = _assigned_names(loop.body)
        if a in assigned or x in assigned or _mutated(a, loop.body):
            continue
        scope = m.scope_of(loop)
        kinds = m.stable_kinds(scope)
        compare = None
        for n in _walk_body(loop.body):
            if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.In, ast.NotIn)) \
                    and isinstance(n.left, ast.Name) and n.left.id == x and isinstance(n.comparators[0], ast.Name) \
                    and n.comparators[0].id in kinds and kinds[n.comparators[0].id][0] in SCANNED | HASHED:
                compare = n
                break
        if compare is None:
            continue
        s = compare.comparators[0].id
        kind, init_line = kinds[s]
        if init_line >= loop.lineno or kind == "tuple" or s in assigned:
            continue
        init = next((n for n in m.own_nodes(scope) if isinstance(n, ast.Assign) and n.lineno == init_line), None)
        if init is None or not _is_empty(init.value):
            continue
        filled_early = any(init_line < n.lineno < loop.lineno and (
            (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and dotted(n.func.value) == s)
            or (isinstance(n, ast.Subscript) and dotted(n.value) == s and isinstance(n.ctx, ast.Store)))
            for n in m.own_nodes(scope) if hasattr(n, "lineno"))
        if filled_early or not ({loop.lineno, compare.lineno} & changed):
            continue                                   # S must still be empty when the loop starts
        # the If whose test is the membership compare, directly in the loop body
        test_if = next((st for st in loop.body if isinstance(st, ast.If) and st.test is compare), None)
        positive = isinstance(compare.ops[0], ast.In)
        # exits: only "found it" exits inside `if x in S:`
        bad_exit = False
        for n in _walk_body(loop.body):
            if isinstance(n, (ast.Try, ast.With, ast.AsyncWith)):
                bad_exit = True
            if _is_exit(n):
                inside_found = (test_if is not None and positive and not test_if.orelse
                                and any(n is b or n in list(ast.walk(b)) for b in test_if.body))
                if not inside_found:
                    bad_exit = True
        if bad_exit:
            continue
        # one growth site, by the loop variable
        growth, bad = [], False
        for n in _walk_body(loop.body):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                    and n.func.value.id == s:
                if n.func.attr in GROWTH and len(n.args) == 1 and isinstance(n.args[0], ast.Name) and n.args[0].id == x:
                    growth.append(n)
                else:
                    bad = True
            elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == s:
                if isinstance(n.ctx, ast.Store) and kind in ("dict", "defaultdict", "Counter") \
                        and isinstance(n.slice, ast.Name) and n.slice.id == x:
                    growth.append(n)
                else:
                    bad = True
        if bad or len(growth) != 1:
            continue
        g = growth[0]
        stmt = g
        while m.parents.get(stmt) is not None and m.parents[stmt] is not loop:
            stmt = m.parents[stmt]
        unconditional = stmt in loop.body and not isinstance(stmt, ast.If)
        guarded = (test_if is not None and not positive and stmt is test_if
                   and any(g in list(ast.walk(b)) for b in test_if.body))
        if not (unconditional or guarded):
            continue
        # does every repeat skip the growth? (then S never holds duplicates)
        no_duplicates = guarded or (test_if is not None and positive and test_if.body and _is_exit(test_if.body[-1])
                                    and loop.body.index(test_if) < loop.body.index(stmt))
        if not _elementary_body(m, loop, s, compare, g, kinds, x):
            continue
        out.append({"loop": loop, "x": x, "a": a, "s": s, "kind": kind, "compare": compare, "growth": g,
                    "init_line": init_line, "scope": scope, "no_duplicates": no_duplicates})
    return out


def _scalar_arg(arg, x):
    """An argument whose size doesn't grow with the input: the loop item, a constant, or an
    f-string / attribute built only from those."""
    if isinstance(arg, ast.Constant):
        return True
    if isinstance(arg, ast.Name):
        return arg.id == x
    if isinstance(arg, ast.Attribute):
        return _scalar_arg(arg.value, x)
    if isinstance(arg, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) or (isinstance(v, ast.FormattedValue) and _scalar_arg(v.value, x))
                   for v in arg.values)
    return False


def _elementary_body(m, loop, s, compare, growth, kinds, x):
    """Nothing in the loop body costs more than Θ(1) per pass, apart from `compare`."""
    for n in _walk_body(loop.body):
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp,
                          ast.GeneratorExp, ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.Await)):
            return False
        if isinstance(n, ast.Compare) and n is not compare:
            for op, right in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    if isinstance(right, (ast.Tuple, ast.List, ast.Set)) and all(isinstance(e, ast.Constant) for e in right.elts):
                        continue
                    if isinstance(right, ast.Name) and kinds.get(right.id, (None,))[0] in HASHED:
                        continue
                    return False
        if isinstance(n, ast.BinOp):
            for side in (n.left, n.right):
                if isinstance(side, (ast.List, ast.Tuple, ast.ListComp)) or (
                        isinstance(side, ast.Name) and kinds.get(side.id, (None,))[0] in SCANNED):
                    return False                         # `out = out + [x]` copies the whole list
        if isinstance(n, ast.AugAssign):
            if not (isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float))):
                return False
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.BinOp):
            acc = n.targets[0].id
            if acc in (dotted(n.value.left), dotted(n.value.right)):
                other = n.value.right if dotted(n.value.left) == acc else n.value.left
                if not (isinstance(other, ast.Constant) and isinstance(other.value, (int, float))):
                    return False                 # `s = s + x` may rebuild a growing string every pass
        if isinstance(n, ast.Name) and n.id == s and isinstance(n.ctx, ast.Load):
            p = m.parents.get(n)
            ok = (p is compare or (isinstance(p, ast.Attribute) and m.parents.get(p) is growth)
                  or (isinstance(p, ast.Call) and dotted(p.func) == "len")
                  or (isinstance(p, ast.Subscript) and p is growth))
            if not ok:
                return False
        if isinstance(n, ast.Call):
            d = dotted(n.func)
            if n is growth or d in ELEMENTARY:
                continue
            if d in SCALAR_ONLY and all(_scalar_arg(a, x) for a in n.args) and not n.keywords:
                continue
            if isinstance(n.func, ast.Attribute):
                owner = n.func.value
                if n.func.attr in ELEMENT_METHODS and _scalar_arg(owner, x):
                    continue
                if n.func.attr in APPENDS and isinstance(owner, ast.Name) and owner.id != s:
                    continue
            return False
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice):
            return False
    return True


def fam_rt_membership(m, changed, rel, specs):
    for r in _membership_loops(m, changed):
        loop, a, s, kind, compare = r["loop"], r["a"], r["s"], r["kind"], r["compare"]
        op = "not in" if isinstance(compare.ops[0], ast.NotIn) else "in"
        if kind in SCANNED:
            stem = ("Suppose `{}` holds n distinct items. How does the running time of the loop on line {} grow with n? "
                    "(Line {} tests `{} {} {}`.)").format(a, loop.lineno, compare.lineno, r["x"], op, s)
            key = "Θ(n²)"
            why = ("With n distinct items no early exit fires, so the loop runs n times, and on pass k the test on line {} "
                   "scans a {} that already holds k − 1 items. The scans add up to 0 + 1 + ⋯ + (n − 1) = n(n − 1)/2 "
                   "comparisons: Θ(n²). (Python wiki, TimeComplexity: `x in s` is O(n) for a list.)").format(compare.lineno, kind)
            distractors = [("Θ(n)", "One visible loop, but `in` on a {} is a hidden loop over `{}`.".format(kind, s)),
                           ("Θ(n log n)", "`in` doesn't search a list by halves; lists aren't kept sorted, so it scans."),
                           ("Θ(1)", "With n distinct items no early exit fires: every pass runs, and each scan grows.")]
        else:
            stem = ("Suppose `{}` holds n distinct items. On average (a hash table can degrade with many collisions), how "
                    "does the running time of the loop on line {} grow with n? (Line {} tests `{} {} {}`.)").format(
                        a, loop.lineno, compare.lineno, r["x"], op, s)
            key = "Θ(n)"
            why = ("The loop runs n times, and `in` on a {} is one hash lookup, Θ(1) on average however large `{}` gets "
                   "(Python wiki, TimeComplexity). Total: Θ(n) on average.").format(kind, s)
            distractors = [("Θ(n²)", "That's the cost with a list. A {} finds an item by its hash instead of scanning.".format(kind)),
                           ("Θ(n log n)", "Hash tables aren't sorted and aren't searched by halves."),
                           ("Θ(1)", "The loop still visits each of the n items once.")]
        specs.append({"family": "rt-membership", "priority": 8, "lens": RUNNING_TIME, "line": loop.lineno, "file": rel,
                      "stem": stem, "key": key, "why": why, "distractors": distractors,
                      "supersedes": [("membership", compare.lineno)]})
        return


def _only_membership_uses(m, r):
    """`S` is used only to answer "seen it?": the test, the growth, and len() when S can't
    hold duplicates. A module-level S is checked across the whole file, functions included."""
    s = r["s"]
    if r["scope"] is m.tree:
        uses = [(n, m.parents.get(n)) for n in ast.walk(m.tree)
                if isinstance(n, ast.Name) and n.id == s and isinstance(n.ctx, ast.Load)]
    else:
        uses = m.uses(s, r["scope"])
    for node, parent in uses:
        if parent is r["compare"]:
            continue
        if isinstance(parent, ast.Attribute) and m.parents.get(parent) is r["growth"]:
            continue
        if isinstance(parent, ast.Call) and dotted(parent.func) == "len" and r["no_duplicates"]:
            continue
        return False
    return True


def fam_design_membership(m, changed, rel, specs):
    for r in _membership_loops(m, changed):
        s, a, x, kind, loop = r["s"], r["a"], r["x"], r["kind"], r["loop"]
        if kind in ("list", "deque"):
            if not _only_membership_uses(m, r):
                continue
            add_spec(specs, "design-membership", 7, DESIGN, r["init_line"],
                     "A reviewer suggests making `{s}` a set (`{s} = set()` and `{s}.add({x})`). Assuming the items in `{a}` "
                     "are strings or numbers, what would change when `{a}` is long?".format(s=s, x=x, a=a),
                     "Same results, and the loop drops from Θ(n²) to Θ(n) on average",
                     "`{s}` only answers \"have I seen this?\", which a hash set does in Θ(1) on average instead of a Θ(k) "
                     "scan. The results stay the same because their order comes from the loop over `{a}`, not from `{s}`. "
                     "(The items must be hashable, which strings and numbers are.)".format(s=s, a=a),
                     [("Faster, but the result can change, because sets have no order",
                       "Only the loop's order over `{}` matters, and it doesn't change. `{}` only answers yes/no questions.".format(a, s)),
                      ("Nothing: `in` checks a set item by item, the same as a list",
                       "Sets are hash tables: `in` jumps to where the item would be instead of scanning."),
                      ("Slower, because hashing an item costs more than comparing it",
                       "Hashing costs a little per test, but it replaces a scan of the whole list; for long inputs the set wins by a growing factor.")],
                     rel)
            return
        if kind in HASHED:
            label = "a set" if kind in ("set", "frozenset") else "a dict"
            wrong = ([("It keeps the items in the order they were added, for faster scans",
                       "Sets don't promise any order, and order has nothing to do with how fast `in` is."),
                      ("It lets the same item be stored twice, so repeats are counted",
                       "A set stores each item once; adding a duplicate does nothing."),
                      ("It sorts the items, so `in` can use binary search instead",
                       "Hash tables don't sort; they jump to the item's slot.")]
                     if kind in ("set", "frozenset") else
                     [("It lets the same key appear twice, so repeats are counted",
                       "Each key appears once; assigning to it again replaces the value."),
                      ("It keeps the keys sorted, so `in` can use binary search",
                       "Dicts keep insertion order, not sorted order, and `in` hashes instead of searching."),
                      ("It lets `in` check the stored values as well as the keys",
                       "`in` on a dict checks keys only; checking values needs `.values()`, a Θ(k) scan.")])
            add_spec(specs, "design-membership", 7, DESIGN, r["init_line"],
                     "Line {} makes `{}` {} instead of a list. What does that choice buy in the loop on line {}?".format(
                         r["init_line"], s, label, loop.lineno),
                     "Each `in` test is one Θ(1) hash lookup on average, not a Θ(k) scan",
                     "Hash tables compute where an item would be from its hash, so a membership test doesn't depend on how "
                     "many items are stored (on average). A list would scan up to k items per test, and over n passes that "
                     "adds up to Θ(n²) instead of Θ(n).",
                     wrong, rel)
            return


# ----------------------------------------------------------------- nested loops

def _bound(it, kinds, params=()):
    """A loop's iterable as (kind, n_text, base name). Items only for containers that can be
    iterated again (a file or generator would be used up by the first pass); a parameter is
    allowed, and the question then states that it's a list."""
    if isinstance(it, ast.Name):
        if kinds.get(it.id, (None,))[0] in REITERABLE:
            return ("items", "len({})".format(it.id), it.id)
        if it.id in params:
            return ("param", "len({})".format(it.id), it.id)
        return None
    if isinstance(it, ast.Call) and dotted(it.func) == "range" and len(it.args) == 1 and not it.keywords:
        arg = it.args[0]
        if isinstance(arg, ast.Name):
            return ("range", arg.id, arg.id)
        if isinstance(arg, ast.Call) and dotted(arg.func) == "len" and len(arg.args) == 1 and isinstance(arg.args[0], ast.Name):
            return ("range", "len({})".format(arg.args[0].id), arg.args[0].id)
    return None


def _triangular(inner_it, outer_target, outer_bound, kinds):
    """range(i), range(i + c, N), range(i, N) where N matches the outer bound."""
    if not (isinstance(inner_it, ast.Call) and dotted(inner_it.func) == "range" and not inner_it.keywords):
        return False
    args = inner_it.args

    def is_i(e):
        if isinstance(e, ast.Name) and e.id == outer_target:
            return True
        return (isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add) and isinstance(e.left, ast.Name)
                and e.left.id == outer_target and int_constant(e.right) is not None and int_constant(e.right) >= 0)

    if len(args) == 1:
        return is_i(args[0])
    if len(args) == 2:
        b = _bound(ast.Call(func=ast.Name(id="range", ctx=ast.Load()), args=[args[1]], keywords=[]), kinds)
        return is_i(args[0]) and b is not None and b[1] == outer_bound[1]
    return False


def _has_loop(stmts):
    return any(isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
               for n in _walk_body(stmts))


LENGTH_CHANGERS = ("append", "extend", "insert", "pop", "remove", "clear", "popitem", "add", "discard", "update")


def _len_alias(m, scope, name):
    """"len(nums)" when `name` is bound exactly once in scope, as `name = len(nums)`, and nothing in
    the scope rebinds `nums` or changes its length."""
    values = []
    for n in m.own_nodes(scope):
        if isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            return None
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.NamedExpr)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == name for tg in targets for t in ast.walk(tg)):
                values.append(n.value if isinstance(n, (ast.Assign, ast.AnnAssign)) else None)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and name in {
            a.arg for a in scope.args.posonlyargs + scope.args.args + scope.args.kwonlyargs}:
        return None
    if not (len(values) == 1 and isinstance(values[0], ast.Call) and dotted(values[0].func) == "len"
            and len(values[0].args) == 1 and isinstance(values[0].args[0], ast.Name)):
        return None
    seq = values[0].args[0].id
    for n in m.own_nodes(scope):
        if isinstance(n, (ast.Global, ast.Nonlocal)) and seq in n.names:
            return None
        if isinstance(n, ast.Name) and n.id == seq and isinstance(n.ctx, (ast.Store, ast.Del)):
            return None                        # `nums = nums + nums` after the alias
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and dotted(n.func.value) == seq \
                and n.func.attr in LENGTH_CHANGERS:
            return None
        if isinstance(n, (ast.Delete, ast.AugAssign)) and any(dotted(getattr(t, "value", t)) == seq
                                                              for t in (n.targets if isinstance(n, ast.Delete) else [n.target])):
            return None
    return "len({})".format(seq)


def fam_rt_nested(m, changed, rel, specs):
    for outer in (n for n in ast.walk(m.tree) if isinstance(n, ast.For)):
        if not isinstance(outer.target, ast.Name) or m.inside_loop(outer) is not None:
            continue                                   # a third loop around them would multiply the count again
        scope = m.scope_of(outer)
        kinds = m.stable_kinds(scope)
        params = set()
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = {a.arg for a in scope.args.posonlyargs + scope.args.args + scope.args.kwonlyargs}
            params -= _assigned_names(scope.body)
        ob = _bound(outer.iter, kinds, params)
        if ob is None:
            continue
        for inner in outer.body:
            if not isinstance(inner, ast.For) or not inner.body or _has_loop(inner.body):
                continue
            ib = _bound(inner.iter, kinds, params)
            full = ib is not None and ib[1] == ob[1]
            tri = ob[0] == "range" and _triangular(inner.iter, outer.target.id, ob, kinds)
            if not (full or tri):
                continue
            line = inner.body[0].lineno
            if not ({outer.lineno, inner.lineno, line} & changed):
                continue
            base = ob[2]
            if base in _assigned_names(outer.body) or _mutated(base, outer.body):
                continue
            if any(_is_exit(n) for st in outer.body if st is not inner for n in _walk_body([st])):
                continue                               # a skipped or shortened outer pass changes the count
            n_text = ob[1]
            if ob[0] == "range" and "(" not in n_text:
                n_text = _len_alias(m, scope, n_text) or n_text     # `n = len(nums)`, then `range(n)`
            assume = "Suppose `{}` is a list. ".format(base) if "param" in (ob[0], ib[0] if ib else None) else ""
            stem = "{}If both loops run to completion, how does the number of times line {} runs grow with {}?".format(
                assume, line, "n" if n_text == "n" else "n = `{}`".format(n_text))
            if full:
                why = ("The outer loop on line {} runs n times, and on every pass the inner loop on line {} runs n times "
                       "too: n × n = n² executions of line {}.").format(outer.lineno, inner.lineno, line)
                shrink = ("Θ(n log n)", "Neither loop halves anything; each one runs about n times.")
            else:
                why = ("On pass i of the outer loop, the inner loop runs about i times (or n − i times), so line {} runs "
                       "about 0 + 1 + ⋯ + (n − 1) = n(n − 1)/2 times. Halving the constant doesn't change the growth: "
                       "Θ(n²).").format(line)
                shrink = ("Θ(n log n)", "The inner loop's length changes by one per pass, not by half, so the total is still about n²/2.")
            add_spec(specs, "rt-nested", 7, RUNNING_TIME, line, stem, "Θ(n²)", why,
                     [("Θ(n)", "Two loops, and the inner one starts over on every pass of the outer one."),
                      shrink,
                      ("Θ(n³)", "There are two nested loops here: about n passes, each with about n inner steps.")],
                     rel)
            return


# ----------------------------------------------------------------- list used as a queue

def _list_only_uses(m, name, scope):
    """Uses a deque can't take over: sorting, slicing, + or *, JSON, comparing with a list,
    or a size check that keeps the list small (then pop(0) is cheap anyway)."""
    for n in m.own_nodes(scope):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and dotted(n.func.value) == name \
                and n.func.attr == "sort":
            return True
        if isinstance(n, ast.Subscript) and dotted(n.value) == name and isinstance(n.slice, ast.Slice):
            return True
        if isinstance(n, ast.BinOp) and name in (dotted(n.left), dotted(n.right)):
            return True
        if isinstance(n, ast.Call) and dotted(n.func) in ("json.dump", "json.dumps") \
                and any(dotted(a) == name for a in n.args):
            return True
        if isinstance(n, ast.Compare):
            sides = [n.left] + list(n.comparators)
            if any(dotted(s) == name for s in sides) and any(isinstance(s, ast.List) for s in sides):
                return True
            if any(isinstance(s, ast.Call) and dotted(s.func) == "len" and s.args and dotted(s.args[0]) == name
                   for s in sides):
                return True
    return False


def fam_deque(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):
            continue
        meth, name = node.func.attr, node.func.value.id
        front = (meth == "pop" and len(node.args) == 1 and int_constant(node.args[0]) == 0) or \
                (meth == "insert" and len(node.args) == 2 and int_constant(node.args[0]) == 0)
        if not front:
            continue
        scope = m.scope_of(node)
        if m.stable_kinds(scope).get(name, (None,))[0] != "list" or _list_only_uses(m, name, scope):
            continue
        if meth == "pop":
            change = "`{}.pop(0)` with `{}.popleft()`".format(name, name)
            key = "Removing the first item becomes Θ(1) instead of Θ(k)"
            order = ("The items come out of the queue in a different order", "`popleft()` removes from the same end `pop(0)` does.")
        else:
            change = "`{}.insert(0, …)` with `{}.appendleft(…)`".format(name, name)
            key = "Adding an item at the front becomes Θ(1) instead of Θ(k)"
            order = ("The items end up in the queue in a different order", "`appendleft` adds at the same end `insert(0, …)` does.")
        add_spec(specs, "design-deque", 7, DESIGN, node.lineno,
                 "Line {} uses `{}` as a queue. What would changing it to a `collections.deque` (replacing {}) change, when it holds k items?".format(
                     node.lineno, name, change),
                 key,
                 "A Python list is a dynamic array: working at index 0 moves the other k − 1 items, Θ(k) per call. A deque "
                 "is built from linked blocks with Θ(1) appends and pops at both ends (Python docs, `collections.deque`; "
                 "Python wiki, TimeComplexity). When the queue grows with the input, as in a breadth-first search, that's "
                 "the difference between Θ(n²) and Θ(n) overall.",
                 [("Nothing: working at the front of a list is already Θ(1)",
                   "A list stores items side by side, so the front can't change without moving the rest."),
                  ("Looking items up by position, like `{}[i]`, gets faster".format(name),
                   "Indexing into the middle of a deque is Θ(k); lists are the fast choice for indexing."),
                  order],
                 rel)
        return


# ----------------------------------------------------------------- binary search

def _mid_value(value, lo, hi):
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.FloorDiv) and int_constant(value.right) == 2:
        s = value.left
        if isinstance(s, ast.BinOp) and isinstance(s.op, ast.Add) and {dotted(s.left), dotted(s.right)} == {lo, hi}:
            return True
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add) and dotted(value.left) == lo:
        r = value.right
        return (isinstance(r, ast.BinOp) and isinstance(r.op, ast.FloorDiv) and int_constant(r.right) == 2
                and isinstance(r.left, ast.BinOp) and isinstance(r.left.op, ast.Sub)
                and dotted(r.left.left) == hi and dotted(r.left.right) == lo)
    return False


def _paths(stmts, act):
    """All paths through a block, each as a list of actions, or None if a statement isn't
    understood. `act(stmt)` names one statement's action ("lo", "hi", "ret", "") or None."""
    paths = [[]]
    for s in stmts:
        if isinstance(s, ast.If):
            body, orelse = _paths(s.body, act), _paths(s.orelse, act) if s.orelse else [[]]
            if body is None or orelse is None:
                return None
            branch = body + orelse
        else:
            a = act(s)
            if a is None:
                return None
            branch = [[a] if a else []]
        new = []
        for p in paths:
            if "ret" in p:
                new.append(p)                    # this path already left the loop
            else:
                new.extend(p + b for b in branch)
        paths = new
    return paths


def _binary_searches(m):
    out = []
    for w in (n for n in ast.walk(m.tree) if isinstance(n, ast.While)):
        t = w.test
        if w.orelse or not (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], (ast.LtE, ast.Lt))
                            and isinstance(t.left, ast.Name) and isinstance(t.comparators[0], ast.Name)):
            continue
        lo, hi = t.left.id, t.comparators[0].id
        strict = isinstance(t.ops[0], ast.Lt)
        first = w.body[0] if w.body else None
        if not (isinstance(first, ast.Assign) and len(first.targets) == 1 and isinstance(first.targets[0], ast.Name)
                and _mid_value(first.value, lo, hi)):
            continue
        mid = first.targets[0].id

        def plus(v, c):
            return (isinstance(v, ast.BinOp) and dotted(v.left) == mid and int_constant(v.right) == 1
                    and isinstance(v.op, ast.Add if c > 0 else ast.Sub))

        def act(s):
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                target = s.targets[0].id
                if target == lo and plus(s.value, 1):
                    return "lo"
                if target == hi and ((strict and dotted(s.value) == mid) or (not strict and plus(s.value, -1))):
                    return "hi"
                return None
            if isinstance(s, (ast.Return, ast.Break)):
                return "ret"
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and dotted(s.value.func) == "print":
                return ""
            return None

        paths = _paths(w.body[1:], act)
        if not paths or any(len(p) != 1 for p in paths):
            continue                             # every pass must move lo, move hi, or leave: exactly one
        if not any(p == ["lo"] for p in paths) or not any(p == ["hi"] for p in paths):
            continue
        cmp = None
        for n in _walk_body(w.body):
            if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.Lt, ast.Gt, ast.LtE, ast.GtE)):
                subs = [s for s in (n.left, n.comparators[0]) if isinstance(s, ast.Subscript)
                        and isinstance(s.value, ast.Name) and dotted(s.slice) == mid]
                if len(subs) == 1:
                    cmp = n
                    break
        if cmp is None:
            continue
        sub = cmp.left if isinstance(cmp.left, ast.Subscript) else cmp.comparators[0]
        arr = sub.value.id
        target = cmp.comparators[0] if cmp.left is sub else cmp.left
        lo_assign = next(n for n in _walk_body(w.body) if isinstance(n, ast.Assign)
                         and isinstance(n.targets[0], ast.Name) and n.targets[0].id == lo)
        branch = m.parents.get(lo_assign)
        if not isinstance(branch, ast.If):
            continue
        test = branch.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Lt, ast.Gt, ast.LtE, ast.GtE))):
            continue
        sub_left = isinstance(test.left, ast.Subscript) and dotted(test.left.slice) == mid
        sub_right = isinstance(test.comparators[0], ast.Subscript) and dotted(test.comparators[0].slice) == mid
        if not (sub_left or sub_right):
            continue
        smaller = isinstance(test.ops[0], (ast.Lt, ast.LtE)) if sub_left else isinstance(test.ops[0], (ast.Gt, ast.GtE))
        if lo_assign not in branch.body:
            smaller = not smaller
        # the search must start at index 0 and end at the last index (or one past it for `<`)
        scope = m.scope_of(w)
        starts = {}
        for s in m.own_nodes(scope):
            if not isinstance(s, ast.Assign) or s.lineno >= w.lineno or len(s.targets) != 1:
                continue
            if isinstance(s.targets[0], ast.Tuple) and isinstance(s.value, ast.Tuple):
                pairs = list(zip(s.targets[0].elts, s.value.elts))
            else:
                pairs = [(s.targets[0], s.value)]
            for tg, v in pairs:
                if dotted(tg) in (lo, hi):
                    starts.setdefault(dotted(tg), []).append(src(m.lines, v))
        want_hi = "len({})".format(arr) if strict else "len({}) - 1".format(arr)
        if starts.get(lo) != ["0"] or starts.get(hi) != [want_hi]:
            continue
        returns_mid = any(isinstance(n, ast.Return) and n.value is not None and dotted(n.value) == mid
                          for n in _walk_body(w.body))
        out.append({"loop": w, "arr": arr, "target": target, "ascending": smaller, "returns_mid": returns_mid,
                    "lo": lo, "hi": hi, "mid": mid})
    return out


def fam_binary_search(m, changed, rel, specs):
    for b in _binary_searches(m):
        w = b["loop"]
        lines = range(w.lineno, (getattr(w, "end_lineno", None) or w.lineno) + 1)
        if not any(n in changed for n in lines):
            continue
        add_spec(specs, "rt-halving", 8, RUNNING_TIME, w.lineno,
                 "In the worst case, how does the number of passes through the `while` loop on line {} grow with n = `len({})`?".format(
                     w.lineno, b["arr"]),
                 "Θ(log n)",
                 "Each pass either returns or moves `{lo}` above `{mid}` or `{hi}` below it, so the range still in play at "
                 "least halves. After k passes at most n / 2ᵏ items remain, which reaches one after about log₂ n passes.".format(
                     lo=b["lo"], hi=b["hi"], mid=b["mid"]),
                 [("Θ(n)", "It doesn't visit every item: each pass throws away about half of the range that's left."),
                  ("Θ(n log n)", "That's the cost of sorting. Searching a sorted list by halves is much cheaper."),
                  ("Θ(1)", "It can stop at the first `{}`, but that's the best case; in the worst case it keeps halving.".format(b["mid"]))],
                 rel)
        if b["returns_mid"]:
            t = short(src(m.lines, b["target"]), 20)
            arr = b["arr"]
            order, other = ("ascending", "descending") if b["ascending"] else ("descending", "ascending")
            add_spec(specs, "precondition", 7, CORRECTNESS, w.lineno,
                     "What must be true of `{}` for the loop on line {} to find `{}` whenever `{}` is in it?".format(arr, w.lineno, t, t),
                     "`{}` must be sorted in {} order".format(arr, order),
                     "The loop discards half the range after one comparison with `{}[{}]`. That's only safe if everything on "
                     "the discarded side is known to be smaller (or larger) than `{}`, which is what sorting guarantees.".format(
                         arr, b["mid"], t),
                     [("`{}` must be sorted in {} order".format(arr, other),
                       "Look at which way the search moves: it goes right when `{}[{}]` is {} than `{}`.".format(
                           arr, b["mid"], "smaller" if b["ascending"] else "larger", t)),
                      ("`{}` must not hold any duplicate values".format(arr),
                       "With duplicates it still finds an index where `{}` holds `{}`; sortedness is what it needs.".format(arr, t)),
                      ("`{}` must contain `{}` somewhere".format(arr, t),
                       "If `{}` is missing, the range becomes empty and the loop simply ends.".format(t))],
                     rel)
        return


# ----------------------------------------------------------------- recursion that calls itself twice

FULL_CACHES = ("cache", "functools.cache")


def _decorator_kind(fn):
    """"none", "cache" (an unbounded cache), or "other" (anything we can't vouch for)."""
    if not fn.decorator_list:
        return "none"
    if len(fn.decorator_list) != 1:
        return "other"
    d = fn.decorator_list[0]
    if dotted(d) in FULL_CACHES:
        return "cache"
    if dotted(d) in ("lru_cache", "functools.lru_cache"):
        return "other"                      # bare @lru_cache keeps only 128 results (Python 3.8+ allows it)
    if isinstance(d, ast.Call) and dotted(d.func) in ("lru_cache", "functools.lru_cache"):
        args = list(d.args) + [k.value for k in d.keywords if k.arg == "maxsize"]
        if args and isinstance(args[0], ast.Constant) and args[0].value is None:
            return "cache"
    return "other"


def _covers_base(fn, p, steps):
    """Do the base cases stop every chain of calls? Thresholds (`n < 2`, `n <= 1`) or
    equality tests that cover two neighbouring values for steps (1, 2), one value for (1, 1)."""
    threshold, values = None, set()
    for s in fn.body:
        if not (isinstance(s, ast.If) and s.body and isinstance(s.body[-1], ast.Return)
                and not _contains_call(s, fn.name)):
            continue
        t = s.test
        tests = t.values if isinstance(t, ast.BoolOp) and isinstance(t.op, ast.Or) else [t]
        for c in tests:
            if not (isinstance(c, ast.Compare) and len(c.ops) == 1 and dotted(c.left) == p):
                continue
            op, right = c.ops[0], c.comparators[0]
            k = int_constant(right)
            if isinstance(op, ast.Lt) and k is not None:
                threshold = max(threshold or k, k)
            elif isinstance(op, ast.LtE) and k is not None:
                threshold = max(threshold or k + 1, k + 1)
            elif isinstance(op, ast.Eq) and k is not None:
                values.add(k)
            elif isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                values |= {int_constant(e) for e in right.elts if int_constant(e) is not None}
    if steps == (1, 1):
        return threshold is not None or bool(values)
    if threshold is not None and threshold >= 2:
        return True
    return any(v + 1 in values for v in values)


def _contains_call(node, name):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name for n in ast.walk(node))


def _double_recursion(fn):
    """(param, (c1, c2), cache kind, return_node) for `return f(n - c1) + f(n - c2)`."""
    params = [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg not in ("self", "cls")]
    if len(params) != 1 or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs or fn.args.defaults:
        return None
    p = params[0]
    if p in _assigned_names(fn.body):
        return None
    deco = _decorator_kind(fn)
    if deco == "other":
        return None
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fn.name]
    if len(calls) != 2:
        return None
    steps = []
    for c in calls:
        if len(c.args) != 1 or c.keywords:
            return None
        a = c.args[0]
        if not (isinstance(a, ast.BinOp) and isinstance(a.op, ast.Sub) and isinstance(a.left, ast.Name)
                and a.left.id == p and int_constant(a.right) in (1, 2)):
            return None
        steps.append(int_constant(a.right))
    ret = next((s for s in fn.body if isinstance(s, ast.Return) and s.value is not None
                and _contains_call(s, fn.name)), None)
    if ret is None or not _pure_sum(ret.value, calls):
        return None
    steps = tuple(sorted(steps))
    if steps not in ((1, 1), (1, 2)) or not _covers_base(fn, p, steps):
        return None
    if any(isinstance(n, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops) for n in ast.walk(fn)):
        return None                              # looks like a hand-written memo table
    return p, steps, deco, ret


def _pure_sum(value, calls):
    """The return value is only the two calls combined with + (or *) and constants."""
    if isinstance(value, ast.BinOp) and isinstance(value.op, (ast.Add, ast.Mult)):
        return _pure_sum(value.left, calls) and _pure_sum(value.right, calls)
    return value in calls or isinstance(value, ast.Constant)


def fam_recursion(m, changed, rel, specs):
    for fn in m.functions():
        r = _double_recursion(fn)
        if not r:
            continue
        p, steps, deco, ret = r
        if not ({fn.lineno, ret.lineno} & changed):
            continue
        f = fn.name
        growth = "Θ(2ⁿ), exponential" if steps == (1, 1) else "Θ(φⁿ), exponential"
        if deco == "none":
            if steps == (1, 1):
                why = ("Each call makes two calls on n − 1, so the number of calls doubles with every step: "
                       "C(n) = 2·C(n − 1) + 1, which is about 2ⁿ.")
            else:
                why = ("Each call to `{f}(k)` calls `{f}(k − 1)` and `{f}(k − 2)`, so the number of calls C(n) = C(n − 1) + "
                       "C(n − 2) + 1 grows like the Fibonacci numbers themselves: Θ(φⁿ), with φ = (1 + √5)/2 ≈ 1.618. "
                       "The same small values are recomputed again and again.").format(f=f)
            add_spec(specs, "rt-recursion", 8, RUNNING_TIME, ret.lineno,
                     "How does the total number of calls made by `{}({})` grow with {}?".format(
                         f, p, "n" if p == "n" else "n = `{}`".format(p)),
                     growth, why,
                     [("Θ(n), linear", "That's how many *different* values it needs; without a cache it recomputes them over and over."),
                      ("Θ(n²), quadratic", "Two calls per step don't give n²: the calls branch, roughly multiplying at every level."),
                      ("Θ(log n), logarithmic", "It subtracts from `{}` instead of halving it, so the recursion alone is about n levels deep.".format(p))],
                     rel)
            add_spec(specs, "design-memo", 7, DESIGN, fn.lineno,
                     "What would adding `@functools.cache` above `def {}` change about the number of calls `{}(n)` makes?".format(f, f),
                     "It drops to Θ(n): each value down to the base case is computed once",
                     "A cache stores each result the first time it's computed. There are only about n distinct arguments, so "
                     "the exponential call tree collapses to Θ(n) calls: top-down dynamic programming. (`functools.cache` "
                     "exists since Python 3.9; the recursion is still about n calls deep, so a very large n can hit Python's "
                     "recursion limit.)",
                     [("Nothing in one call: it only helps the next time the program runs",
                       "The cache lives in memory for the whole run, so repeated subproblems inside one call hit it."),
                      ("It drops to Θ(1): the answer is stored after the first call",
                       "The first call to `{}(n)` still computes every smaller value once.".format(f)),
                      ("It gets slower: storing results costs more than recomputing",
                       "Storing n results replaces an exponential number of repeated calls.")],
                     rel)
        else:
            add_spec(specs, "design-memo", 7, DESIGN, fn.lineno,
                     "`{}` calls itself twice per step, but it's decorated with an unbounded cache. Why does that make `{}(n)` fast?".format(f, f),
                     "Each distinct argument is computed once, so it makes Θ(n) calls",
                     "Without the cache, the call tree branches at every level and recomputes the same values: {} calls. With "
                     "it, there are only about n distinct arguments, each computed once: top-down dynamic programming.".format(
                         growth.split(",")[0]),
                     [("The cache runs the two recursive calls in parallel threads",
                       "Caching stores results; it doesn't add parallelism."),
                      ("The cache turns the recursion into a loop automatically",
                       "The recursion is still there, about n calls deep; the cache just answers repeated calls from memory."),
                      ("It isn't faster within one call; it only helps later calls",
                       "Repeated subproblems inside a single call hit the cache too.")],
                     rel)
        return


# ----------------------------------------------------------------- sorting, counting

def fam_sort_cost(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed):
            continue
        if dotted(node.func) == "sorted" and node.args and isinstance(node.args[0], ast.Name):
            shown = "sorted({})".format(node.args[0].id)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "sort" and isinstance(node.func.value, ast.Name) \
                and not node.args and m.stable_kinds(m.scope_of(node)).get(node.func.value.id, (None,))[0] == "list":
            shown = "{}.sort()".format(node.func.value.id)
        else:
            continue
        add_spec(specs, "rt-sort", 5, RUNNING_TIME, node.lineno,
                 "What is the worst-case running time of `{}` on line {}, for n items?".format(shown, node.lineno),
                 "Θ(n log n)",
                 "Python's sort is an adaptive merge sort (Timsort; Python 3.11 switched its merge policy to powersort). It "
                 "makes O(n log n) comparisons in the worst case, and no comparison sort can do better in the worst case. On "
                 "input that's already sorted it takes only Θ(n).",
                 [("Θ(n)", "That's its best case, on already-sorted input. The worst case is n log n."),
                  ("Θ(n²)", "That's insertion or bubble sort. Python's sort is a merge sort with an n log n worst case."),
                  ("Θ(log n)", "Sorting has to look at every item at least once, so it can't be below n.")],
                 rel)
        return


def fam_counting(m, changed, rel, specs):
    for loop in (n for n in ast.walk(m.tree) if isinstance(n, (ast.For, ast.While))):
        kinds = m.stable_kinds(m.scope_of(loop))
        for n in _walk_body(loop.body):
            target = None
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Subscript):
                t, v = n.targets[0], n.value
                if isinstance(t.value, ast.Name) and isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add) \
                        and isinstance(v.left, ast.Call) and dotted(v.left.func) == t.value.id + ".get" \
                        and len(v.left.args) == 2 and int_constant(v.left.args[1]) == 0 and int_constant(v.right) == 1:
                    target = (t.value.id, n.lineno, ("dict", "defaultdict", "Counter"))
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Subscript) and isinstance(n.target.value, ast.Name) \
                    and isinstance(n.op, ast.Add) and int_constant(n.value) == 1:
                target = (n.target.value.id, n.lineno, ("defaultdict", "Counter"))
            if not target or target[1] not in changed:
                continue
            name, line, allowed = target
            if kinds.get(name, (None,))[0] not in allowed:
                continue
            add_spec(specs, "design-counting", 5, DESIGN, line,
                     "Line {} keeps counts in `{}`, a dict keyed by item. Compared with a list of (item, count) pairs, what does the dict buy?".format(
                         line, name),
                     "Finding an item's count is a Θ(1) hash lookup on average, not a scan of the pairs",
                     "A dict is a hash table, so `{}[item]` jumps straight to the item's entry. With a list of pairs, every "
                     "update would scan for the item first, Θ(k) for k distinct items, and Θ(n·k) over the whole loop.".format(name),
                     [("It keeps the counts sorted from the highest to the lowest",
                       "Dicts keep insertion order, not sorted order; sorting takes `sorted(..., key=...)`."),
                      ("It lets the same item appear under two different keys",
                       "Each key appears once, which is exactly what keeps each count in one place."),
                      ("It stores the counts without using any memory per item",
                       "Every entry takes memory, and a hash table keeps spare slots too; the gain is time, not space.")],
                     rel)
            return


FAMILIES = [fam_rt_membership, fam_design_membership, fam_rt_nested, fam_deque, fam_binary_search,
            fam_recursion, fam_sort_cost, fam_counting]
