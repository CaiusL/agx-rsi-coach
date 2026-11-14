"""Run the AGX kit simulators on every public map with calibrated parameters.

Racing: kit sim/optimize.py searches a lateral line + speed profile per track.
FPS:    kit sim/optimize.py searches spawn routes + a minimax position per site.

Parameters come from sim/params/*.json. Fields measured from our own public
recordings are tagged "measured"; the rest are labelled estimates. Local
objectives are approximations, NOT platform scores.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RACING_KIT = ROOT / "agx-racing-kit"
FPS_KIT = ROOT / "agx-fps-kit"
OUT = ROOT / "sim/out"
OUT.mkdir(parents=True, exist_ok=True)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def racing(params: dict) -> list[dict]:
    opt = _load("racing_opt", RACING_KIT / "sim/optimize.py")
    rows = []
    for npz in sorted((RACING_KIT / "data").glob("*.npz")):
        track = npz.stem.replace("racing-", "").replace(".mapdata", "")
        result, arrays = opt.optimize(npz, params)
        np.savez_compressed(OUT / f"racing-{track}.profile.npz", **arrays)
        (OUT / f"racing-{track}.result.json").write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n")
        rows.append(dict(track=track, **{k: result[k] for k in
                    ("estimated_lap_seconds", "centerline_seconds",
                     "estimated_energy_mj", "line_scale",
                     "min_speed_mps", "max_speed_mps")}))
    return rows


def fps(params: dict) -> list[dict]:
    opt = _load("fps_opt", FPS_KIT / "sim/optimize.py")
    rows = []
    for js in sorted((FPS_KIT / "data").glob("*.json")):
        mapname = js.stem.replace("fps-", "").replace(".mapdata", "")
        for site in ("A", "B"):
            try:
                result = opt.optimize(js, params, site)
            except Exception as exc:  # keep going across maps/sites
                result = dict(approximation=True, error=str(exc))
            (OUT / f"fps-{mapname}-{site}.result.json").write_text(
                json.dumps(result, indent=2, allow_nan=False, default=float) + "\n")
            rows.append(dict(map=mapname, site=site,
                             reachable=result.get("reachable"),
                             route_seconds=result.get("route_seconds"),
                             local_objective=result.get("local_objective"),
                             line_of_sight=result.get("line_of_sight"),
                             error=result.get("error")))
    return rows


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    summary = {}
    if which in ("racing", "both"):
        rp = json.loads((ROOT / "sim/params/racing.json").read_text())
        summary["racing"] = racing(rp["parameters"])
    if which in ("fps", "both"):
        fp = json.loads((ROOT / "sim/params/fps.json").read_text())
        summary["fps"] = fps(fp["parameters"])
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=float) + "\n")
    print(json.dumps(summary, indent=2, default=float))


if __name__ == "__main__":
    main()