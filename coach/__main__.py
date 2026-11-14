"""Run the AGX counter-strategy loop.

    python3 -m coach self-check
    python3 -m coach gate
    python3 -m coach dry-run
    python3 -m coach rewrite
    python3 -m coach status
    python3 -m coach once
    python3 -m coach run
    python3 -m coach ui
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import zipfile
from pathlib import Path

from coach.config import load
from coach.loop import dry_run, run_forever, status, tick_all
from coach.strategies import clamp, seed


def self_check() -> None:
    from coach.bundle import build

    wild = {
        "front_wing": 99,
        "stops": [{"lap": 9, "compound": "slick"}],
        "start_compound": "soft",
        "buy_style": "awp",
        "default_site": "Z",
    }
    racing = clamp("racing", wild, seed("racing"))
    fps = clamp("fps", wild, seed("fps"))
    if racing["front_wing"] != 25 or racing["stops"][0]["compound"] == "soft":
        raise SystemExit(f"racing clamp failed: {racing}")
    if fps["buy_style"] != "full_rifle" or fps["default_site"] not in ("A", "B"):
        raise SystemExit(f"fps clamp failed: {fps}")
    for game in ("racing", "fps"):
        blob = build(game, seed(game))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with zipfile.ZipFile(__import__("io").BytesIO(blob)) as archive:
                archive.extractall(root)
            if game == "fps":
                _exercise_fps(root)
            else:
                _exercise_racing(root)
            print(f"{game} bundle ok, {len(blob)} bytes, files={sorted(p.name for p in root.iterdir())}")
    print("self-check passed")


def _load(path: Path, name: str):
    sys.modules.pop("strategy", None)
    sys.modules.pop("formula_driver", None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _exercise_fps(root: Path) -> None:
    sys.path.insert(0, str(root))
    agent = _load(root / "agent.py", "fps_agent_check")
    agent.init({"contract_id": "rsi-fps-5.5", "seat": 0, "role": 0, "seed": 1, "weights_dir": str(root)})
    bought = agent.act({
        "phase": "buy",
        "side": "attack",
        "round_index": 0,
        "round_tick": 0,
        "me": {"alive": True, "cash": 800, "armor": 0, "weapons": [None, {"id": "pistol"}, {"id": "knife"}], "throwable_kinds": []},
        "visible": [],
        "teammates": [],
        "bomb": {"state": "carried"},
    })
    if "buy" not in bought:
        raise SystemExit(f"fps buy failed: {bought}")
    fought = agent.act({
        "phase": "live",
        "side": "attack",
        "round_index": 0,
        "round_tick": 2000,
        "match_tick": 2000,
        "buy_deadline_tick": 1860,
        "me": {"alive": True, "x": 0, "y": 0, "z": 0, "yaw": 0, "pitch": 0, "vx": 0, "vy": 0, "cash": 0, "armor": 650, "weapons": [None, {"id": "pistol", "ammo": 12}, {"id": "knife"}], "throwable_kinds": [], "carrying_bomb": False},
        "visible": [{"x": 8, "y": 0, "z": 0, "seat": 9, "parts": ["head"]}],
        "teammates": [],
        "bomb": {"state": "dropped"},
    })
    if "yaw_rate" not in fought:
        raise SystemExit(f"fps aim failed: {fought}")
    sys.path.pop(0)


def _exercise_racing(root: Path) -> None:
    sys.path.insert(0, str(root))
    agent = _load(root / "agent.py", "racing_agent_check")
    agent.init({
        "contract_id": "rsi-racing-4.4",
        "seat": 0,
        "role": 0,
        "seed": 1,
        "weights_dir": str(root),
    })
    idle = agent.act({"phase": "qualifying"})
    if not isinstance(idle, dict) or "throttle" not in idle:
        raise SystemExit(f"racing act failed: {idle}")
    sys.path.pop(0)


def gate() -> None:
    from coach.validate import check

    code = (Path(__file__).resolve().parents[1] / "runtime" / "fps" / "agent.py").read_text()
    ok, report = check("fps", code, seed("fps"))
    print(report)
    raise SystemExit(0 if ok else 1)


def rewrite(cfg) -> None:
    """Ask oMLX for a new agent.py, gate it, and store it as Plan B. Does not upload."""
    from coach.agx import Agx
    from coach.loop import (attach_standings, combat_fault, evidence, gate_best, improve_code,
                            load_state, sampling_for, save_state, we_won)

    state = load_state("fps")
    passed, gate_report = gate_best("fps", state)
    focus = state["A"].get("last") or {}
    client = Agx(cfg.base_url, cfg.api_key)
    if focus:
        attach_standings(client, "fps", focus)
    mode = "win" if we_won("fps", focus) else "loss"
    text = evidence(state, mode, focus, client=client)
    fault = combat_fault(focus)
    if fault:
        text += "\n\n" + fault
    sampling = sampling_for(cfg, state, mode, "code")
    book, code = improve_code(cfg, "fps", state, text, gate_report, sampling, force=True)
    if code is None:
        print("no rewrite passed the local gate")
        raise SystemExit(1)
    state["B"]["strategy"] = book
    state["B"]["code"] = code
    state["B"]["live_hash"] = None
    save_state("fps", state)
    print(f"Plan B '{book['name']}' passed the gate, {len(code.encode())} bytes, stored not uploaded")


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "run"
    if command == "self-check":
        self_check()
        return
    if command == "gate":
        gate()
        return
    cfg = load()
    if command == "dry-run":
        dry_run(cfg)
    elif command == "rewrite":
        rewrite(cfg)
    elif command == "status":
        status(cfg)
    elif command == "once":
        tick_all(cfg)
    elif command == "run":
        run_forever(cfg)
    elif command == "ui":
        from coach.ui import serve
        serve()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
