"""Build a submission zip from a clamped strategy dict."""

from __future__ import annotations

import base64
import io
import json
import urllib.request
import zipfile
from pathlib import Path

from coach.strategies import design_document, render

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
CACHE = ROOT / "state" / "templates"


def build(game: str, strategy: dict, code: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        agent = code.encode() if code is not None else (RUNTIME / game / "agent.py").read_bytes()
        bundle.writestr("agent.py", agent)
        bundle.writestr("strategy.py", render(strategy).encode())
        if game == "racing":
            template = ensure_racing_template()
            bundle.write(template / "agent.py", "formula_driver.py")
            bundle.write(template / "plan.npz", "plan.npz")
            bundle.write(template / "mechanics.npz", "mechanics.npz")
            from coach.racing_assets import bundled_files
            for name, path in bundled_files().items():
                bundle.write(path, name)
            base = json.loads((template / "design.json").read_text())
            document = design_document(base, strategy)
            bundle.writestr("design.json", json.dumps(document, indent=2).encode())
    return buffer.getvalue()


def ensure_racing_template() -> Path:
    target = CACHE / "racing"
    marker = target / "plan.npz"
    if marker.is_file() and (target / "agent.py").is_file() and (target / "design.json").is_file():
        return target
    distributed = ROOT / "assets" / "racing" / "template"
    if all((distributed / name).is_file() for name in ("agent.py", "design.json", "plan.npz", "mechanics.npz")):
        return distributed
    target.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        "https://agx.cool/api/v1/python-submissions/templates/racing",
        headers={"User-Agent": "comp-rsi", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    for item in payload["files"]:
        raw = item["source"].encode() if item.get("encoding") != "base64" else base64.b64decode(item["source"])
        if item.get("encoding") == "base64":
            (target / item["path"]).write_bytes(raw)
        else:
            (target / item["path"]).write_text(item["source"])
    return target
