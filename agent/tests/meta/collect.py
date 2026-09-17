"""One source of truth for "what is a test function, and what is in its body".

Both meta-tests ask structural questions about the same set of functions. That
set is computed exactly once, here, so the falsifier rule and the vacuity rule
cannot drift into disagreeing about which functions they govern.

Comments are not in the AST — Python discards them — so the falsifier check
needs `tokenize`, while the vacuity check needs the AST. This module carries
both and joins them on line numbers.
"""

from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass
from pathlib import Path

# The directory the rules govern. tests/meta itself is excluded: these files are
# assertions *about* tests, and pointing the falsifier rule at them would be
# self-referential without adding evidence.
UNIT_DIR = Path(__file__).resolve().parent.parent / "unit"

#: The convention discovered in tests/unit: the first comment inside a test body
#: is a line beginning with this marker, naming the real-world failure the test
#: would catch. See tests/unit/test_triage.py and test_phrases.py.
FALSIFIER_PREFIX = "falsifier:"


@dataclass(frozen=True)
class CollectedTest:
    """A single test function, with everything the rules need to judge it."""

    path: Path
    name: str
    lineno: int
    end_lineno: int
    #: Contiguous `#` comment blocks inside the function body, each already
    #: joined into a single string, in source order. The suite writes a
    #: falsifier as a wrapped multi-line block, so the unit of meaning is the
    #: block rather than the line.
    comments: tuple[str, ...]
    #: Number of `assert` statements anywhere in the body, including nested in
    #: `for`/`with`/`if` blocks.
    assert_count: int
    #: Number of `pytest.raises` (or bare `raises`) context managers, which
    #: assert a failure mode without using the `assert` keyword.
    raises_count: int
    #: Statements in the body, excluding the docstring.
    body_statements: int

    @property
    def location(self) -> str:
        return f"{self.path.name}::{self.name} (line {self.lineno})"

    @property
    def falsifier(self) -> str | None:
        """The falsifier text, or None when the function declares no falsifier."""
        for comment in self.comments:
            stripped = comment.lstrip("#").strip()
            if stripped.lower().startswith(FALSIFIER_PREFIX):
                return stripped[len(FALSIFIER_PREFIX) :].strip()
        return None

    @property
    def asserts_something(self) -> bool:
        """Whether the body contains any assertion mechanism at all."""
        return (self.assert_count + self.raises_count) > 0


def _is_statically_false(node: ast.expr) -> bool:
    """True for a condition the parser can already prove never holds.

    `if False:` / `while 0:` / `if not True:` around an assertion means that
    assertion never executes, so it must not count towards "this test asserts
    something" - otherwise a dead branch is an easy way to satisfy the gate
    with no assertion actually running.
    """
    if isinstance(node, ast.Constant):
        return not bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        operand = node.operand
        if isinstance(operand, ast.Constant):
            return bool(operand.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return len(node.elts) == 0
    return False


class _BodyScanner(ast.NodeVisitor):
    """Counts assertion mechanisms inside one function body.

    Assertions in provably-dead branches are skipped: they are present in the
    source but never run, which is precisely the distinction this gate exists
    to make.
    """

    def __init__(self) -> None:
        self.asserts = 0
        self.raises = 0

    def visit_If(self, node: ast.If) -> None:
        # Walk the live side(s) only. An `if False: ... else: ...` still runs
        # its else branch, so that branch is visited.
        if _is_statically_false(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if _is_statically_false(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        # `for x in []:` never enters the body.
        if _is_statically_false(node.iter):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.asserts += 1
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if _is_raises_call(item.context_expr):
                self.raises += 1
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            if _is_raises_call(item.context_expr):
                self.raises += 1
        self.generic_visit(node)


def _is_raises_call(node: ast.expr) -> bool:
    """True for `pytest.raises(...)`, `pytest.warns(...)` and bare `raises(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in {"raises", "warns"}
    if isinstance(func, ast.Name):
        return func.id in {"raises", "warns"}
    return False


def _comment_blocks(path: Path) -> list[tuple[int, str]]:
    """Contiguous `#` comment runs, as `(first_line, joined_text)` pairs.

    The suite wraps a falsifier across several `#` lines:

        # falsifier: an assessed increase in severity is not recorded, so the
        # UI and the record disagree with what the agent concluded.

    Read line-by-line that is two comments, the second of which does not start
    with the marker and the first of which is truncated mid-sentence. Joining
    the run first is what makes both the marker test and the length test look
    at the falsifier the author actually wrote.
    """
    with path.open("rb") as handle:
        comments = [
            (token.start[0], token.start[1], token.string)
            for token in tokenize.tokenize(handle.readline)
            if token.type is tokenize.COMMENT
        ]

    # A comment token can be a whole line of its own or trail a statement
    # (`setup()  # arrange`). Only standalone lines form blocks: merging a
    # trailing comment into the block below it would prepend unrelated text to
    # a falsifier and make a properly-annotated test look unannotated.
    # `utf-8-sig`, not `utf-8`: tokenize strips a UTF-8 BOM as part of its
    # encoding handling, so reading the same file as plain utf-8 would leave a
    # BOM on line 1 that tokenize's column numbers do not account for, shifting
    # the standalone-comment check by one character on that line.
    source_lines = path.read_text(encoding="utf-8-sig").splitlines()

    def _is_standalone(line: int, column: int) -> bool:
        if not (1 <= line <= len(source_lines)):
            return False
        return not source_lines[line - 1][:column].strip()

    blocks: list[tuple[int, str]] = []
    previous_standalone = False
    for line, column, text in comments:
        standalone = _is_standalone(line, column)
        body = text.lstrip("#").strip()
        contiguous = (
            blocks
            and previous_standalone
            and standalone
            and line == blocks[-1][0] + _block_line_count(blocks[-1][1])
        )
        if contiguous:
            start, existing = blocks.pop()
            blocks.append((start, f"{existing}\n{body}"))
        else:
            blocks.append((line, body))
        previous_standalone = standalone
    return blocks


def _block_line_count(joined: str) -> int:
    return joined.count("\n") + 1


def _is_test(node: ast.AST) -> bool:
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name.startswith("test_")


def _body_without_docstring(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.stmt]:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return body[1:]
    return body


def _signature_end_line(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    body: list[ast.stmt],
    end: int,
) -> int:
    """The last line of the `def` header, after which the body begins.

    Signatures in this suite wrap across lines, so the `def` line alone is not
    the boundary - and the boundary cannot be "the line before the first
    statement" either, because the falsifier block sits above that statement and
    must fall inside the window. So it is derived from the header itself: the
    lowest line of the first body statement is the floor, and the highest line
    occupied by any argument or return annotation is the header's true extent.
    """
    header_end = node.lineno
    annotations: list[ast.AST] = [
        *node.args.args,
        *node.args.posonlyargs,
        *node.args.kwonlyargs,
    ]
    if node.args.vararg is not None:
        annotations.append(node.args.vararg)
    if node.args.kwarg is not None:
        annotations.append(node.args.kwarg)
    if node.returns is not None:
        annotations.append(node.returns)
    for element in annotations:
        candidate = getattr(element, "end_lineno", None) or getattr(
            element, "lineno", node.lineno
        )
        header_end = max(header_end, candidate)
    # Never let the header swallow the body: the first statement bounds it.
    first_statement = min(
        (statement.lineno for statement in (body or node.body)), default=end
    )
    return min(header_end, max(node.lineno, first_statement - 1))


def collect_test_functions(directory: Path = UNIT_DIR) -> list[CollectedTest]:
    """Every `test_*` function under `directory`, as structured records.

    Nested test functions and methods on test classes are included: a lazy test
    is no less lazy for living inside a class.
    """
    collected: list[CollectedTest] = []
    for path in sorted(directory.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        blocks = _comment_blocks(path)
        for node in ast.walk(tree):
            if not _is_test(node):
                continue
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            # `end_lineno` is populated by ast.parse on every supported Python.
            end = node.end_lineno or node.lineno
            body = _body_without_docstring(node)
            scanner = _BodyScanner()
            for statement in body:
                scanner.visit(statement)
            # A comment block counts as this function's when it begins after the
            # `def` line and ends inside the function. The falsifier sits above
            # the first statement, so the window is the whole function span
            # rather than "from the first statement onward" - an earlier version
            # of this filter started at `first_statement - 1` and so kept only
            # the block's trailing continuation line, truncating every
            # multi-line falsifier in the suite.
            signature_end = _signature_end_line(node, body, end)
            collected.append(
                CollectedTest(
                    path=path,
                    name=node.name,
                    lineno=node.lineno,
                    end_lineno=end,
                    comments=tuple(
                        text
                        for line, text in blocks
                        if signature_end < line <= end
                    ),
                    assert_count=scanner.asserts,
                    raises_count=scanner.raises,
                    body_statements=len(body),
                )
            )
    return collected
