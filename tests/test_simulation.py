"""Lock the end-to-end simulation demo (examples/simulate.py) so it can't bitrot.

Runs the whole script in the test interpreter (which has flowspec2 + httpx) and
asserts every scenario reached its ticket, the retry recovered, and idempotency
fired once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_simulation_script_runs_all_scenarios():
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "simulate.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    # all six tickets opened
    for n in range(481, 487):
        assert f"RLU-2026-000{n}" in out
    # scenario 5: ticketing system went down then recovered (two calls)
    assert "temporarily unavailable" in out
    assert "ticketing calls: 2" in out
    # scenario 6: duplicate submission fired the side effect exactly once
    assert "actual ticketing calls: 1" in out
    assert "all simulations completed" in out
