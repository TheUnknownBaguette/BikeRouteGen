"""pytest bootstrap for the test suite.

Puts the project root on `sys.path` so `import windroute` / `import webapp` resolve
during collection, regardless of where pytest is invoked from. Each test file still
carries its own `sys.path.insert` for the `python tests/test_x.py` direct-run path
(the project's original no-pytest runner) — this just centralizes the same thing for
the `pytest` entry point. Once the package is pip-installable (see
CODE_HEALTH_WORKPLAN Task B2) both can go away.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
