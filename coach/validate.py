"""Run a model-written candidate through the local gate before it can be uploaded."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from coach.strategies import render

GATE = Path(__file__).resolve().parent / "gate_fps.py"
MAX_SOURCE = 4194304
TIMEOUT = 90


def check(game: str, code: str, strategy: dict, timeout: int = TIMEOUT) -> tuple[bool, str]:
    """Return (passed, report). The report is written for the model, not for a human."""
    if game != "fps":
        return True, "no local gate for this game"
    if not isinstance(code, str) or not code.strip():
        return False, "LOCAL GATE: the candidate contained no agent.py source."
    size = len(code.encode())
    if size > MAX_SOURCE:
        return False, f"LOCAL GATE: agent.py is {size} bytes, the limit is {MAX_SOURCE}."
    with tempfile.TemporaryDirectory(prefix="gate-") as folder:
        root = Path(folder)
        (root / "agent.py").write_text(code)
        (root / "strategy.py").write_text(render(strategy))
        try:
            done = subprocess.run(
                [sys.executable, "-I", str(GATE), str(root)],
                capture_output=True, text=True, timeout=timeout, cwd=root,
            )
        except subprocess.TimeoutExpired:
            return False, (
                f"LOCAL GATE: the candidate ran past {timeout}s and was killed. "
                "A decision must return in well under 50 ms. Remove any loop that can run forever."
            )
    payload = _payload(done.stdout)
    if payload is None:
        detail = (done.stderr or done.stdout or "no output").strip()[-1500:]
        return False, f"LOCAL GATE: the candidate crashed the fixture.\n{detail}"
    return bool(payload.get("ok")), str(payload.get("report") or "")


def _payload(text: str) -> dict | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "checks" in parsed:
                return parsed
    return None