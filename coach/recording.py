"""Public match recordings. Reads public_events only, never public_frames."""

from __future__ import annotations

import json

from coach.agx import Agx, AgxError

_BASE = "/api/v1/python-submissions/recordings"

# Bump when the facts or the rendered text change, so cached summaries are re-read.
SCHEMA = 6


def fetch_events(client: Agx, job_id: str) -> list[dict] | None:
    """Every public event object, or None when this job has no recording."""
    try:
        _status, _headers, body = client.get(f"{_BASE}/{job_id}/replay/index")
    except AgxError as exc:
        if exc.status == 404:
            return None
        raise
    chunks = (_layout(body).get("public_events") or [])
    # Chunks are fixed-size and cut objects in half, so join before parsing.
    blob = b""
    for n in range(len(chunks)):
        _status, _headers, part = client.get(
            f"{_BASE}/{job_id}/replay/streams/public_events/chunks/{n}"
        )
        blob += part
    events = []
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _layout(body: bytes) -> dict:
    text = body.decode("utf-8", "replace")
    start = text.find('{"layout"')
    if start < 0:
        start = text.find("{")
    if start < 0:
        return {}
    # The FPS index is AGXI2: JSON followed by a binary frame table.
    parsed, _end = json.JSONDecoder().raw_decode(text[start:])
    return parsed.get("layout") or {}


def read(client: Agx, game: str, job_id: str, seat, team) -> dict | None:
    if not job_id or not isinstance(seat, int):
        return None
    try:
        events = fetch_events(client, job_id)
    except (AgxError, json.JSONDecodeError):
        return None
    if not events:
        return None
    facts = _racing(events, seat) if game == "racing" else _fps(events, seat, team)
    if not facts:
        return None
    facts["seat"] = seat
    if game == "racing":
        try:
            metadata = static_metadata(client, job_id)
            track = metadata.get("track") or {}
            facts["track_id"] = track.get("id")
            facts["track_length_m"] = track.get("length_m")
            cars = static_cars(client, job_id, metadata)
        except (AgxError, json.JSONDecodeError, ValueError):
            cars = None
        if cars:
            facts["cars"] = cars
    return {"job_id": job_id, "seat": seat, "schema": SCHEMA, "facts": facts, "text": _text(game, facts)}


def static_metadata(client: Agx, job_id: str) -> dict:
    _s, _h, body = client.get(f"{_BASE}/{job_id}/replay/streams/public_static/chunks/0")
    for line in body.decode("utf-8", "replace").splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("type") == "initial_metadata":
                return record.get("metadata") or {}
    return {}


def static_cars(client: Agx, job_id: str, metadata=None) -> list[dict] | None:
    """Every seat's design_summary from the public_static stream."""
    cars = (static_metadata(client, job_id) if metadata is None else metadata).get("cars")
    if not cars:
        return None
    rows = []
    for car in cars:
        design = car.get("design_summary") or {}
        if not isinstance(design, dict):
            continue
        rows.append({
            "seat": car.get("seat"),
            "fuel_load_kg": design.get("fuel_load_kg"),
            "ballast_kg": design.get("ballast_kg"),
            "front_wing": design.get("front_wing"),
            "rear_wing": design.get("rear_wing"),
            "radiator_opening": design.get("radiator_opening"),
            "ride_height_mm": design.get("ride_height_mm"),
            "weight_front_pct": design.get("weight_front_pct"),
            "final_drive": design.get("final_drive"),
            "brake_bias_front_pct": design.get("brake_bias_front_pct"),
            "ers_harvest_level": design.get("ers_harvest_level"),
            "start_compound": design.get("start_compound"),
        })
    rows.sort(key=lambda row: row["seat"] if isinstance(row["seat"], int) else 99)
    return rows or None


def _racing(events: list[dict], seat: int) -> dict:
    def rows(kind):
        return [e for e in events if e.get("kind") == kind and seat in (e.get("seats") or [])]

    qual = [e["payload"] for e in rows("qualifying_lap") if e["payload"].get("valid")]
    mine = min((p["lap_time_s"] for p in qual), default=None)
    others = sorted(
        e["payload"]["lap_time_s"]
        for e in events
        if e.get("kind") == "qualifying_lap"
        and e["payload"].get("valid")
        and seat not in (e.get("seats") or [])
    )
    finish = [
        e for e in events
        if e.get("kind") == "finished" and e["payload"].get("total_time_s") is not None
    ]
    ours = next((e for e in finish if seat in (e.get("seats") or [])), None)
    winner = min(finish, key=lambda e: e["payload"]["position"], default=None)
    pits = [e["payload"] for e in rows("pit_exit")]
    laps = [
        {"lap": e["payload"].get("lap"), "lap_time_s": e["payload"].get("lap_time_s")}
        for e in rows("fastest_lap")
    ]
    retired = [{"phase": e.get("phase"), "tick": e.get("tick"), **(e.get("payload") or {})}
               for e in rows("retired")]
    rejected = [e.get("payload") or {} for e in rows("design_rejected")]
    invalid_qual = [e.get("payload") or {} for e in rows("qualifying_lap")
                    if not (e.get("payload") or {}).get("valid")]
    if mine is None and ours is None and not retired and not rejected and not invalid_qual:
        return {}
    our_time = ours["payload"]["total_time_s"] if ours else None
    win_time = winner["payload"]["total_time_s"] if winner else None
    weather = [e["payload"] for e in events if e.get("kind") == "weather_change"]
    passes = [e["payload"] for e in events if e.get("kind") == "overtake"]
    energy = [
        e["payload"] for e in events
        if e.get("kind") == "energy_mode" and seat in (e.get("seats") or [])
    ]
    modes: dict[int, int] = {}
    for payload in energy:
        mode = payload.get("ers_mode")
        modes[mode] = modes.get(mode, 0) + 1
    soc = [p["ers_soc_mj"] for p in energy if p.get("ers_soc_mj") is not None]
    return {
        "field_results": [
            {"seat": rival,
             "qualifying_s": min((e['payload']['lap_time_s'] for e in events
                 if e.get('kind')=='qualifying_lap' and rival in (e.get('seats') or [])
                 and e.get('payload',{}).get('valid') and e['payload'].get('lap_time_s') is not None), default=None),
             "finish_position": next((e['payload'].get('position') for e in finish if rival in (e.get('seats') or [])), None),
             "finish_time_s": next((e['payload'].get('total_time_s') for e in finish if rival in (e.get('seats') or [])), None)}
            for rival in sorted({s for e in events for s in (e.get('seats') or []) if isinstance(s,int)})
        ],
        "retirements": retired,
        "design_rejections": rejected,
        "invalid_qualifying": invalid_qual,
        "finished": ours is not None,
        "qualifying_s": mine,
        "qualifying_fastest": bool(mine is not None and others and mine < others[0]),
        "next_qualifying_s": others[0] if others else None,
        "race_laps_seen": laps,
        "pit_stops": pits,
        "pit_requests": [e["payload"].get("requested_compound") for e in rows("pit_entry")],
        "finish_time_s": our_time,
        "finish_position": ours["payload"].get("position") if ours else None,
        "finish_laps": ours["payload"].get("lap") if ours else None,
        "winner_time_s": win_time,
        "gap_to_winner_s": round(our_time - win_time, 3) if our_time is not None and win_time is not None else None,
        "finishers": len(finish),
        "weather_start": weather[0].get("condition") if weather else None,
        "weather_end": weather[-1].get("condition") if weather else None,
        "weather_changes": len(weather),
        "max_track_wetness": round(max((w.get("track_wetness") or 0) for w in weather), 3) if weather else None,
        # The driver latches wet_race on the first wet tick and never clears it.
        "wet_race": any(w.get("condition") == "wet" for w in weather) if weather else None,
        "pit_stops_field": sum(1 for e in events if e.get("kind") == "pit_exit"),
        "pit_requests_field": sum(1 for e in events if e.get("kind") == "pit_entry"),
        "passes_made": sum(1 for p in passes if p.get("passer") == seat),
        "passes_conceded": sum(1 for p in passes if p.get("passed") == seat),
        "incidents": sum(1 for e in events if e.get("kind") == "incident" and seat in (e.get("seats") or [])),
        "ers_modes": modes,
        "ers_soc_min": round(min(soc), 3) if soc else None,
        "ers_soc_max": round(max(soc), 3) if soc else None,
    }


def _fps(events: list[dict], seat: int, team) -> dict:
    ends = [e for e in events if e.get("type") == "round_end"]
    kills = [e for e in events if e.get("type") == "kill"]
    if not ends:
        return {}
    final = ends[-1]
    wins = final.get("round_wins") or []
    reasons: dict[str, int] = {}
    for event in ends:
        reason = event.get("reason") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    shots = [e for e in events if e.get("type") == "fire" and e.get("player") == seat]
    hits = [e for e in events if e.get("type") == "hit" and e.get("player") == seat]
    plants = [e for e in events if e.get("type") == "plant_start" and e.get("player") == seat]
    starts = [e for e in events if e.get("type") == "round_start"]
    attack = {r.get("round_index") for r in starts if isinstance(team, int) and r.get("attack_team") == team}
    by_round = {e.get("round_index"): e for e in ends}

    def split(indices):
        counts: dict[str, int] = {}
        for index in indices:
            event = by_round.get(index)
            if event:
                reason = event.get("reason") or "unknown"
                counts[reason] = counts.get(reason, 0) + 1
        return counts

    return {
        "round_wins": wins,
        "our_team": team,
        "match_winner": final.get("round_winner"),
        "rounds": len(ends),
        "round_end_reasons": reasons,
        "our_kills": sum(1 for e in kills if seat in (e.get("attackers") or [])),
        "our_deaths": sum(1 for e in kills if e.get("target") == seat),
        "total_kills": len(kills),
        "our_shots": len(shots),
        "our_hits": len(hits),
        "our_accuracy": round(len(hits) / len(shots), 3) if shots else None,
        "our_weapons": sorted({e.get("weapon") for e in shots if e.get("weapon")}),
        "our_plants": len(plants),
        "our_plant_sites": sorted({e.get("site") for e in plants if e.get("site")}),
        "field_plants": sum(1 for e in events if e.get("type") == "plant_start"),
        "attack_rounds": len(attack),
        "attack_reasons": split(attack),
        "defend_reasons": split(set(by_round) - attack),
        "our_faults": sum(1 for e in events if e.get("type") == "fault" and e.get("seat") == seat),
        "our_reloads": sum(1 for e in events if e.get("type") == "reload_start" and e.get("player") == seat),
    }


def _text(game: str, facts: dict) -> str:
    if game == "racing":
        return _racing_text(facts)
    return _fps_text(facts)


def _racing_text(f: dict) -> str:
    lines = ["Recording (public events, our seat):", f"Track: {f.get('track_id', 'unknown')}."]
    for retirement in f.get("retirements") or []:
        lines.append(f"RETIRED during {retirement.get('phase')}: {retirement.get('reason')}, lap {retirement.get('lap')}.")
    for rejection in f.get("design_rejections") or []:
        lines.append("DESIGN REJECTED: " + json.dumps(rejection, ensure_ascii=False))
    if f.get("finished") is False:
        lines.append("This seat did NOT finish. Rank-array placement is not a classified finish or a pace measurement.")
    qual = f.get("qualifying_s")
    if qual is not None:
        line = f"Qualifying {qual:.3f}s"
        if f.get("qualifying_fastest"):
            line += f", fastest in the field (next {f['next_qualifying_s']:.3f}s)"
        elif f.get("next_qualifying_s") is not None:
            line += f", next best {f['next_qualifying_s']:.3f}s"
        lines.append(line + ".")
    laps = f.get("race_laps_seen") or []
    if laps:
        lines.append(
            "Race laps published for this seat: "
            + ", ".join(f"lap {l['lap']} {l['lap_time_s']:.3f}s" for l in laps)
            + ". No other per-lap times are in the public events."
        )
    else:
        lines.append("No race lap time for this seat is published in the public events.")
    if f.get("weather_changes"):
        line = (
            f"Weather: {f.get('weather_start')} to {f.get('weather_end')} over "
            f"{f['weather_changes']} changes, peak track wetness {f.get('max_track_wetness')}."
        )
        if f.get("wet_race"):
            line += (
                " The race went wet, so the driver latched its wet-race waiver: the scheduled dry"
                " stop is cancelled unless tyre wear passes 0.8, and the two-compound rule is waived."
            )
        lines.append(line)
    for pit in f.get("pit_stops") or []:
        lines.append(
            f"Pit stop: {pit.get('old_compound')} to {pit.get('new_compound')}, "
            f"stationary {pit.get('stationary_s')}s, {pit.get('pit_elapsed_s'):.1f}s in the pit."
        )
    if not f.get("pit_stops"):
        lines.append(
            f"This seat completed no pit stop. Whole field: {f.get('pit_stops_field', 0)} completed"
            f" stops, {f.get('pit_requests_field', 0)} pit requests."
        )
    if f.get("pit_requests"):
        lines.append("This seat requested: " + ", ".join(str(c) for c in f["pit_requests"]) + ".")
    lines.append(
        f"Overtakes: this seat passed {f.get('passes_made', 0)} and was passed"
        f" {f.get('passes_conceded', 0)} times; {f.get('incidents', 0)} incidents."
    )
    if f.get("ers_modes"):
        modes = ", ".join(f"mode {m} x{n}" for m, n in sorted(f["ers_modes"].items(), key=lambda kv: str(kv[0])))
        lines.append(
            f"ERS telemetry for this seat: {modes}. Battery {f.get('ers_soc_min')} to"
            f" {f.get('ers_soc_max')} MJ."
        )
    if f.get("finish_time_s") is not None:
        lines.append(
            f"Finish {f['finish_time_s']:.3f}s over {f.get('finish_laps')} laps, "
            f"position {f['finish_position']} of {f['finishers']} finishers, "
            f"{f['gap_to_winner_s']:.1f}s behind the winner ({f['winner_time_s']:.3f}s)."
        )
    field = _field_text(f.get("cars") or [], f.get("seat"))
    if f.get("field_results"):
        lines.append("Same-match timing by seat (pair with the car table; different drivers and weather still confound causality): " + json.dumps(f["field_results"]))
    if field:
        lines.append(field)
    return "\n".join(lines)


def _num_text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _field_text(cars: list, seat) -> str:
    """One line per car for the setup values that can differ, plus the shared ones."""
    if not cars:
        return ""
    lines = ["Field cars from public_static design_summary:"]
    for car in cars:
        mark = " us" if car.get("seat") == seat else ""
        lines.append(
            f"seat {car.get('seat')}{mark}: fuel {_num_text(car.get('fuel_load_kg'))} "
            f"ballast {_num_text(car.get('ballast_kg'))} "
            f"wings {_num_text(car.get('front_wing'))}/{_num_text(car.get('rear_wing'))} "
            f"radiator {_num_text(car.get('radiator_opening'))} "
            f"ers {_num_text(car.get('ers_harvest_level'))} "
            f"start {car.get('start_compound')}"
        )
    shared = []
    for key, label in (
        ("ride_height_mm", "ride height"),
        ("weight_front_pct", "front weight"),
        ("final_drive", "final drive"),
        ("brake_bias_front_pct", "brake bias"),
    ):
        values = {json.dumps(car.get(key), sort_keys=True) for car in cars}
        if len(values) == 1:
            shared.append(f"{label} {_num_text(cars[0].get(key))}")
    if shared:
        lines.append("Same on every car: " + ", ".join(shared) + ".")
    return "\n".join(lines)


def _fps_text(f: dict) -> str:
    wins = f.get("round_wins") or []
    team = f.get("our_team")
    ours = wins[team] if isinstance(team, int) and team < len(wins) else None

    def reasons(counts):
        return ", ".join(f"{n} {r}" for r, n in sorted((counts or {}).items())) or "none"

    lines = [
        "Recording (public events, our seat):",
        f"Final round_wins {wins}. Our team {team}, match winner team {f.get('match_winner')}.",
        f"{f.get('rounds')} rounds ended: "
        + ", ".join(f"{count} {reason}" for reason, count in sorted((f.get("round_end_reasons") or {}).items()))
        + ".",
        f"This seat: {f.get('our_kills')} kills, {f.get('our_deaths')} deaths. "
        f"Whole match: {f.get('total_kills')} kills.",
    ]
    if ours is not None:
        lines.append(f"Our team won {ours} of {f.get('rounds')} rounds.")
    accuracy = f.get("our_accuracy")
    shot_line = (
        f"This seat fired {f.get('our_shots', 0)} shots and landed {f.get('our_hits', 0)} hits"
        + (f" ({accuracy:.0%} accuracy)" if accuracy is not None else " (no shots fired)")
        + f", weapons {', '.join(f.get('our_weapons') or []) or 'none'},"
        f" {f.get('our_reloads', 0)} reloads, {f.get('our_faults', 0)} faults."
    )
    lines.append(shot_line)
    lines.append(
        f"This seat planted {f.get('our_plants', 0)} times"
        + (f" at {', '.join(f['our_plant_sites'])}" if f.get("our_plant_sites") else "")
        + f"; the whole field planted {f.get('field_plants', 0)} times."
    )
    lines.append(
        f"We attacked {f.get('attack_rounds', 0)} rounds ({reasons(f.get('attack_reasons'))})"
        f" and defended the rest ({reasons(f.get('defend_reasons'))})."
    )
    if not f.get("our_hits") and f.get("our_shots"):
        lines.append(
            "Every shot this seat fired missed. The local gate's aim check does not reproduce the"
            " real engine, so a passing gate is not evidence of hitting anything."
        )
    if not f.get("our_plants") and f.get("attack_rounds"):
        lines.append(
            "This seat never planted while attacking. A timeout on attack means the bomb was never"
            " planted by anyone on our team."
        )
    lines.append(
        "A bomb_explode round is a planted bomb that detonated; a defuse round is a planted bomb "
        "that was defused; a timeout round ended with no bomb resolved."
    )
    return "\n".join(lines)