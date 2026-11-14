"""Local-simulation evidence for the reflection loop.

Runs the AGX kit simulator on the CURRENT track/map and compares the estimate
against the winner's measured best, so oMLX can verify a proposed change against
physics and the field rather than rank numbers alone.

The sim is a calibrated approximation, NOT the platform engine: racing is a
particle lap model fitted to our own pace, FPS is a 2D route/visibility search
with no shooting. Its objective is never a platform score. Any failure returns
"" so a round is never blocked.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KITS = {"racing": ROOT / "agx-racing-kit", "fps": ROOT / "agx-fps-kit"}
PARAMS = ROOT / "sim" / "params"
OUT = ROOT / "sim" / "out"
_STATIC = "/api/v1/python-submissions/recordings/{mid}/replay/streams/public_static/chunks/0"

_mods: dict = {}
_ids: dict = {}


def _kit(game: str):
    if game not in _mods:
        path = KITS[game] / "sim" / "optimize.py"
        spec = importlib.util.spec_from_file_location(f"kit_{game}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mods[game] = mod
    return _mods[game]


def _params(game: str) -> dict:
    return json.loads((PARAMS / f"{game}.json").read_text())["parameters"]


def current_id(client, game: str, focus: dict) -> str | None:
    """Track id (racing) or map name (fps) from the match's public_static, cached."""
    mid = (focus or {}).get("match_id")
    if not mid:
        return None
    if mid in _ids:
        return _ids[mid]
    try:
        _s, _h, body = client.get(_STATIC.format(mid=mid))
    except Exception:
        return None
    val = None
    for line in body.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if game == "racing":
            val = ((rec.get("metadata") or {}).get("track") or {}).get("id")
        else:
            raw = (rec.get("map") or {}).get("id") or ""
            val = raw.split("-")[0] or None
        if val:
            break
    _ids[mid] = val
    return val


def _cached(game: str, key: str, run, params=None):
    """Reuse sim/out results when their parameters match; else compute and store."""
    params = _params(game) if params is None else params
    path = OUT / f"{key}.result.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("parameters") == params:
                return data
        except Exception:
            pass
    data = run()
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, default=float))
    except Exception:
        pass
    return data


RACING_SCENARIOS = (("dry", 0.0), ("damp", 0.5), ("wet", 0.95))


def racing_scenarios(track: str) -> dict:
    """One fixed weather grid shared by every book, independent of the latest weather."""
    npz = KITS["racing"] / "data" / f"racing-{track}.mapdata.npz"
    if not npz.exists():
        raise ValueError("Unknown track")
    params = _params("racing")
    signature = hashlib.sha256(npz.read_bytes() + json.dumps(params, sort_keys=True).encode()
        + json.dumps(RACING_SCENARIOS).encode() + (KITS["racing"] / "sim/optimize.py").read_bytes()).hexdigest()
    path = OUT / f"racing-{track}-weather-grid-v2.result.json"
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            if cached.get("signature") == signature:
                return cached
        except (OSError, ValueError):
            pass
    rows = []
    for name, wetness in RACING_SCENARIOS:
        grip = 1.0 - 0.45 * wetness
        scenario = dict(params, lateral=params["lateral"] * grip, brake=params["brake"] * grip)
        result = _kit("racing").optimize(npz, scenario)[0]
        rows.append({"scenario": name, "wetness": wetness,
                     "shared_context_lap_s": round(float(result["estimated_lap_seconds"]), 3)})
    data = {"signature": signature, "track": track, "scenarios": rows,
            "kind": "fixed-physics steady-state flying-lap context, not a full weather-transition race",
            "models_strategy_effects": False,
            "unsupported_strategy_fields": "ALL book fields, including fuel, wings and controller gains"}
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return data


def racing_assessment(track: str, baseline: dict, candidate: dict) -> dict:
    """Expose what the approximation can and cannot compare; never invent car effects."""
    changed = {k: {"baseline": baseline.get(k), "candidate": candidate.get(k)}
               for k in set(baseline) | set(candidate)
               if k not in ("name", "thesis") and baseline.get(k) != candidate.get(k)}
    grid = racing_scenarios(track)
    return {"track": track, "baseline_name": baseline.get("name"), "candidate_name": candidate.get("name"),
            "changed": changed, "shared_weather_grid": grid["scenarios"],
            "predicted_delta_s": None, "rankable_by_simulation": False,
            "reason": "The particle simulator does not consume either strategy book. All changed-field effects are unmodelled. Validate the hypothesis in a match."}


def _racing(track: str, focus: dict) -> str:
    grid = racing_scenarios(track)
    facts = ((focus or {}).get("recording") or {}).get("facts") or {}
    lines = [
        "Local simulation: MATCHED WEATHER CONTEXT, not a strategy evaluator.",
        "A and B mean baseline and experiment. They are NOT separate wet/dry presets.",
        "Use the SAME book across all scenarios below; consider BOTH dry and wet tradeoffs.",
        "These are steady-state flying-lap approximations; they do not simulate a full dry-to-wet race.",
    ]
    for row in grid["scenarios"]:
        lines.append(f"  {track} {row['scenario']}: wetness {row['wetness']:.2f}, shared context lap {row['shared_context_lap_s']:.1f}s.")
    lines += [
        "MODEL LIMIT: this simulator does NOT read fuel_load_kg, front_wing, rear_wing, any other car book value, or controller gains.",
        "Its scenario numbers are shared context for baseline AND candidate; an unchanged estimate is NOT proof a car field is inert.",
        "Fuel and wings ARE applied to the real engine through design.json. Their causal effects remain unmeasured here; do not invent a predicted gain.",
        "Physics parameters were fitted to Parkland dry laps; other tracks and wet scenarios are extrapolations, not engine scores.",
        f"Observed latest weather only: start {facts.get('weather_start')}, wet {facts.get('wet_race')}, peak {facts.get('max_track_wetness')}. This does not change the scenario grid.",
    ]
    ours = facts.get("qualifying_s")
    fastest = facts.get("next_qualifying_s")
    if ours is not None:
        lines.append(f"Our recorded qualifying: {ours:.3f}s; fastest other qualifying: {fastest}. Qualifying weather is not matched to these scenarios, so no sim-versus-qualifying gain is inferred.")
    return "\n".join(lines)


def _fps(mapname: str, focus: dict) -> str:
    js = KITS["fps"] / "data" / f"fps-{mapname}.mapdata.json"
    if not js.exists():
        return ""
    lines = ["Local simulation CONTEXT ONLY (map route estimates; does not run the candidate agent; no shooting):"]
    params = _params('fps')
    calibration = ((focus.get('training') or {}).get('calibration') or {})
    speed = (calibration.get('parameters') or {}).get('move_speed_mps')
    if (focus.get('training') or {}).get('status') == 'verified' and isinstance(speed,(int,float)) and speed > 0:
        params = {**params,'move_speed_mps':speed}
        lines.append(f'  Route speed uses our verified input-log motion envelope {speed:.3f} m/s ({calibration.get("pairs")} pairs); reaction time remains an estimate.')
    for site in ("A", "B"):
        res = _cached("fps", f"fps-{mapname}-{site}",
                      lambda s=site: _kit("fps").optimize(js, params, s), params=params)
        if res.get("reachable"):
            rs = res.get("route_seconds") or [0, 0]
            lines.append(
                f"  site {site}: attack route {rs[0]:.0f}s, defend route {rs[1]:.0f}s, "
                f"spawn-to-site line of sight {res.get('line_of_sight')}."
            )
        else:
            lines.append(f"  site {site}: grid disconnected in this approximation (verify in logs).")
    facts = ((focus or {}).get("recording") or {}).get("facts") or {}
    wins = facts.get("round_wins")
    if wins is not None:
        lines.append(
            f"  Match winner: team {facts.get('match_winner')}; final round_wins {wins}. "
            f"Our plants landed at sites {facts.get('our_plant_sites') or 'none'}."
        )
    return "\n".join(lines)


def sim_insight(client, game: str, focus: dict) -> str:
    """Simulation + winner-comparison block for the reflection. Empty on any failure."""
    try:
        if client is None or game not in ("racing", "fps"):
            return ""
        ident = current_id(client, game, focus)
        if not ident:
            return ""
        return _racing(ident, focus) if game == "racing" else _fps(ident, focus)
    except Exception:
        return ""
