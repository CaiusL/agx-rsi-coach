"""Derive racing calibration from OUR OWN match data, lean on the network.

Historical public-only fallback collector. Own-seat training exports are now fetched
by coach.training through /api/v1/python-submissions/jobs/{id}/training.jsonl.
This collector independently uses:
  * stored recording.facts.qualifying_s  -> per-track flying-lap targets (no net)
  * public_static (small)                -> match -> track id
  * public_events from ONE match         -> real ERS drain (energy_mj_per_s)

Writes sim/params/racing_measured.json. Never prints credentials.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coach.config import load
from coach.agx import Agx
from coach.recording import fetch_events

STATIC = "/api/v1/python-submissions/recordings/{mid}/replay/streams/public_static/chunks/0"


def track_of(agx: Agx, mid: str) -> str | None:
    try:
        _s, _h, body = agx.get(STATIC.format(mid=mid))
    except Exception:
        return None
    for line in body.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        md = (json.loads(line).get("metadata") or {})
        if (md.get("track") or {}).get("id"):
            return md["track"]["id"]
    return None


def own_seat(match: dict) -> int | None:
    for p in match.get("participants", []):
        if p.get("is_mine"):
            return p.get("seat")
    return match.get("seat")


def energy_from(agx: Agx, mid: str, seat: int) -> list[float]:
    try:
        events = fetch_events(agx, mid)
    except Exception:
        return []
    drains, prev = [], None
    for e in events or []:
        if e.get("kind") != "energy_mode" or seat not in (e.get("seats") or []):
            continue
        soc = (e.get("payload") or {}).get("ers_soc_mj")
        t = e.get("time")
        if prev is not None and isinstance(soc, (int, float)):
            t0, soc0 = prev
            dt = t - t0
            if 0 < dt < 0.5 and soc0 > soc:
                drains.append((soc0 - soc) / dt)
        prev = (t, soc)
    return drains


def collect(limit: int = 12) -> dict:
    cfg = load()
    agx = Agx(cfg.base_url, cfg.api_key)
    logs = sorted((ROOT / "state/logs/racing").glob("*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    laps: dict[str, list[float]] = {}
    per_match, drains = [], []
    energy_done = False
    for path in logs:
        d = json.loads(path.read_text())
        mid = d.get("result", {}).get("match_id") or d.get("match", {}).get("id")
        seat = own_seat(d.get("match", {}))
        qual = ((d.get("result", {}).get("recording") or {}).get("facts") or {}).get("qualifying_s")
        if not mid or seat is None:
            continue
        track = track_of(agx, mid)
        time.sleep(0.4)
        if track and isinstance(qual, (int, float)):
            laps.setdefault(track, []).append(qual)
        if not energy_done:
            dr = energy_from(agx, mid, seat)
            if dr:
                drains = dr
                energy_done = True
            time.sleep(0.4)
        per_match.append(dict(match=mid, track=track, seat=seat, qualifying_s=qual))
        print(f"  {mid[:8]} track={track} qual={qual}", flush=True)

    def q(v, p):
        a = np.asarray(v, float); a = a[np.isfinite(a) & (a > 0)]
        return float(np.quantile(a, p)) if len(a) else None

    out = dict(
        source="stored qualifying_s + public_static track id + one-match energy_mode",
        matches=len(per_match),
        energy_drain_samples=len(drains),
        energy_mj_per_s=q(drains, 0.8),
        energy_mj_per_s_median=q(drains, 0.5),
        qualifying_laps_by_track={k: sorted(v) for k, v in laps.items()},
        best_qualifying_by_track={k: min(v) for k, v in laps.items()},
        per_match=per_match,
    )
    (ROOT / "sim/params/racing_measured.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "per_match"},
                     indent=2, ensure_ascii=False))
    return out


if __name__ == "__main__":
    collect(int(sys.argv[1]) if len(sys.argv) > 1 else 12)