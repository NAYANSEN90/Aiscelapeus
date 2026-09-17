"""No unit test may pass without asserting anything.

A vacuous test is worse than a missing one: the missing test is visibly absent,
while the vacuous one reports success. This is the same defect as a CI job that
passes with zero tests collected, one level down, and it is banned by the same
reasoning.

The rules here are structural, read off the AST, so they cannot be satisfied by
a comment or a docstring claiming an assertion exists.

Scope, stated plainly so the gate is not over-trusted. It rejects: a body with
no assertion at all; a body that is only `pass`/`...`; an assertion over
literals (`assert True`, `assert not False`, `assert bool(True)`,
`assert 1 == 1`); an assertion over a name bound to a literal in the same body
(`ok = True; assert ok`); and an assertion reachable only through a provably
dead branch (`if False:`). It does NOT reason about values returned by helper
functions, so a test asserting on a hand-rolled constant helper can still slip
through; it does not flag assertions over collection displays
(`assert len([]) == 0`), because treating those as tautologies rejected honest
tests; and it does not detect `@pytest.mark.skip`, which stops a test running
without making it vacuous. Those are the known limits.
"""

from __future__ import annotations

import ast

import pytest

from tests.meta.collect import CollectedTest, collect_test_functions

TESTS = collect_test_functions()

#: Comparisons of two literals - `assert True`, `assert 1 == 1`, `assert "a"`.
#: These are decided by the parser, not by the code under test.
_CONSTANT = (ast.Constant,)


def _id(function: CollectedTest) -> str:
    return f"{function.path.name}::{function.name}"


def _is_literal(node: ast.expr, literal_names: frozenset[str]) -> bool:
    """True when `node` evaluates without consulting the system under test.

    Covers the forms an author reaches for when quieting the "asserts
    something" rule: a bare literal, a name bound to a literal earlier in the
    same body, a unary op or boolean op over those, and `bool(...)`/`len(...)`
    wrappers around them.

    Collection *displays* (`[1, 2, 3]`, `(1, 2)`) are deliberately NOT literals
    here, even though their elements are. A list of literals is ordinary test
    input - `values = [1, 2, 3]` then `assert len(values) == 3` is a legitimate
    assertion about a collection - and treating it as a tautology made the
    gate reject an honest test during verification. Narrowing this is a chosen
    trade: `assert [1] == [1]` is not reported, which is the cost of not
    rejecting real tests.
    """
    if isinstance(node, _CONSTANT):
        return True
    if isinstance(node, ast.Name):
        # `ok = True` earlier in the body, then `assert ok`.
        return node.id in literal_names
    if isinstance(node, ast.UnaryOp):
        # assert not False
        return _is_literal(node.operand, literal_names)
    if isinstance(node, ast.BoolOp):
        # assert True and True
        return all(value for value in (
            _is_literal(operand, literal_names) for operand in node.values
        ))
    if isinstance(node, ast.Compare):
        # assert 1 == 1, assert "x" != "y" - every side a literal.
        operands = [node.left, *node.comparators]
        return all(_is_literal(operand, literal_names) for operand in operands)
    if isinstance(node, ast.Call):
        # assert bool(True), assert len([]) == 0
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if name in {"bool", "len", "int", "str", "float", "tuple", "list", "set"}:
            return all(_is_literal(arg, literal_names) for arg in node.args)
    return False


def _literal_bound_names(
    body: list[ast.stmt] | tuple[ast.stmt, ...],
) -> frozenset[str]:
    """Names assigned a literal, and never reassigned anything else, in `body`.

    Deliberately conservative: a name assigned a literal *and* later bound to
    anything else - by assignment, unpacking, augmented assignment, a `for`
    target, `with ... as`, or a walrus - is not reported. Every binding form
    that is not a direct `name = <literal>` taints, so wrapping a genuine value
    in a variable is never mistaken for a tautology.

    Taint is monotonic and subtracted at the end, which makes the result
    independent of the order `ast.walk` happens to visit statements in.
    """
    literal: set[str] = set()
    tainted: set[str] = set()

    def taint_all(node: ast.AST) -> None:
        """Taint every name bound anywhere inside a binding target."""
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                tainted.add(child.id)

    for statement in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        # Direct `name = <literal>` / `name: T = <literal>` is the only form
        # that can *introduce* a literal binding.
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets: list[ast.expr] = (
                list(statement.targets)
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            value = statement.value
            for target in targets:
                if isinstance(target, ast.Name):
                    if value is not None and _is_literal(value, frozenset(literal)):
                        literal.add(target.id)
                    else:
                        tainted.add(target.id)
                else:
                    # Tuple/List/Starred/attribute/subscript target. An earlier
                    # version skipped these entirely, so `v = 0` followed by
                    # `v, other = state.level, x` left `v` marked literal and a
                    # genuine `assert v == 4` was reported as a tautology - a
                    # false positive in the direction that gets a gate switched
                    # off. Found by independent review.
                    taint_all(target)
        # Every other binding form only ever taints.
        elif isinstance(statement, ast.AugAssign):
            taint_all(statement.target)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            taint_all(statement.target)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                if item.optional_vars is not None:
                    taint_all(item.optional_vars)
        elif isinstance(statement, ast.NamedExpr):
            taint_all(statement.target)
        elif isinstance(statement, (ast.comprehension,)):
            taint_all(statement.target)

    # Mutation through a method call taints too, and this was missed: the rules
    # above all track *rebinding*, but a collection can be filled without ever
    # being rebound. `seen = set()` then `seen.add(x)` in a loop leaves `seen`
    # marked literal, so a genuine `assert "k" in seen` was reported as a
    # tautology - the Compare branch needs every operand literal, and `seen`
    # wrongly counted as one. That is a false positive in the dangerous
    # direction: it rejects an honest test, which is how a gate gets switched
    # off. Found when a real test in tests/unit/test_ports.py built its
    # expected set by AST walk and was refused.
    #
    # Deliberately blunt: any `name.<method>(...)` taints `name`, without
    # reasoning about whether that method mutates. A literal a test bothers to
    # call a method on is not the `ok = True` pattern this gate exists to catch,
    # so over-tainting here costs nothing and under-tainting rejects real tests.
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            tainted.add(node.func.value.id)

    return frozenset(literal - tainted)


def _is_tautological(
    node: ast.Assert, literal_names: frozenset[str] = frozenset()
) -> bool:
    """True when the asserted expression cannot depend on the system's behaviour."""
    return _is_literal(node.test, literal_names)


def _assert_nodes(
    function: CollectedTest,
) -> tuple[list[ast.Assert], frozenset[str]]:
    """This function's assert nodes, plus the names it bound to literals.

    The names travel with the asserts because `assert ok` is only a tautology
    in the context of an earlier `ok = True`; the assert node alone cannot say.
    """
    source = function.path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(function.path))
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function.name
            and node.lineno == function.lineno
        ):
            asserts = [
                child for child in ast.walk(node) if isinstance(child, ast.Assert)
            ]
            return asserts, _literal_bound_names(node.body)
    return [], frozenset()


def test_the_collector_found_the_existing_suite() -> None:
    # falsifier: the collector matches nothing and every rule below iterates an
    # empty list, so this file reports green while permitting exactly the lazy
    # tests it exists to reject.
    assert len(TESTS) >= 20, (
        f"expected the unit suite to contain many tests, collected {len(TESTS)}"
    )


@pytest.mark.parametrize("function", TESTS, ids=_id)
def test_every_unit_test_asserts_something(function: CollectedTest) -> None:
    # falsifier: a test that only calls the code under test is added, so it
    # passes whenever the call does not raise - it certifies "does not crash"
    # while reading, in a coverage report, as proof the behaviour is correct.
    assert function.asserts_something, (
        f"{function.location} contains no assertion: "
        f"{function.assert_count} assert statements and "
        f"{function.raises_count} raises/warns context managers.\n"
        f"A test that asserts nothing passes whenever the code does not crash."
    )


@pytest.mark.parametrize("function", TESTS, ids=_id)
def test_no_unit_test_has_an_empty_body(function: CollectedTest) -> None:
    # falsifier: a placeholder `def test_x(): pass` (or one holding only `...`,
    # or only a docstring) is committed as a stand-in and never filled in, so an
    # unbuilt requirement shows up in the run as a passing test.
    assert function.body_statements > 0, (
        f"{function.location} has an empty body (only a docstring)"
    )
    source = function.path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(function.path))
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function.name
            and node.lineno == function.lineno
        ):
            statements = [
                statement
                for statement in node.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                )
            ]
            only_filler = all(
                isinstance(statement, ast.Pass)
                or (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and statement.value.value is Ellipsis
                )
                for statement in statements
            )
            assert not (statements and only_filler), (
                f"{function.location} is a placeholder: its body is only "
                f"`pass` or `...`"
            )


@pytest.mark.parametrize("function", TESTS, ids=_id)
def test_no_assertion_is_a_tautology(function: CollectedTest) -> None:
    # falsifier: `assert True`, `assert not False` or `ok = True; assert ok` is
    # used to quiet the "asserts something" rule above, so a test satisfies the
    # letter of the gate while its outcome is decided by the parser rather than
    # by the system under test.
    nodes, literal_names = _assert_nodes(function)
    offenders = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in nodes
        if _is_tautological(node, literal_names)
    ]
    assert not offenders, (
        f"{function.location} contains assertions that cannot fail:\n  "
        + "\n  ".join(offenders)
    )


def test_the_tautology_detector_recognises_the_forms_it_claims_to() -> None:
    # falsifier: `_is_tautological` misses `assert True` or flags a real
    # assertion, so the rule above is either toothless or blocks honest tests -
    # and either way nobody finds out until someone commits a lazy test.
    def parse(source: str) -> ast.Assert:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.Assert)
        return node

    vacuous = [
        "assert True",
        "assert 1",
        "assert 1 == 1",
        "assert 'a' != 'b'",
        # Forms an independent review found bypassing an earlier version of
        # this detector. Each one satisfied every rule while asserting nothing.
        "assert (True)",
        "assert not False",
        "assert bool(True)",
        "assert True and True",
    ]
    for source in vacuous:
        assert _is_tautological(parse(source)), f"{source!r} should be rejected"

    real = [
        "assert state.level is Criticality.SEVERE",
        "assert len(state.history) == 1",
        "assert hit is not None",
        "assert result == expected",
        "assert not state.escalated",
        "assert bool(state.clinician_present)",
        "assert len(state.history) == 1 and state.level == 4",
        # A collection of literals is test input, not a tautology. Asserting on
        # its length is legitimate; see `_is_literal`'s docstring for the trade.
        "assert len(values) == 3",
        # The acknowledged cost of that same narrowing: this one really is
        # vacuous and is NOT reported. Pinned here so the limitation is a
        # recorded decision rather than an accident someone "fixes" by
        # re-widening the rule and breaking honest tests again.
        "assert len([]) == 0",
    ]
    for source in real:
        assert not _is_tautological(parse(source)), f"{source!r} is a real assertion"


def test_a_name_bound_to_a_literal_is_not_a_real_assertion() -> None:
    # falsifier: `ok = True; assert ok` passes the tautology rule because the
    # asserted node is a Name rather than a Constant, so the whole vacuity gate
    # is bypassed by introducing one variable. An independent review found this
    # exact hole in the first version of this file.
    body = ast.parse("ok = True\nassert ok\n").body
    names = _literal_bound_names(body)
    assert "ok" in names, "a name assigned a literal must be tracked"
    statement = body[-1]
    assert isinstance(statement, ast.Assert)
    assert _is_tautological(statement, names)


def test_a_name_bound_to_real_behaviour_is_left_alone() -> None:
    # falsifier: the literal-tracking above is too eager and flags
    # `level = state.level; assert level == 4` as vacuous, so the gate starts
    # rejecting honest tests and gets switched off.
    body = ast.parse("level = state.level\nassert level == 4\n").body
    names = _literal_bound_names(body)
    assert "level" not in names
    statement = body[-1]
    assert isinstance(statement, ast.Assert)
    assert not _is_tautological(statement, names)


def test_a_name_reassigned_from_a_literal_to_real_behaviour_is_left_alone() -> None:
    # falsifier: a name that starts as a literal placeholder and is then
    # overwritten with a real value is treated as forever-literal, so a genuine
    # assertion on it is wrongly reported as a tautology.
    body = ast.parse("value = 0\nvalue = state.level\nassert value == 4\n").body
    assert "value" not in _literal_bound_names(body)


@pytest.mark.parametrize(
    "source",
    [
        # Tuple unpacking - the gap an independent review found. `start` was
        # left marked literal because the target is a Tuple, not a Name.
        "start = 0\nstart, end = hit.span\nassert start == 3\n",
        "a = 0\n[a, b] = state.pair\nassert a == 3\n",
        "a = 0\na, *rest = state.values\nassert a == 3\n",
        # Every other binding form that must taint.
        "total = 0\ntotal += state.level\nassert total == 4\n",
        "item = 0\nfor item in state.history:\n    pass\nassert item == 4\n",
        "handle = 0\nwith open(p) as handle:\n    pass\nassert handle\n",
    ],
)
def test_every_rebinding_form_taints_a_literal_name(source: str) -> None:
    # falsifier: a literal placeholder is later rebound by unpacking, `+=`, a
    # `for` target or `with ... as`, the name stays marked literal, and a
    # genuine assertion on its real value is reported as a tautology. That is a
    # false positive in the worst direction: it rejects an honest test, which is
    # how a gate earns a reputation for crying wolf and gets switched off.
    body = ast.parse(source).body
    bound = _literal_bound_names(body)
    rebound = source.split("\n")[0].split(" ")[0]
    assert rebound not in bound, (
        f"{rebound!r} was rebound to a real value but is still marked literal"
    )


@pytest.mark.parametrize(
    "source",
    [
        # Filled by mutation, never rebound - the form that was missed.
        "seen = set()\nseen.add(state.level)\nassert 4 in seen\n",
        "rows = []\nrows.append(state.level)\nassert rows == [4]\n",
        "seen = set()\nfor node in tree:\n    seen.add(node.attr)\nassert 'x' in seen\n",
        "counts = {}\ncounts.update(state.tally)\nassert counts['x'] == 1\n",
    ],
)
def test_a_collection_filled_by_mutation_is_not_a_literal(source: str) -> None:
    # falsifier: every rule in `_literal_bound_names` tracks *rebinding*, so a
    # collection initialised from a literal and then filled by `.add()` /
    # `.append()` stays marked literal though its contents came entirely from
    # the system under test. `assert "k" in seen` is then a `Compare` whose
    # every operand looks literal, and an honest test is rejected as a
    # tautology - a false positive in the direction that gets a gate switched
    # off. This is not hypothetical: it rejected
    # `test_the_port_declares_everything_the_agent_layer_uses`, which builds its
    # expected sets by walking agent.py's AST.
    body = ast.parse(source).body
    bound = _literal_bound_names(body)
    name = source.split(" ")[0]
    assert name not in bound, (
        f"{name!r} is filled from real behaviour by mutation but is still marked literal"
    )


def test_mutation_tainting_did_not_blind_the_detector_to_real_tautologies() -> None:
    # falsifier: the mutation fix above is written as a blanket "any name with a
    # method call is tainted" and over-reaches into the forms this gate exists
    # to catch, so `ok = True; assert ok` starts passing and the whole rule
    # quietly stops working. Pins both directions in one place, because a fix
    # for a false positive is exactly where a false negative gets introduced.
    still_caught = _literal_bound_names(ast.parse("ok = True\nassert ok\n").body)
    assert "ok" in still_caught, "a literal-bound name must still be reported"

    node = ast.parse("assert True").body[0]
    assert isinstance(node, ast.Assert)
    assert _is_tautological(node), "`assert True` must still be a tautology"
