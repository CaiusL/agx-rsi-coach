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
You are writing the next bomb-defusal strategy for ONE seat in a 5v5 of other people's agents.
You do not control teammates. MR12, first to 13, 12-12 is a draw. Match score is the team result.
Change only this JSON object:
{
  "name": "short id",
  "thesis": "why this counters the current best",
  "default_site": "A"|"B",
  "expect_site": "A"|"B",
  "buy_style": "armor_pistol"|"force_rifle"|"smg_force"|"full_rifle",
  "utility": "none"|"flashbang"|"smokegrenade"|"molotov",
  "post_plant": "hold"|"molotov"|"hunt",
  "ct_setup": "retake"|"stack_expect"|"mid",
  "execute_live_seconds": 12 to 45
}
What these fields do in the match:
- Attack walks to default_site. It plants only if this seat is carrying, after execute_live_seconds, and only within 2m.
- The seat closes on any visible enemy and fires only after it has stopped. That fight comes before planting or defusing.
- retake and stack_expect push expect_site. mid pushes mid. A recently seen enemy is chased.
- A moving rifle does not hit, so the seat stops once the duel is inside 14m.
- Freeze is 6s, live is 115s, post is 5s. The first 25s of live are still the buy window, and the seat does not walk during buy.
The score is the team result. Seat score 1 is a win. Team rounds break a tie. A 13-0 says the other side won no round. It does not say which site anyone played.
The evidence carries the public recording when one exists: final round_wins, our team, the count of
each round_end reason (elimination, timeout, bomb_explode, defuse), this seat's kills and deaths,
its shots fired and hits landed with accuracy, its weapons, reloads and faults, how many times it
planted and at which site, and our attack rounds split from our defend rounds with their reasons.
A team win with zero kills, or with shots fired and zero hits, means the seat contributed nothing
and rode its teammates. Survival alone is not a contribution.
The local gate is a fixture, NOT the engine. A candidate can pass all 13 checks and still land zero
hits in the real match. When the recording shows zero accuracy, the aiming code is wrong regardless
of what the gate says: fix the code, do not tune the book.
Many timeout rounds on attack mean our team never planted. If this seat planted zero times while
attacking, it is not executing the objective. Many bomb_explode rounds against us on defend mean
the plant was not defused. A player name is still not a setup.
Experiment, in order. Copy every field that is not the current experiment:
1. Defense setup. The opposite of mid is stack_expect, and the opposite of retake is mid. Measure both before moving on.
2. Site. A and B. Move default_site and expect_site together.
3. After the plant: hunt against hold. molotov's opposite is hunt.
4. Only after those pairs have scores, and only if the better one did not win, plant 4 seconds earlier, down to 12.
Buy and utility stay copied. Keeping the whole book is only allowed after a win. After a loss or a
rank drop, be more aggressive than the book that lost: advance to the next unmeasured lever, or change
the one lever the recording identifies. The measured lever scores are historical bests, not this match.
""".strip()


def plan_messages(game: str, best: dict, report: str, mode: str, exploration=None) -> list[dict]:
    card = RACING_CARD if game == "racing" else FPS_CARD
    if game == "fps" and mode != "init":
        task = (
            "This is the shooter reflection. Read the Outcome line first. "
            "After a loss or a rank drop you MUST change the book and be more aggressive than the "
            "book that lost; returning 'keep whole book' after a loss is forbidden. "
            "Only a win lets you keep the whole book. "
            "The evidence starts with every finished match. "
            "Read all of those rows before the latest one. A team win with many deaths, a pistol, "
            "or zero plants is a seat loss. "
            "The 'Measured shooter levers' scores are each value's BEST historical result, not this "
            "match: a lever that won an old match does not excuse a loss now. "
            "Find the column that stayed bad while the book name changed, and the column that "
            "changed between a better row and a worse row. "
            "Change the one field the file actually reads that this comparison identifies. "
            "Do not step site, utility, or plant time off a team score alone. "
            "If the opposite of the best measured defense, site, or post-plant value has no score, that opposite is the whole change and every other field is copied. "
            "If both sides have a score and the better one won AND this match was won, keep the whole book. "
            "If both sides have a score and the better one lost, lock it and move to the next lever. "
            "If this match was lost, advance to the next unmeasured lever or change the one the recording identifies; do not keep the whole book. "
            "The plant time moves only after those pairs, by 4 seconds toward 12. "
            "The thesis states this match's outcome, the scores already measured, and the one change."
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
You own agent.py for ONE seat in a 5v5 bomb-defusal match of other people's agents.
You do not control teammates. Rewrite the whole file. The harness runs your file in a
local fixture and only uploads it if every check passes, so a partial or broken file
is rejected and you get the failure report back.

ENTRY POINTS
  init(context) -> None. context = {"contract_id": "rsi-fps-5.5", "seat", "role", "seed", "weights_dir"}.
      Raise if contract_id is wrong. Clear any per-match memory here.
  act(observation) -> dict. Called at 20 Hz. Must return in well under 50 ms.
      Return {} when dead or when phase is "post"/"finished". Never raise: wrap the body
      and return {} on error, but do not use that to hide a broken plan.
Standard library only. No network, no files, no subprocess. Read the tunable book with
`from strategy import STRATEGY`; strategy.py is generated beside your file.

OBSERVATION (verified against the official public baseline)
  phase: "buy" | "live" | "post" | "finished"
  round_index, round_tick, match_tick, buy_deadline_tick, seat, side ("attack"|"defend")
  map.nav.nodes[] = {"id": int, "position": [x, y, z]}
  map.nav.edges[] = {"from": id, "to": id, "kind": "walk"|"crouch"|"jump"|"source-adjacency"}
      Edges are directed. Add the reverse edge yourself for walk/crouch/jump/source-adjacency.
  map.sites[] = {"center": [x, y, z], "radius": float}
      An ordered list. No name or id field is verified: index 0 is treated as A, index 1 as B.
  me = {alive, x, y, z, yaw, pitch, vx, vy, cash, armor, has_kit, carrying_bomb,
        ladder_id, ammo, weapons[], slots{}}
  me.slots.primary is truthy when a primary is held, not an index.
  me.weapons[slot] = {"id": "ak47"|..., "recoil_yaw", "recoil_pitch"} or None. Slot 0 primary, 1 secondary, 2 knife.
  me.ammo is the held weapon's magazine.
  visible[] = enemies in line of sight, with x, y, z. Nothing else about them is verified.
  bomb = {"state": "carried"|"dropped"|"planted"|"defusing"|"defused", "position": [x,y,z]}
Sites are lists under "center", NOT dicts with x/y. Walk the nav graph; do not expect
named landmarks. The map is only present on some ticks, so cache it.

ACTION VOCABULARY (every key the official baseline uses; the gate rejects anything else)
  weapon_slot int      buy str          fire bool        reload bool
  yaw_rate float -1..1                 pitch_rate float -1..1
  move_forward float -1..1             move_right float -1..1
  jump bool            use "plant"|"defuse"              ladder "attach"
Keys such as sprint, walk, crouch, throw, throw_state, throw_mode are NOT verified and
are rejected. Yaw convention: forward is (cos yaw, -sin yaw), so the bearing to a target
is atan2(-(dy), dx). Compensate recoil: yaw -= 2*recoil_yaw, pitch -= 2*recoil_pitch.
Aim at the target's centre mass, roughly z + 1.64 for both seats.

RULES
  MR12, first to 13, 12-12 is a draw, 24 rounds max. Team score is the result.
  Freeze 6 s, live 115 s, post 5 s. Buying is allowed through the first 25 s of live.
  Plant 3 s inside the site radius. Defuse 10 s (5 s with a kit) below 2.8 m.
  Bomb pickup is automatic below 2.3 m with line of sight. A moving rifle does not hit.
  Halftime clears inventory and resets cash to 800. Loss bonus tiers 1400/1900/2400/2900/3400.
  Prices: armor 650, helmet 1000, defusekit 400, flashbang 200, smokegrenade 300,
  molotov 400, incgrenade 500, ak47 2700, m4a1 2900, galilar 1800, mac10 1050, mp9 1250.
  Galilar/mac10/tec9/molotov/ak47 are attack-side; m4a1/mp9/fiveseven/incgrenade/defusekit are defend-side.
  Ladders: when me.ladder_id is set, send ladder "attach" and move_forward toward the next node's z.

OBJECTIVE
  The seat gets stuck at corners and never plays the round. The current jiggle peek sets
  move_forward to 0 at every sharp turn and repeats. That is the failure.
  Peek once on the way to the site: at least 8 ticks of move_right with move_forward 0,
  and reverse the strafe direction twice. Then clear the peek and walk. Do not peek again
  this round. A reached waypoint is left behind. The route continues to the objective.
  An attacker who is not carrying the bomb hunts kills until the bomb is planted:
  fight a visible enemy, chase the last one, and otherwise push the site without peeking.
  The carrier still plants. Attack: go to the bomb site, clear any enemy that is there, plant, then hold the
  planted bomb. Stay on the bomb and fight anyone who comes. Do not wander off after the plant.
  Defend, before the plant: hold the approach to the expected site, the path an attacker
  would take. Do not chase across the map.
  Defend, after the plant: rotate to the planted bomb immediately. Kill an attacker who
  is there. When no enemy is visible and you are within 2.8 m, send use "defuse" every
  tick until the bomb is defused. A visible enemy stops the defuse.
  A visible enemy is fought, then the seat returns to that objective.
  Read the current evidence for kills, deaths, shots and plants; do not reuse obsolete match
  numbers as the latest result. full_rifle asks for an ak47 at 2700 or an
  m4a1 at 2900 and then skips both when the cash is short, so a seat that dies stays on the
  starting pistol and loses the next duel. Buy a rifle the cash can afford, galilar or mac10
  on attack and mp9 on defense, before armor, and fire that rifle. Then walk to the site and plant.

TEAM SPACING (do not body-block teammates)
  You control one seat; teammates may use different files. Your seat ID changes per match.
  Avoid stacking on a teammate's destination or doorway. The fixture tests every seat ID.
  Implement the seat-specific spacing contract in movement code:
  - Add a per-seat offset around the destination so seats fan out instead of stacking on the
    exact center, e.g. goal = center plus a small lateral offset keyed by seat. The bomb carrier
    must still land inside the site radius to plant, so keep its offset small: the local gate
    runs every seat as the carrier and requires it to reach and plant at default_site.
  - Stagger spawn departure by seat so teammates do not enter the same narrow route together,
    e.g. hold movement until round_tick >= 360 + (seat % 5) * 90.
  - Vary the stuck-recovery sidestep direction by seat so blocked teammates do not all jam
    the same way, e.g. turn = 1 if (seat + escapes) % 2 else -1.
  Non-carrier seats may take a wider offset or a different site; the carrier still plants at
  the site the book names. Do not strand the carrier away from default_site.

WHAT THE FIXTURE MEASURES
  buy: spends the pistol-round cash. attack_moves: travels more than 20 m with no enemy
  visible. attack_reaches_site: enters the site radius. attack_plants: plants the bomb.
  defend_defuses: reaches a planted bomb and defuses it. duel_fires: stops and hits a
  visible enemy. duel_reloads: reloads an empty rifle. action_keys: vocabulary only.
A seat that stands still loses every round. Movement is the first thing to get right.

REFERENCE: the official public rule-based baseline, in JavaScript. It is known to
navigate, plant, defuse and shoot. Read it for the real schema and structure. Your
answer must be Python, not a copy of this JavaScript.
""" + _FPS_BASELINE + """

OUTPUT
Return exactly two fenced blocks and no prose outside them:
```python
<the complete new agent.py>
```
```json
<the strategy book, same fields as the current one>
```
""".strip()


def book_messages(best: dict, code: str, gate: str, reads: list[str], avoid: str | list[str]) -> list[dict]:
    """Ask for the book that pairs with a rewrite that already passed the gate."""
    task = (
        "Your agent.py passed every local gate check. It is going live. "
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
            f"This is improvement attempt {attempt}. Your file already PASSES every local gate check, "
            "but the real match went badly. The fixture is not the engine: passing it only means the "
            "seat buys, navigates, plants, defuses, shoots and reloads in a simplified map. "
            "The evidence starts with every finished match. Compare at least two rows: deaths, shots, "
            "weapon, plants and round reasons. A column that stays bad while the book name changes is "
            "in this file, not in the book. The wish-list prices are in the file. Change the behaviour "
            "the comparison identifies. A cause that cites only the latest match is rejected. "
            "You must keep every gate check passing: do not regress movement, planting, defusing, "
            "or burst discipline. "
            "Hard constraint: peek once per round, then never again. A file that starts a new peek "
            "for every corner key is not acceptable, even if the fixture passes. After that one peek, "
            "move_forward stays positive unless the seat is planting, defusing, or fighting. "
            "After the plant, stay on the bomb. On defense, go to the planted bomb and defuse only "
            "when no enemy is visible. "
            "Compare at least two matches inside your thinking: the column that stayed bad, "
            "the function in this file that produced it, and the one behaviour you will change. "
            "Then return the complete corrected agent.py, not a diff. "
            "The thesis states the one behaviour you changed and the recorded number that justifies it."
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
    user += "\n\n" + task + " The visible reply is the two fenced blocks and nothing else. The gate now tests all ten seat IDs as carriers, and requires the declared departure schedule on every seat. Waiting intentionally must never activate stuck recovery. Keep each carrier inside the actual site radius, regardless of seat ID."
    return [
        {"role": "system", "content": FPS_CODE_CARD},
        {"role": "user", "content": user},
    ]


def repair_messages(book: dict, code: str, gate: str, diagnosis: str) -> list[dict]:
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
            "all caller initialization paths and round reset together. A dict setdefault only "
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
         'The recorded match used an older version: check whether its defect is already fixed in this candidate. '
         'If already fixed, select a different evidence-backed hypothesis; do not repeat the old patch. '
         'Local simulation and offline replay cannot prove a win-rate improvement.'},
        {'role':'user','content':'Current book: '+json.dumps(book)+'\nReal match and training evidence:\n'+evidence+
         '\nDiagnosis to implement:\n'+diagnosis+'\nPassing constraints:\n'+gate+
         '\nCurrent candidate:\n```python\n'+code+'\n```'}]
