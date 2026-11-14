"""Legal neighborhoods for the two strategy files the model is allowed to edit."""

from __future__ import annotations

import json
import pprint

GAMES = {
    "racing": {
        "competition_id": "1eb91364-950b-5725-aa4b-3501b16860ec",
        "game_id": "racing",
        "title": "多人赛车",
    },
    "fps": {
        "competition_id": "2a97f94f-816a-52b7-b4b8-5925db160245",
        "game_id": "fps",
        "title": "多人枪战：爆破模式",
    },
}

RACING_A = {
    "name": "early-undercut",
    "thesis": (
        "Start on softs and pit at the end of lap 1 for medium, which is the "
        "second dry compound. Run the official driver's attack margins so the "
        "1-second Parkland detector is reachable, and keep the sealed car on "
        "the template book."
    ),
    "qual_margin": 1.0,
    "race_margin": 0.995,
    "preview": 0.2,
    "latency": 0.05,
    "ld_min": 7,
    "ld_time": 0.35,
    "yaw_gain": 0.8,
    "kv": 1.6,
    "brake_gain": 1.04,
    "stops": [{"lap": 1, "compound": "medium", "request_ahead_m": 180}],
    "front_wing": 20,
    "rear_wing": 22,
    "fuel_load_kg": 12,
    "ballast_kg": 40,
    "radiator_opening": 0.7,
    "ers_harvest_level": 5,
    "start_compound": "soft",
    "pressure_front_psi": 29,
    "pressure_rear_psi": 28,
}

FPS_A = {
    "name": "entry-frag",
    "thesis": (
        "Take the duel before the objective. Buy a rifle, push the contact, "
        "and shoot the first enemy in view once this seat has stopped. Plant "
        "or defuse after that fight."
    ),
    "default_site": "B",
    "expect_site": "B",
    "buy_style": "full_rifle",
    "utility": "flashbang",
    "post_plant": "hunt",
    "ct_setup": "mid",
    "execute_live_seconds": 12,
}


def seed(game: str) -> dict:
    return json.loads(json.dumps(RACING_A if game == "racing" else FPS_A))


def _clip(value, low, high):
    return max(low, min(high, value))


def _text(value, limit):
    text = " ".join(str(value or "").split())
    return text[:limit] or "counter"


def clamp(game: str, raw: dict, base: dict) -> dict:
    """Overlay a model proposal onto the current best, inside the legal band."""
    merged = json.loads(json.dumps(base))
    raw = raw or {}
    merged["name"] = _text(raw.get("name", merged["name"]), 48)
    merged["thesis"] = _text(raw.get("thesis", merged["thesis"]), 600)
    if game == "racing":
        return _clamp_racing(merged, raw)
    return _clamp_fps(merged, raw)


def _num(raw, key, default, low, high, places=3):
    try:
        value = float(raw.get(key, default))
    except (TypeError, ValueError):
        value = float(default)
    return round(_clip(value, low, high), places)


def _clamp_racing(merged, raw):
    merged["qual_margin"] = _num(raw, "qual_margin", merged["qual_margin"], 0.97, 1.0)
    merged["race_margin"] = _num(raw, "race_margin", merged["race_margin"], 0.97, 1.0)
    merged["preview"] = _num(raw, "preview", merged["preview"], 0.12, 0.40)
    merged["latency"] = _num(raw, "latency", merged["latency"], 0.02, 0.10)
    merged["ld_min"] = int(_num(raw, "ld_min", merged["ld_min"], 6, 10, 0))
    merged["ld_time"] = _num(raw, "ld_time", merged["ld_time"], 0.25, 0.45)
    merged["yaw_gain"] = _num(raw, "yaw_gain", merged["yaw_gain"], 0.5, 1.2)
    merged["kv"] = _num(raw, "kv", merged["kv"], 1.0, 2.2)
    merged["brake_gain"] = _num(raw, "brake_gain", merged["brake_gain"], 0.95, 1.15)
    merged["front_wing"] = int(_num(raw, "front_wing", merged["front_wing"], 18, 25, 0))
    merged["rear_wing"] = _num(raw, "rear_wing", merged["rear_wing"], 18, 26, 1)
    merged["fuel_load_kg"] = int(_num(raw, "fuel_load_kg", merged["fuel_load_kg"], 7, 14, 0))
    if "ballast_kg" in raw or "ballast_kg" in merged:
        ballast = _num(raw, "ballast_kg", merged.get("ballast_kg", 40), 20, 40, 1)
        if "ballast_kg" in merged or ballast != 40:
            merged["ballast_kg"] = ballast
    merged["radiator_opening"] = _num(raw, "radiator_opening", merged["radiator_opening"], 0.55, 0.85, 2)
    merged["ers_harvest_level"] = int(_num(raw, "ers_harvest_level", merged["ers_harvest_level"], 4, 6, 0))
    merged["pressure_front_psi"] = int(_num(raw, "pressure_front_psi", merged["pressure_front_psi"], 27, 30, 0))
    merged["pressure_rear_psi"] = int(_num(raw, "pressure_rear_psi", merged["pressure_rear_psi"], 26, 30, 0))
    start = raw.get("start_compound", merged["start_compound"])
    if start not in ("soft", "medium", "hard"):
        start = merged["start_compound"]
    stop_raw = raw.get("stops") or merged["stops"]
    first = stop_raw[0] if isinstance(stop_raw, list) and stop_raw else {}
    compound = first.get("compound", merged["stops"][0]["compound"])
    if compound not in ("soft", "medium", "hard"):
        compound = "medium" if start != "medium" else "soft"
    if compound == start:
        compound = "medium" if start != "medium" else "hard"
    lap = int(_num(first, "lap", merged["stops"][0]["lap"], 1, 2, 0))
    ahead = int(_num(first, "request_ahead_m", merged["stops"][0].get("request_ahead_m", 160), 120, 200, 0))
    merged["start_compound"] = start
    merged["stops"] = [{"lap": lap, "compound": compound, "request_ahead_m": ahead}]
    return merged


def _choice(raw, key, default, options):
    value = raw.get(key, default)
    return value if value in options else default


def _clamp_fps(merged, raw):
    merged["default_site"] = _choice(raw, "default_site", merged["default_site"], ("A", "B"))
    merged["expect_site"] = _choice(raw, "expect_site", merged["expect_site"], ("A", "B"))
    merged["buy_style"] = _choice(
        raw, "buy_style", merged["buy_style"],
        ("armor_pistol", "force_rifle", "smg_force", "full_rifle"),
    )
    merged["utility"] = _choice(
        raw, "utility", merged["utility"],
        ("none", "flashbang", "smokegrenade", "molotov"),
    )
    merged["post_plant"] = _choice(raw, "post_plant", merged["post_plant"], ("hold", "molotov", "hunt"))
    merged["ct_setup"] = _choice(raw, "ct_setup", merged["ct_setup"], ("retake", "stack_expect", "mid"))
    merged["execute_live_seconds"] = int(_num(
        raw, "execute_live_seconds", merged["execute_live_seconds"], 12, 45, 0
    ))
    return merged


def render(strategy: dict) -> str:
    body = pprint.pformat(strategy, width=100, sort_dicts=False)
    return f"STRATEGY = {body}\n"


def design_document(base: dict, strategy: dict) -> dict:
    """Write the sealed racing book. Only fields inside the legal band change."""
    document = json.loads(json.dumps(base))
    design = document["design"]
    design["aero"]["front_wing"] = strategy["front_wing"]
    design["aero"]["rear_wing"] = strategy["rear_wing"]
    design["power_unit"]["fuel_load_kg"] = strategy["fuel_load_kg"]
    if "ballast_kg" in strategy:
        design["mass"]["ballast_kg"] = strategy["ballast_kg"]
    design["power_unit"]["ers_harvest_level"] = strategy["ers_harvest_level"]
    design["cooling"]["radiator_opening"] = strategy["radiator_opening"]
    design["tyres"]["start_compound"] = strategy["start_compound"]
    design["tyres"]["pressure_front_psi"] = strategy["pressure_front_psi"]
    design["tyres"]["pressure_rear_psi"] = strategy["pressure_rear_psi"]
    return document


_RACING_GROUPS = {
    "stop": ("start_compound", "stops"),
    "pace": (
        "qual_margin", "race_margin", "preview", "latency", "ld_min", "ld_time",
        "yaw_gain", "kv", "brake_gain",
    ),
    "car": (
        "front_wing", "rear_wing", "fuel_load_kg", "ballast_kg", "radiator_opening",
        "ers_harvest_level", "pressure_front_psi", "pressure_rear_psi",
    ),
}

_FPS_GROUPS = {
    "site": ("default_site", "expect_site"),
    "setup": ("ct_setup",),
    "buy": ("buy_style",),
    "utility": ("utility", "post_plant"),
    "timing": ("execute_live_seconds",),
}

_GROUP_WORDS = {
    "stop": ("pit", "undercut", "overcut", "compound", "soft", "medium", "hard", "lap"),
    "pace": ("margin", "preview", "latency", "pace", "brake"),
    "car": ("wing", "fuel", "ballast", "pressure", "radiator", "ers"),
    "site": ("site",),
    "setup": ("retake", "mid", "stack", "defense", "stand"),
    "buy": ("buy", "rifle", "armor", "economy"),
    "utility": ("molotov", "smoke", "flash", "utility", "grenade"),
    "timing": ("second", "plant", "execute", "early", "late"),
}


def caused_change(game: str, proposed: dict, base: dict) -> dict:
    """Enforce one lever by selecting one of the model's own changes, never by reverting.

    RSI: a cycle that publishes the identical book is a wasted round. When several
    levers changed, keep the one the thesis names (else the first changed) and hold
    the rest — but always apply a real change rather than copying the base back.
    """
    groups = _RACING_GROUPS if game == "racing" else _FPS_GROUPS
    changed = [name for name, keys in groups.items() if any(proposed.get(key) != base.get(key) for key in keys)]
    if len(changed) <= 1:
        return proposed
    thesis = (proposed.get("thesis") or "").lower()
    named = [name for name in changed if any(word in thesis for word in _GROUP_WORDS[name])]
    kept = json.loads(json.dumps(base))
    kept["name"] = proposed.get("name") or base.get("name")
    chosen = (named or changed)[0]
    for key in groups[chosen]:
        kept[key] = proposed[key]
    held = [name for name in changed if name != chosen]
    text = proposed.get("thesis") or base.get("thesis") or ""
    prefix = f"One lever only: applied {chosen}" + (f", held {', '.join(held)}" if held else "") + ". "
    kept["thesis"] = _text(prefix + text, 600)
    return kept


def stop_kind(strategy: dict | None) -> str | None:
    if not strategy:
        return None
    stop = (strategy.get("stops") or [{}])[0]
    if stop.get("lap") == 1 and strategy.get("start_compound") == "soft":
        return "undercut"
    if stop.get("lap") == 2 and stop.get("compound") == "soft":
        return "overcut"
    return None


_FPS_FIELDS = (
    "default_site", "expect_site", "buy_style", "utility",
    "post_plant", "ct_setup", "execute_live_seconds",
)

def fps_snapshot(strategy: dict | None) -> dict | None:
    if not strategy or "ct_setup" not in strategy:
        return None
    snap = {"name": strategy.get("name")}
    for key in _FPS_FIELDS:
        if key in strategy:
            snap[key] = strategy[key]
    return snap


def fps_score(result: dict | None) -> tuple | None:
    if not isinstance(result, dict):
        return None
    score = result.get("seat_score")
    rounds = result.get("team_rounds")
    if score is None and rounds is None:
        return None
    return (
        float(score) if score is not None else -1.0,
        float(rounds) if rounds is not None else -1.0,
    )


def _fps_value(plan: dict, factor: str):
    if factor == "site":
        return plan.get("default_site")
    return plan.get(factor)


def _fmt_score(score: tuple | None) -> str:
    if not score:
        return "no score"
    return f"score {score[0]:.0f}, rounds {score[1]:.0f}"


def fps_measurement_text(records: list) -> str:
    if not records:
        return "No shooter lever has a score yet."
    lines = ["Measured shooter levers:"]
    for factor in ("ct_setup", "site", "post_plant"):
        values = {}
        for plan, score in records:
            value = _fps_value(plan, factor)
            if value is None:
                continue
            if value not in values or score > values[value]:
                values[value] = score
        if not values:
            continue
        shown = ", ".join(f"{value} ({_fmt_score(score)})" for value, score in values.items())
        lines.append(f"{factor}: {shown}")
    return "\n".join(lines)


def canonical(strategy: dict) -> str:
    return json.dumps(strategy, sort_keys=True, ensure_ascii=False)
