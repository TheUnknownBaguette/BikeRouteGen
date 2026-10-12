"""pytest bootstrap for the test suite.

Puts the project root on `sys.path` so `import windroute` / `import webapp` resolve
during collection, regardless of where pytest is invoked from. Each test file still
carries its own `sys.path.insert` for the `python tests/test_x.py` direct-run path
(the project's original no-pytest runner) — this just centralizes the same thing for
the `pytest` entry point. With an editable install (`pip install -e ".[dev]"`, as
CI does) neither is strictly needed; both stay so a bare checkout also works.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
