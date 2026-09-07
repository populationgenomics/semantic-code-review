"""`python -m semantic_code_review.cli` — how `scr review` re-executes
itself as the detached review server (`review/runner.py`).
"""

from __future__ import annotations

from . import app

app()
