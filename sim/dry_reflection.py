"""Read-only dry reflection: run the loop's evidence() (with sim) + propose() for
both games so oMLX consumes the simulation and produces a one-lever RSI change.
NO upload, NO state mutation (propose does not save_state). For pre-round inspection.
"""
import json, glob, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import coach.loop as L
from coach.config import load
from coach.agx import Agx

cfg = load(); client = Agx(cfg.base_url, cfg.api_key)
out = {}
for game in ("racing", "fps"):
    p = sorted(glob.glob(str(ROOT / f"state/logs/{game}/*.json")), key=os.path.getmtime)[-1]
    focus = json.load(open(p)).get("result", {})
    state = json.load(open(ROOT / f"state/{game}.json"))
    best = state["A"]["strategy"]
    report = L.evidence(state, "loss", focus, client=client)   # includes the sim block
    sim = report[report.find("Local simulation"):report.find("Local simulation")+400] if "Local simulation" in report else "(no sim)"
    trace = {}
    try:
        cand = L.propose(cfg, game, best, report, "loss", trace=trace)
        changed = not L._same_book(cand, best)
        out[game] = dict(ok=True, sim_block=sim, changed=changed,
                         best_name=best.get("name"), candidate=cand)
        print(f"\n===== {game} =====", flush=True)
        print("SIM oMLX saw:\n" + sim, flush=True)
        print(f"\nbest '{best.get('name')}' -> candidate '{cand.get('name')}' | changed={changed}", flush=True)
        print("thesis:", cand.get("thesis"), flush=True)
        print("candidate:", json.dumps(cand, ensure_ascii=False), flush=True)
    except Exception as exc:
        out[game] = dict(ok=False, error=str(exc), sim_block=sim)
        print(f"\n===== {game} ERROR: {exc}", flush=True)

Path("/tmp/dry_reflection.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print("\nSaved /tmp/dry_reflection.json", flush=True)