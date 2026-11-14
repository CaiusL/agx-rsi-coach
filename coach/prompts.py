"""Prompts for the local counter-strategy call."""

from __future__ import annotations

import json

RACING_CARD = """
You are writing the next racing strategy for one car in a 10-car AGX Formula race.
Track varies between Parkland, Highland and Crossover; read the current track_id. One qualifying lap, four race laps. Dry finishers must use two dry compounds.
The official driver is frozen. You may change only this JSON object:
{
  "name": "short id",
  "thesis": "why this counters the current best",
  "qual_margin": 0.97 to 1.0,
  "race_margin": 0.97 to 1.0,
  "preview": 0.12 to 0.40,
  "latency": 0.02 to 0.10,
  "ld_min": 6 to 10,
  "ld_time": 0.25 to 0.45,
  "yaw_gain": 0.5 to 1.2,
  "kv": 1.0 to 2.2,
  "brake_gain": 0.95 to 1.15,
  "stops": [{"lap": 1 or 2, "compound": "soft"|"medium"|"hard", "request_ahead_m": 120 to 200}],
  "front_wing": 18 to 25,
  "rear_wing": 18 to 26,
  "fuel_load_kg": 7 to 14,
  "ballast_kg": 20 to 40,
  "radiator_opening": 0.55 to 0.85,
  "ers_harvest_level": 4 to 6,
  "start_compound": "soft"|"medium"|"hard",
  "pressure_front_psi": 27 to 30,
  "pressure_rear_psi": 26 to 30
}
Lower rank is better (0 wins). Rank 6 is 7th of 10. A dry car on one compound is unclassified.
The official driver is frozen. These fields are the only levers, and they are not equal.
WHAT REACHES THE CAR:
- The frozen driver file loads plan.npz and mechanics.npz. It does not open design.json.
  The platform builds the car from design.json, and the public recording publishes that car
  as design_summary for every seat. Fuel, wings, radiator, ballast, ERS, pressures, and the
  start compound in this book are written into design.json and appear in that summary.
- Ride height, front weight, gear ratios, final drive, and brake bias stay on the sealed
  template. When the field table says they are the same on every car, they are not the
  measured difference.
- The nine control fields and the stops are applied to the driver every tick. A car field
  the field table shows on a faster car is also a live lever. Change one lever.
- Pit stops are conditional, not a fixed toll. In Driver.pit():
  * wet_race latches on the first tick whose condition is "wet" and never clears.
  * Once wet_race is set, the scheduled stop is CANCELLED unless max tyre wear exceeds 0.8,
    and the two-compound obligation is waived. A wet race therefore usually has NO stops at all.
  * No stop is ever taken on the final lap, and a lap-0 stop on soft is skipped.
  * In wet conditions a car not on soft is forced onto soft at the next opportunity.
  Read actual pit events; wet races can still include emergency or compound-change stops.
  Do not infer that a stop occurred or was skipped when recording data is missing.
- Wetness scales the tracking gains directly. The driver's safety margin is 1.45 + wet*0.65*(1+thermal),
  its steering lookahead is max(ld_min + 3*wet*(1+thermal), ld_time*v*(1+0.2*wet)), its steer rate is
  scaled by sqrt(1-0.45*wet) and its braking by (1-0.45*wet). So ld_min, ld_time, yaw_gain, kv,
  brake_gain, preview and latency all behave differently in the wet than in the dry. A gain that is
  correct on a dry lap can be wrong on a wet one. The recording reports the peak track wetness.
- qual_margin and race_margin scale the speed reference. 1.0 is full attack; lower is slower.
  Finishing alone does not prove that a margin is stable or optimal.
The evidence carries the public recording when one exists: our qualifying lap, the race lap times
actually published, the weather transition and peak wetness, completed stops and pit requests for
the whole field, our passes made and conceded, our ERS mode histogram and battery range, the
finish time and gap to the winner, and each seat's design_summary from public_static. Those are
measured. The match ranks array can disagree with the recording; the recording wins.
A name ahead of us is still not a tire choice.
EXPERIMENT RULES:
1. Read track_id, asset binding, retirement/design-rejection status and weather before pace.
   Different tracks cannot be compared as a controlled tuning experiment. A retirement from
   damage or mismatched path assets is a reliability failure, not merely a slow finishing lap.
2. Missing recording fields mean UNKNOWN, never dry weather or zero stops. Do not invent
   qualifying times, causes, or weather. A DNF has no classified finish time.
3. Compare like tracks and similar conditions, keeping every other book field constant.
   Peak wetness alone does not match a whole weather trajectory. If several fields changed,
   attribution is confounded; describe a hypothesis instead of claiming an isolated cause.
4. A lower gain or margin can be appropriate after damage/instability. There is no rule forcing
   more aggression after every loss. Let the actual evidence guide ONE scalar change, or the
   coupled starting-compound/stop schedule. Do not restore one gain AND change another field.
5. Field-car design differences are live hypotheses. Avoid repeatedly oscillating one gain
   based on incomparable races. Dry stop comparisons require an actual executed stop.
6. Always consider dry AND wet tradeoffs for the SAME book. Fixed simulation numbers do not
   model fuel, wing, or controller effects; missing sensitivities are uncertainty, not frozen fields.
7. Name the exact old and new value in the thesis, distinguish measured failure from hypothesis,
   and state a testable expected outcome. Return a new name with the next experiment.
""".strip()

FPS_CARD = """
You author the JSON book paired with one seat's Python policy in AGX 5v5 FPS.
Use only fields actually read by the current source. Preserve unused fields.
Legal fields: name, thesis, default_site/expect_site (A or B), buy_style
(armor_pistol/force_rifle/smg_force/full_rifle), utility
(none/flashbang/smokegrenade/molotov), post_plant (hold/molotov/hunt),
ct_setup (retake/stack_expect/mid), execute_live_seconds (12..45).
A loss does not mandate aggression. A team win does not establish individual contribution.
Compare versioned real evidence and describe ONE testable hypothesis. A leaderboard name
or rating does not reveal a strategy. No invented opponent source or settings.
Local testing selects an experiment, never proves a kill-rate or win-rate improvement.
""".strip()


def plan_messages(game: str, best: dict, report: str, mode: str, exploration=None) -> list[dict]:
    card = RACING_CARD if game == "racing" else FPS_CARD
    if game == "fps" and mode != "init":
        task = (
            "Use the current versioned FPS experiment evidence. Select ONE source-grounded live "
            "book lever only if the current policy reads it. Keep every other field unchanged. "
            "A loss does not mandate aggression or rotating preset values. State measured facts, "
            "a hypothesis and its real-match falsifier. Do not invent opponent tactics from ratings."
        )
    elif game == "racing" and mode != "init":
        task = (
            "Read the latest result, its exact played book, track and retirement status first. "
            "The current baseline book can differ from the failed trial. Compare only same-track "
            "evidence; separate dry and wet conditions and state confounders. Never claim a "
            "single cause from a different track, changed weather and multiple changed controls. "
            "If asset binding failed, identify that harness failure before tuning gains. "
            "Change exactly ONE scalar field, or the coupled tyre/stop schedule. No mandatory "
            "aggression: recovery and lower gains can be valid hypotheses. Preserve every other "
            "baseline value. The thesis must name the exact field and old/new value and the "
            "measured fact motivating the hypothesis."
        )
    elif mode == "init":
        task = (
            "This is the initial checkpoint. Our own match has not finished. "
            "Study our current leaderboard rank, the top of the board, the three players "
            "immediately ahead of us, and the latest public result. "
            "Write Plan B to counter the players ahead. The live best stays in the next match, "
            "and Plan B stays in reserve until that best loses. "
            "Copy any field the checkpoint does not identify."
        )
    elif mode == "win":
        task = (
            "The best strategy won. Read our score first, then the counter result and the three players "
            "immediately behind us. Plan B stays in reserve until this best loses. "
            "A win is not a reason to stand still: still change ONE lever to push further (racing: a "
            "control that can lower the finish time; fps: the next unmeasured lever). Copy every other "
            "field. Do not return the same book."
        )
    else:
        task = (
            "The last attempt lost. Read our score first, then the best result and the previous Plan B. "
            "Keep what that score still supports, but you MUST change one lever and be more aggressive "
            "than the book that lost. Returning the same book is forbidden. Copy every other field."
        )
    if mode == "init" or game not in ("racing", "fps"):
        task += (
            " The thesis must be two sentences: the situation, then the one change and why. "
            "Even if no cause is identified, change one lever to test a hypothesis (the next unmeasured "
            "lever); never return the current best unchanged."
        )
    if game == "racing" and exploration is not None:
        task += (
            " Compare fuel_load_kg, front_wing and rear_wing against controller alternatives. "
            "Unchanged car fields are not frozen: the platform reads design.json. Use one weather-robust book, not separate wet/dry presets. "
            "Output exactly {strategy: <complete legal book>, experiment: {lever: <exact changed field>, "
            "why_this_lever: <reason and alternatives>, dry_hypothesis: <expected dry tradeoff>, "
            "wet_hypothesis: <expected wet tradeoff>, uncertainty: <confounders and what a match must test>, "
            "simulation_effect_s: null}} as valid JSON. The fixed-physics simulator cannot predict a book's time gain. "
            "Do not use a wet name as evidence of a wet-only setup. Name the tested lever rather than implying a weather-specific preset. "
        )
        if exploration.get("car_exploration_due"):
            task += " Exploration is due: choose ONE of " + ", ".join(exploration["eligible_car_fields"]) + ". You choose the value; keep every other field copied."
    user = (
        "Current best strategy:\n"
        + json.dumps(best, ensure_ascii=False, indent=2)
        + "\n\nEvidence:\n"
        + report
        + "\n\n"
        + task
        + " RSI rule: every version must change exactly ONE lever versus the current best above; "
          "returning an identical book is a failed round."
        + " If a 'Local simulation' block is present, treat it only as map/weather context: compare the "
          "estimate against measured pace cautiously. It does not run your proposed book or code and cannot verify an improvement. The simulation is "
          "an approximation, not the engine — never treat its objective as a platform score."
        + (
            " Compare at least two matches inside your thinking. "
            "The visible reply is one JSON object and nothing else."
            if mode != "init" else " Return one JSON object and nothing else."
        )
    )
    return [
        {"role": "system", "content": card},
        {"role": "user", "content": user},
    ]


_FPS_BASELINE = """// Legal rule-based baseline. It reads only the referee's seat observation.
let map=null,nodes=[],byId={},adj={},route=[],round=-1,goal='',last=null,stuck=0;
const dist=(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1],(a[2]||0)-(b[2]||0));
const angle=x=>Math.atan2(Math.sin(x),Math.cos(x));
function path(from,to){
  if(!nodes.length)return [to];
  let nearest=p=>nodes.reduce((a,b)=>dist(a.position,p)<dist(b.position,p)?a:b);
  let start=nearest(from),end=nearest(to),cost={[start.id]:0},prev={},open=[start.id],closed={};
  while(open.length){
    open.sort((a,b)=>cost[a]-cost[b]);let id=open.shift();if(closed[id])continue;closed[id]=1;if(id===end.id)break;
    for(let e of adj[id]||[]){let next=byId[e.to];if(!next)continue;
      let node=byId[id],c=cost[id]+dist(node.position,next.position);
      if(cost[e.to]===undefined||c<cost[e.to]){cost[e.to]=c;prev[e.to]=id;open.push(e.to);}}
  }
  if(cost[end.id]===undefined)return [to];
  let ids=[end.id];while(ids[0]!==start.id&&prev[ids[0]]!==undefined)ids.unshift(prev[ids[0]]);
  // A nearby entry node already represents our current navigable region.
  // Sending opposite spawn offsets into its centre causes a head-on crowd
  // deadlock before either player can reach the outgoing edge.
  if(ids.length>1&&dist(from,start.position)<3)ids.shift();
  return ids.map(id=>byId[id].position).concat([to]);
}
function act(o){
  if(o.map){map=o.map;nodes=map.nav?.nodes||[];byId={};for(let n of nodes)byId[n.id]=n;adj={};for(let e of map.nav?.edges||[]){
    // Source adjacency is a route hypothesis; actual movement still obeys collisions.
    (adj[e.from]??=[]).push(e);if(e.kind==='walk'||e.kind==='crouch'||e.kind==='jump'||e.kind==='source-adjacency')
      (adj[e.to]??=[]).push({to:e.from,kind:e.kind});}}
  let p=o.me,a={weapon_slot:p.slots.primary?0:1},pos=[p.x,p.y,p.z];
  if(!p.alive)return a;
  if(o.round_index!==round){round=o.round_index;route=[];goal='';last=null;stuck=0;}
  if(o.phase==='buy'){let gun=o.side==='attack'?'ak47':'m4a1';
    if(!p.slots.primary&&p.cash>=(o.side==='attack'?2700:2900))a.buy=gun;
    else if(p.armor<100&&p.cash>=650)a.buy='armor';
    return a;}
  let target=o.visible?.[0];
  if(target){let dx=target.x-p.x,dy=target.y-p.y,dz=target.z+1.64-(p.z+1.64),yaw=Math.atan2(-dy,dx),pitch=Math.atan2(dz,Math.hypot(dx,dy));
    const held=p.weapons?.[a.weapon_slot];yaw-=2*(held?.recoil_yaw||0);pitch-=2*(held?.recoil_pitch||0);
    a.yaw_rate=Math.max(-1,Math.min(1,angle(yaw-p.yaw)/.15));a.pitch_rate=Math.max(-1,Math.min(1,(pitch-p.pitch)/.10));
    a.fire=Math.abs(angle(yaw-p.yaw))<.15&&(p.weapons?.[a.weapon_slot]?.id==='pistol'||p.weapons?.[a.weapon_slot]?.id==='usp'?Math.floor(o.match_tick/3)%5===0:true);
    a.reload=p.ammo===0;return a;}
  let sites=map?.sites||[],site=sites[(o.seat%2)%Math.max(1,sites.length)];
  let destination=o.bomb.state==='planted'&&o.bomb.position?o.bomb.position:site?.center;
  if(!destination)return a;
  let near=dist(pos,destination);
  if(p.carrying_bomb&&near<site.radius-.5){a.use='plant';return a;}
  if(o.bomb.state==='planted'&&o.side==='defend'&&near<1.1){a.use='defuse';return a;}
  let key=destination.join(',');if(key!==goal||!route.length){goal=key;route=path(pos,destination);}
  while(route.length>1&&dist(pos,route[0])<1.4)route.shift();
  let next=route[0]||destination,dx=next[0]-p.x,dy=next[1]-p.y,yaw=Math.atan2(-dy,dx),error=angle(yaw-p.yaw);
  a.yaw_rate=Math.max(-1,Math.min(1,error/.15));a.move_forward=Math.abs(error)<.3?1:0;
  if(last&&dist(pos,last)<.02)stuck++;else stuck=0;last=pos;
  if(stuck>8)a.move_right=o.seat%2?1:-1;
  a.jump=stuck>10&&Math.floor(stuck/10)%2===1;
  if(p.ladder_id){a.ladder='attach';a.move_forward=next[2]>p.z?1:-1;}
  return a;
}
"""

FPS_CODE_CARD = """
You author agent.py for ONE independent seat in AGX 5v5 FPS. The harness evaluates it;
local checks establish an experiment, not a champion. Teammates run other people's files.

CONTRACT
init(context) clears match memory. context has contract_id=rsi-fps-5.5, seat, role,
seed and weights_dir. act(observation) returns a legal dict at 20 Hz (3 physics ticks).
Use standard library and `from strategy import STRATEGY`. No network or subprocess.
Do not hide exceptions behind an empty action. Keep work bounded below the decision budget.

OBSERVATIONS
phase, match_tick, round_tick, round_index, side, seat, team, buy_deadline_tick;
me includes alive, x/y/z, yaw/pitch, vx/vy, cash, armor, carrying_bomb, has_kit,
active_slot, ammo, blind_ticks, reload, switch_until, weapons[] and slots{}.
Weapons include id, ammo/reserve, recoil_yaw/recoil_pitch, shot_ready, reload_until,
aim_punch and reload_state. me.ammo belongs to the currently active weapon.
visible[] includes legal visible enemies x/y/z, seat, parts and possibly crouch.
No hidden enemy inventory, future observations or match seed may be assumed.
map is intermittent: cache it. nav.nodes use id and position[x,y,z]; edges are directed.
Reverse only walk/crouch/jump/source-adjacency edges as in the public reference.
Preserve drop/ladder direction and height. Sites are ordered centers plus radii, A then B.
bomb includes state and position; only the carrier can plant. A noncarrier's zero plants
is not proof of failure. Read receipts when diagnosing purchases or failed interactions.

SUPPORTED ACTIONS
weapon_slot (0 primary/1 secondary), buy string, fire/reload/jump bool,
yaw_rate/pitch_rate/move_forward/move_right finite [-1,1], use plant/defuse,
ladder attach. Preserve these verified action keys; extending the vocabulary requires
contract verification and matching harness support first.
Yaw forward=(cos(yaw),-sin(yaw)); target bearing=atan2(-dy,dx).
The published baseline compensates 2*held.recoil_yaw/pitch and uses yaw/.15,
pitch/.10 at 20Hz. These are reference control assumptions, not proof of hits.
Check readiness, equipped weapon, movement, recoil and alignment together.

RULES VERIFIED FROM THE CURRENT /api/v1/games/fps AND OWN LOGS
MR12, first13, halftime after12 resets inventory/cash800; 12:12 draw.
Freeze6s, live115s, post5s; buying through first25 live seconds in own buy area.
Plant3s inside legal radius; defuse10s (kit5s) within2.8m; pickup within2.3m with sight.
Read actual round timing/state rather than hardcoding a departure time by seat ID.
Prices and side restrictions are in the current source and published reference.

EXPERIMENT DISCIPLINE
Latest versioned match facts and current code come first. Verify a defect still exists.
Choose ONE combat, navigation, economy or objective hypothesis with an exact function,
measured evidence, expected audit change and a real-match falsifier.
No mandatory aggression, peek count, spawn delay, purchase ordering or chase policy.
Preserve successful objective execution and all-seat runtime compatibility.
Do not maximize fire=True counts, oscillation commands, synthetic fixture scores or K/D
by hiding. Aim-control and firing-discipline audits use calibrated adjacent observations;
they do not simulate the changed world or predict hits, survival or wins.
Opponent reference evidence contains only public events and ratings. Their private source,
observations, routes, aim settings and intentions remain UNKNOWN.

REFERENCE: published AGX rule-based baseline, for schema and control conventions only:
""" + _FPS_BASELINE + """

Follow the final task's output format exactly. Implementation requests specify either
complete existing replacement functions or a complete Python file and its JSON book.
""".strip()


def book_messages(best: dict, code: str, gate: str, reads: list[str], avoid: str | list[str]) -> list[dict]:
    """Ask for the book that pairs with a rewrite that already passed the gate."""
    task = (
        "Your agent.py passed the functional gate. It still needs independent evaluation before selection. "
        "Write the strategy book that belongs to this file. "
        f"Forbidden revision names (already used): {json.dumps(avoid)}. "
        "The name must differ from EVERY forbidden name. Pick a new short id that says what this file does. "
        "The thesis is two sentences: what the seat was doing wrong, and what this file does instead. "
        f"Only these fields are read by the code: {', '.join(reads) or 'none'}. "
        "Set those to values the code can actually use. Copy every other field from the current book "
        "unchanged, because the code ignores them. Return one JSON object and nothing else."
    )
    user = (
        "Current strategy book:\n"
        + json.dumps(best, ensure_ascii=False, indent=2)
        + "\n\nThe agent.py that passed:\n```python\n"
        + code
        + "\n```\n\nLocal gate report on that file:\n"
        + gate
        + "\n\n"
        + task
    )
    return [
        {"role": "system", "content": FPS_CARD},
        {"role": "user", "content": user},
    ]


def code_messages(best: dict, code: str, report: str, gate: str = "", attempt: int = 1,
                  mode: str = "repair", deliver: bool = False) -> list[dict]:
    if mode == "improve":
        task = (
            "The functional checks pass. Use FPS EXPERIMENT WORKFLOW v2: read the latest played "
            "version, own trace, current source and public opponent reference. Identify ONE defect "
            "still present in this exact source. State the function and current condition, two "
            "measured numbers, an independent hypothesis and a real-match falsifier. Extra fire "
            "commands alone cannot pass selection. Preserve objective execution and readiness. "
            "Do not repeat a past repair, force aggression or invent hidden opponent settings. "
            "Implement exactly one behavior change in the requested format."
        )
    elif gate:
        task = (
            f"This is repair attempt {attempt}. Your previous file was rejected by the local gate. "
            "Read the gate report line by line. Fix every FAIL, keep every PASS, and return the "
            "complete corrected agent.py. Do not return a fragment or a diff. "
            "The strategy book changes only if a failed check is caused by a book value."
        )
    else:
        task = (
            "Rewrite agent.py so the seat plays the round instead of standing still. "
            "Use the nav graph for movement, stop before firing, reload an empty rifle, "
            "and finish the objective. Keep the strategy book unless the evidence identifies "
            "a book value as the cause. The thesis states the one behaviour you changed and why."
        )
    if mode == 'improve' and 'COMBAT GOAL REQUESTED BY USER:' in report:
        task = (
            'Diagnose the requested combat goal using the latest recorded combat facts and the CURRENT '
            'baseline response audit. Compare those two evidence rows, clearly separating played results '
            'from replay responses. Historical rows are context; do not revive a historical defect already '
            'fixed in the supplied source. Quote the current function and its actual condition before '
            'proposing ONE combat change. No recorded shots does not establish no weapon purchase: '
            'the public weapons column lists fired weapons only. If the latest log records rifles, do '
            'not diagnose missing rifle purchases without contradictory verified inventory evidence. '
            'Explain which measured engagement response the change should improve. Preserve all '
            'passing navigation, objective, reload, alignment and recoil checks. No indiscriminate '
            'firing or hiding to inflate K/D. Offline replay cannot demonstrate a kill-rate gain.'
        )
    user = (
        "Current strategy book:\n"
        + json.dumps(best, ensure_ascii=False, indent=2)
        + "\n\nCurrent agent.py:\n```python\n"
        + code
        + "\n```\n\nMatch evidence:\n"
        + report
    )
    if gate:
        user += "\n\nLocal gate report on your previous file:\n" + gate
    if deliver:
        task += (
            " The explicit diagnosis in the evidence is the implementation contract. Now IMPLEMENT the one behaviour change you identified "
            "and return exactly two fenced blocks and no other prose: a ```python block with the complete "
            "corrected agent.py (it MUST differ from the current file — do not return it unchanged), then a "
            "```json block with the strategy book."
        )
    user += "\n\n" + task + " The visible reply is the two fenced blocks and nothing else. The functional gate tests all ten seat IDs as carriers. Keep each carrier inside the actual site radius. No specific peek pattern or seat-dependent delay is required."
    return [
        {"role": "system", "content": FPS_CODE_CARD},
        {"role": "user", "content": user},
    ]


def repair_messages(book: dict, code: str, gate: str, diagnosis: str, evidence: str = '') -> list[dict]:
    """A repair contract driven by current failures, not an old strategic thesis."""
    failures = "\n".join(line for line in gate.splitlines() if line.startswith("FAIL"))
    purpose = ("Repair the CURRENT failed checks below." if failures else
               "All fixture checks pass. Implement ONE behavior experiment justified by the current diagnosis "
               "from real match evidence, while preserving all fixture checks. A fixture pass is not a match win.")
    return [
        {"role": "system", "content": purpose + " " +
            "You repair an existing Python FPS agent. Output ONLY one Python fenced block "
            "containing 1 to 3 COMPLETE replacement functions whose names already exist in the "
            "file. No JSON edits, diffs, imports, global assignments, book changes or full-file "
            "rewrite. The harness replaces those functions by AST positions, compiles, and "
            "runs every check. Make the smallest semantic repair to CURRENT failures. "
            "First fix runtime exceptions, then remaining failed behavior. Inspect the helper, "
            "all caller initialization paths and round reset together. "
            "Measured helper calls and first_fight snapshots are ground truth. Do not claim a helper "
            "was bypassed when its call count is nonzero. Translation blocked is a fixture label, "
            "not a field available to the policy; detect lack of progress from actual observations. A dict setdefault only "
            "creates the whole dict when absent; it does not fill missing keys in an existing "
            "dict. Ensure every state key is initialized before its first read. An except that "
            "returns an empty action hides errors and is not a repair. Do not change checks that "
            "already pass. Use fixture measurements as requirements: corner_peek needs at least "
            "8 stationary strafe ticks and 2 direction reversals, then movement must continue. "
            "All ten carrier seats must plant. Preserve departure holds, aiming, reload and "
            "defuse behavior. Limit changes to the current failure, or ONE explicit behavior "
            "hypothesis when all checks already pass; preserve unrelated code. After repairing a helper, verify its callers "
            "construct the same state schema. Return actual executable function bodies."},
        {"role": "user", "content":
            "Strategy (unchanged): " + json.dumps(book) + "\nCurrent diagnosis: " + diagnosis +
            ('\nMeasured response context (not proof of counterfactual outcomes):\n'+evidence if evidence else '') +
            "\nCurrent failures (these take priority over earlier hypotheses):\n" + failures +
            "\nComplete gate report; preserve all PASS checks:\n" + gate +
            "\nCurrent best candidate, including callers and reset paths:\n```python\n" + code + "\n```"},
    ]


def behavior_messages(book: dict, code: str, evidence: str, gate: str, diagnosis: str) -> list[dict]:
    """Passing candidates need a match experiment, not another fixture repair."""
    return [
        {'role':'system','content':FPS_CODE_CARD + '\nAll local fixture checks already PASS. '
         'Implement ONE behavior hypothesis justified by the real evidence and diagnosis. '
         'Output ONLY one Python fenced block with 1 to 3 COMPLETE replacement functions '
         'whose names already exist. No full-file rewrite, imports, globals, JSON, or book changes. '
         'Function bodies MUST change semantically. Preserve all passing checks and unrelated behavior. '
         'A paired replay rejects identical action sequences and requires a gain in captured blocked-motion, '
         'near-objective, or calibrated aim-control/fire-discipline quality without protected-response regressions. More fire requests alone cannot pass. '
         'Diagnose the supplied examples without assuming their cause. For an explicit combat '
         'goal prioritize the current combat audit and engagement examples. Repeated forward input into an unchanged '
         'position is not progress; diagnose elevation, route validity, and recovery state. '
         'Changing fallback defaults already explicitly supplied in the book is behaviorally inert. '
         'Pure formatting, renamed variables and neutral action keys are not improvements. '
         'For a user-requested combat goal, navigation-only gains do not satisfy the request. '
         'Preserve alignment, recoil, reload and objective behavior; do not emit indiscriminate fire or hide to inflate K/D. '
         'Check played-version provenance: the recorded match may use this baseline or an older version. '
         'Check whether its defect is already fixed in this candidate. '
         'If already fixed, select a different evidence-backed hypothesis; do not repeat the old patch. '
         'Local simulation and offline replay cannot prove a win-rate improvement.'},
        {'role':'user','content':'Current book: '+json.dumps(book)+'\nReal match and training evidence:\n'+evidence+
         '\nDiagnosis to implement:\n'+diagnosis+'\nPassing constraints:\n'+gate+
         '\nCurrent candidate:\n```python\n'+code+'\n```'}]
