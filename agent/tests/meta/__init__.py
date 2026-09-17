"""Meta-tests: the suite judged as evidence rather than as code.

These enforce the discipline CLAUDE.md states — a requirement is BUILT only when
something that runs asserts it — one level up. A test that asserts nothing runs
green and reports nothing, which is the exact failure mode this project has on
record. tests/meta is the mechanism that makes such a test fail.
"""
