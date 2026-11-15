"""METHOD
Category: rules; search
Approach: One seat. The bomb carrier plants. An attacker without the bomb
hunts kills until it is planted. Defenders hold, then retake a planted bomb.
Data & training: Public match score. The coach rewrites strategy.py.
Direct author: Codex, round 2026-11-15 14:00 Asia/Shanghai.
Changes (movement and combat repair):
  - Timed persistent recovery, blocked-waypoint replanning and directed navigation.
  - Visible-part stance-aware aim and actual-shot weapon-specific burst control.
Earlier revisions:
  - Staggered departure: hold movement until round_tick >= 360 + (seat%5)*90.
  - Stuck-recovery is gated so it never fires during the intentional hold.
  - Per-seat lateral offset around the site center so seats fan out.
  - Carrier keeps a small offset so it still lands inside the site radius.
  - One-time jiggle peek (10 ticks, 2 direction reversals) then walk.
  - Fix engagement: fire when aligned, compensate recoil, reload when empty.
  - Fix aim: lower burst size, stronger recoil compensation, tighter aim gates.
  - Fix runtime: clip the FULL yaw/pitch rate (aim error + recoil compensation)
    so the sum can never leave [-1,1]. Previously the recoil term was added
    after the aim-error clip, letting yaw_rate exceed the legal bound and
    trip the gate's finite-bounds validation.
END
"""

import math

from strategy import STRATEGY

MEM = {}

PRICES = {
    "armor": 650,
    "helmet": 1000,
    "defusekit": 400,
    "flashbang": 200,
    "smokegrenade": 300,
    "molotov": 400,
    "incgrenade": 500,
    "ak47": 2700,
    "m4a1": 2900,
    "galilar": 1800,
    "mac10": 1050,
    "mp9": 1250,
    "tec9": 500,
    "fiveseven": 500,
}

WISH = {
    "armor_pistol": ["armor"],
    "force_rifle": ["galilar", "mp9", "armor", "flashbang"],
    "smg_force": ["mac10", "mp9", "armor"],
    "full_rifle": ["ak47", "m4a1", "armor", "defusekit", "smokegrenade", "flashbang", "molotov", "incgrenade"],
}

SIDE_OK = {
    "galilar": "attack",
    "mac10": "attack",
    "tec9": "attack",
    "molotov": "attack",
    "ak47": "attack",
    "mp9": "defend",
    "fiveseven": "defend",
    "incgrenade": "defend",
    "m4a1": "defend",
    "defusekit": "defend",
}

# Per-seat lateral offset (meters) around the site center.
SEAT_OFFSETS = [
    (0.0, 0.0),
    (1.2, 0.8),
    (-1.2, 0.8),
    (0.8, -1.2),
    (-0.8, -1.2),
    (1.5, -0.5),
    (-1.5, 0.5),
    (0.5, 1.5),
    (-0.5, -1.5),
    (1.0, 1.0),
]

PEEK = {"out": 4, "back": 4, "max": 1, "turn": 0.6, "near": 6.0}
AIM = {"burst_max": 4, "burst_pause": 8, "yaw_gate": 0.12, "pitch_gate": 0.15, "stop_dist": 15.0}


def clip(value, low, high):
    return max(low, min(high, value))


def wrap(angle):
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def dist3(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)


def dist2(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)


def build_nav(map_obj):
    nodes = list((map_obj or {}).get("nav", {}).get("nodes", []))
    by_id = {n["id"]: n for n in nodes}
    adj = {}
    for edge in (map_obj or {}).get("nav", {}).get("edges", []):
        if edge.get("kind") == "ladder":
            continue
        if edge.get("from") in by_id and edge.get("to") in by_id:
            adj.setdefault(edge["from"], []).append(edge)
            # Source adjacency is directed and explicitly unvalidated on Mirage.
            if edge.get("kind") == "walk":
                adj.setdefault(edge["to"], []).append({"from": edge["to"], "to": edge["from"], "kind": "walk"})
    return nodes, by_id, adj


def find_nearest_node(pos, nodes):
    if not nodes:
        return None
    best = None
    best_d = float('inf')
    for n in nodes:
        p = n.get("position", [0, 0, 0])
        d = dist3(pos, p)
        if d < best_d:
            best_d = d
            best = n
    return best


def pathfind(start_pos, end_pos, nodes, by_id, adj):
    import heapq
    if not nodes:
        return [list(end_pos)]
    tick = MEM.get("tick", 0)
    blocked = MEM.setdefault("blocked_waypoints", {})
    blocked = {key: until for key, until in blocked.items() if until > tick}
    MEM["blocked_waypoints"] = blocked
    available = [n for n in nodes if tuple(n["position"]) not in blocked]
    start_node = find_nearest_node(start_pos, available)
    end_node = find_nearest_node(end_pos, available)
    if not start_node or not end_node:
        return []
    first, last = start_node["id"], end_node["id"]
    queue = [(0., 0, first)]; serial = 0
    costs = {first: 0.}; parent = {}
    while queue:
        cost, _, here = heapq.heappop(queue)
        if cost != costs[here]:
            continue
        if here == last:
            break
        for edge in adj.get(here, []):
            there = edge["to"]
            if there not in by_id or tuple(by_id[there]["position"]) in blocked:
                continue
            new = cost + dist3(by_id[here]["position"], by_id[there]["position"])
            if new < costs.get(there, float("inf")):
                costs[there] = new; parent[there] = here; serial += 1
                heapq.heappush(queue, (new, serial, there))
    if last not in costs:
        return []
    ids = [last]
    while ids[-1] != first:
        ids.append(parent[ids[-1]])
    return [list(by_id[i]["position"]) for i in reversed(ids)] + [list(end_pos)]


def reset_round(obs):
    index = obs.get("round_index")
    if MEM.get("round") == index:
        return
    MEM["round"] = index
    MEM["attempted"] = set()
    MEM["stuck"] = 0
    MEM["last_pos"] = None
    MEM["route"] = []
    MEM["goal"] = None
    MEM["last_enemy"] = None
    MEM["last_enemy_tick"] = 0
    MEM["burst"] = 0
    MEM["burst_pause"] = 0
    MEM["peek_done"] = False
    MEM["peek"] = None
    MEM["peeked"] = set()
    MEM["departed"] = False
    MEM["progress_anchor"] = None
    MEM["progress_tick"] = 0
    MEM["escape_until"] = 0
    MEM["escapes"] = 0
    MEM["blocked_waypoints"] = {}
    MEM["combat_ammo"] = None
    MEM["actual_burst"] = 0
    MEM["burst_until"] = 0
    MEM["entry_points"] = {}
    MEM["previous_hp"] = None
    MEM["alert_until"] = 0
    MEM["sound_check_after"] = 0


def owned(me):
    found = set()
    if (me.get("armor") or 0) > 0:
        found.add("armor")
    if me.get("helmet"):
        found.add("helmet")
    if me.get("has_kit"):
        found.add("defusekit")
    for weapon in me.get("weapons") or []:
        if isinstance(weapon, dict) and weapon.get("id"):
            found.add(weapon["id"])
    return found


def wish_list(side):
    # Full buys must not remain on an SMG merely because the old book forced it.
    primary = ["ak47", "galilar"] if side == "attack" else ["m4a1", "mp9"]
    if STRATEGY.get("buy_style") == "armor_pistol":
        return ["armor"]
    return primary + ["armor", "helmet"] + (["defusekit"] if side == "defend" else [])


def _in_buy(obs):
    if obs.get("phase") == "buy":
        return True
    if obs.get("phase") != "live":
        return False
    deadline = obs.get("buy_deadline_tick")
    tick = obs.get("match_tick")
    return deadline is not None and tick is not None and tick <= deadline


def buy(obs, me, side):
    if not _in_buy(obs):
        return None
    have = owned(me)
    cash = me.get("cash") or 0
    attempted = MEM.setdefault("attempted", set())
    primary = "ak47" if side == "attack" else "m4a1"
    fallback = "galilar" if side == "attack" else "mp9"
    guns = have & {"ak47", "m4a1", "galilar", "mac10", "mp9"}
    choices = []
    if not guns and STRATEGY.get("buy_style") != "armor_pistol":
        if cash >= PRICES[primary]:
            choices.append(primary)
        elif cash >= PRICES[fallback] + (0 if "armor" in have else PRICES["armor"]):
            choices.append(fallback)
    # Protection and a CT kit precede unused utility. Preserve an owned primary.
    if (me.get("armor") or 0) < 50:
        choices.append("armor")
    if guns:
        if not me.get("helmet"):
            choices.append("helmet")
        if side == "defend" and not me.get("has_kit"):
            choices.append("defusekit")
    for item in choices:
        if item in attempted or cash < PRICES[item]:
            continue
        attempted.add(item)
        return item
    return None


def get_site_center(obs, side, seat):
    map_obj = MEM.get("map")
    if not map_obj:
        return None
    sites = map_obj.get("sites", [])
    if not sites:
        return None
    if side == "attack":
        letter = STRATEGY.get("default_site", "A")
    else:
        letter = STRATEGY.get("expect_site", STRATEGY.get("default_site", "A"))
    idx = 0 if letter == "A" else 1
    if idx >= len(sites):
        idx = seat % len(sites)
    site = sites[idx]
    center = site.get("center")
    if center and len(center) >= 2:
        z = center[2] if len(center) > 2 else 0
        return (float(center[0]), float(center[1]), float(z))
    return None


def get_site_radius(obs, side, seat):
    map_obj = MEM.get("map")
    if not map_obj:
        return 4.0
    sites = map_obj.get("sites", [])
    if not sites:
        return 4.0
    if side == "attack":
        letter = STRATEGY.get("default_site", "A")
    else:
        letter = STRATEGY.get("expect_site", STRATEGY.get("default_site", "A"))
    idx = 0 if letter == "A" else 1
    if idx >= len(sites):
        idx = seat % len(sites)
    return sites[idx].get("radius", 4.0)


def _has_primary(me):
    weapons = me.get("weapons") or []
    return bool(weapons and weapons[0])


def nearest_enemy(me, visible):
    best = None
    best_dist = 1e9
    for enemy in visible:
        if "x" not in enemy or "y" not in enemy:
            continue
        d = math.hypot(enemy["x"] - me["x"], enemy["y"] - me["y"])
        if d < best_dist:
            best, best_dist = enemy, d
    return best


def fight(me, enemy):
    """Aim at a visible, stance-adjusted part; stay settled through cooldowns."""
    dx, dy = enemy["x"] - me["x"], enemy["y"] - me["y"]
    distance = max(.1, math.hypot(dx, dy))
    heights = {"head": 1.64, "chest": 1.18, "abdomen": .88}
    parts = enemy.get("parts")
    part = next((p for p in ("chest", "head", "abdomen") if p in (parts or [])), "chest")
    height = heights[part] * (.6 if enemy.get("crouch") else 1.)
    eye = .95 if me.get("crouch") else 1.62
    dz = enemy.get("z", 0) + height - me.get("z", 0) - eye
    slot = 0 if _has_primary(me) else 1
    weapons = me.get("weapons") or []
    gun = (weapons[slot] or {}) if slot < len(weapons) else {}
    punch = gun.get("aim_punch") or {}
    yaw_error = wrap(math.atan2(-dy, dx) - me.get("yaw", 0) - 2 * punch.get("yaw", 0))
    pitch_error = math.atan2(dz, distance) - me.get("pitch", 0) - 2 * punch.get("pitch", 0)
    radius = .10 if part == "head" else .23
    tolerance = math.atan2(radius, distance)
    aligned = abs(yaw_error) <= tolerance and abs(pitch_error) <= tolerance
    tick = MEM.get("tick", 0)
    ammo = gun.get("ammo", me.get("ammo", 0)) or 0
    identity = (gun.get("item_id", gun.get("id")), enemy.get("seat"))
    prior = MEM.get("combat_ammo")
    if prior and prior[0] == identity and ammo < prior[1]:
        MEM["actual_burst"] = MEM.get("actual_burst", 0) + prior[1] - ammo
    elif not prior or prior[0] != identity or ammo > prior[1]:
        MEM["actual_burst"] = 0
        MEM["burst_until"] = 0
    MEM["combat_ammo"] = (identity, ammo)
    semi = gun.get("id") in ("pistol", "usp", "tec9", "fiveseven")
    if not semi and MEM.get("actual_burst", 0) >= 4:
        MEM["burst_until"] = tick + 9
        MEM["actual_burst"] = 0
    conflict = bool(me.get("reload") or gun.get("reload_state") or
                    (gun.get("reload_until") or 0) > tick or
                    (me.get("switch_until") or 0) > tick or
                    me.get("active_slot", slot) != slot)
    trigger = not semi or not (gun.get("trigger_state") or {}).get("pressed")
    ready = not conflict and (gun.get("shot_ready") or 0) <= tick
    settled = math.hypot(me.get("vx", 0), me.get("vy", 0)) < .5
    fire = bool(aligned and settled and ready and trigger and ammo > 0 and
                tick >= MEM.get("burst_until", 0) and not me.get("blind_ticks") and
                (parts is None or any(p in heights for p in parts)))
    return {"yaw_rate": clip(yaw_error / .15, -1, 1),
            "pitch_rate": clip(pitch_error / .1, -1, 1),
            "move_forward": 0., "move_right": 0., "fire": fire,
            "weapon_slot": slot,
            "reload": bool(ammo == 0 and gun.get("reserve", 30) > 0 and not conflict)}


def corner_ahead(pos, route):
    if len(route) < 2:
        return False
    a, b, c = pos, route[0], route[1]
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 0.5 or n2 < 0.5:
        return False
    cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return cosang < math.cos(PEEK["turn"])


def jiggle_peek(me, route):
    """A bounded peek before continuing the route."""
    if MEM.get("peek") is None:
        return None
    peek = MEM["peek"]
    if "prev_dir" not in peek:
        peek["prev_dir"] = None
    if "reversals" not in peek:
        peek["reversals"] = 0
    if "tick" not in peek:
        peek["tick"] = 0

    cycle = 16  # 8 out, 8 back -> 1 reversal, 16 strafe ticks
    if peek["tick"] >= cycle:
        MEM["peek_done"] = True
        MEM["peek"] = None
        return None

    phase = peek["tick"] % 8
    if phase < 4:
        move_forward, move_right = 0.0, peek["dir"]
    else:
        move_forward, move_right = 0.0, -peek["dir"]

    peek["tick"] += 1

    target = route[1] if len(route) > 1 else route[0]
    dx, dy = target[0] - me["x"], target[1] - me["y"]
    want = math.atan2(-dy, dx)
    error = wrap(want - (me.get("yaw") or 0))

    dir_now = peek["dir"] if phase < 4 else (-peek["dir"])
    if peek["prev_dir"] is not None and dir_now != peek["prev_dir"]:
        peek["reversals"] += 1
    peek["prev_dir"] = dir_now

    return {
        "yaw_rate": clip(error / 0.25, -1, 1),
        "move_forward": move_forward,
        "move_right": move_right,
        "weapon_slot": 0 if _has_primary(me) else 1,
    }


def steer_to(me, point, look=None, speed=.85):
    dx, dy = point[0] - me["x"], point[1] - me["y"]
    distance = math.hypot(dx, dy)
    move_yaw = math.atan2(-dy, dx)
    if look is None:
        error = wrap(move_yaw - me.get("yaw", 0))
        return {"yaw_rate":clip(error/.15,-1,1), "move_forward":speed*math.cos(error), "move_right":-speed*math.sin(error)}, distance
    target_yaw = math.atan2(-(look[1] - me["y"]), look[0] - me["x"])
    error = wrap(target_yaw - me.get("yaw", 0))
    rate = clip(error / .15, -1, 1)
    # Translation follows the route while the camera can watch a different entry.
    next_yaw = me.get("yaw", 0) + rate * .15
    relative = wrap(move_yaw - next_yaw)
    amount = speed
    return {"yaw_rate": rate, "move_forward": amount * math.cos(relative),
            "move_right": -amount * math.sin(relative)}, distance


def escape_action(me):
    error = wrap(MEM["escape_yaw"] - me.get("yaw", 0))
    return {"yaw_rate": clip(error / .15, -1, 1),
            "move_forward": .85 * math.cos(error),
            "move_right": -.85 * math.sin(error),
            "jump": bool(me.get("grounded") and MEM.get("tick", 0) % 24 == 0)}


def recover_navigation(me, pos, action):
    """Detect a blocked waypoint after ~30 ticks of commanded motion with <0.25 m net
    progress, then commit to a perpendicular detour for ~50 ticks before replanning
    toward the same objective.

    The previous 60-tick gate waited too long: 1812 stationary commanded pairs and
    2 blocked episodes in the latest match indicate the seat wasted live-window time
    before triggering recovery. A shorter, waypoint-aware gate detects corner
    blockage earlier, preserving the 115 s live budget for replanning and combat.
    """
    tick = MEM.get("tick", 0)
    moving = math.hypot(action.get("move_forward", 0), action.get("move_right", 0)) > .2
    if not moving or action.get("use") in ("plant", "defuse"):
        MEM["progress_anchor"] = pos
        MEM["progress_tick"] = tick
        return

    anchor = MEM.get("progress_anchor")
    if anchor is None or dist3(pos, anchor) > .25:
        MEM["progress_anchor"], MEM["progress_tick"] = pos, tick
        return

    if tick - MEM.get("progress_tick", tick) < 30:
        return

    route = MEM.get("route") or []
    if route:
        MEM.setdefault("blocked_waypoints", {})[tuple(route[0])] = tick + 600
    MEM["route"] = []
    MEM["goal"] = None
    MEM["peek"] = None

    count = MEM.get("escapes", 0) + 1
    MEM["escapes"] = count
    turn = 1 if (MEM.get("seat", 0) + count) % 2 else -1
    MEM["escape_yaw"] = me.get("yaw", 0) + turn * (1.2 + .4 * (count % 3))
    MEM["escape_until"] = tick + 50
    MEM["progress_anchor"], MEM["progress_tick"] = pos, tick + 50

    action.update(escape_action(me))


def seat_departure_tick(seat):
    return 0


def seat_offset(seat, is_carrier):
    """Return (dx, dy) lateral offset for this seat."""
    off = SEAT_OFFSETS[seat % len(SEAT_OFFSETS)]
    if is_carrier:
        # Carrier must land at the site center so dist_to_dest is reliably
        # well inside the plant gate (site_radius - 0.5). Zero offset avoids
        # the radius-edge flakiness that can delay the plant in real matches.
        return (0.0, 0.0)
    return off



def opposition_entry(obs, site):
    """A public-map approach estimate, never an unseen enemy position."""
    if not site:
        return None
    key = (obs.get("side"), tuple(site))
    cached = MEM.setdefault("entry_points", {})
    if key in cached:
        return cached[key]
    spawns = (MEM.get("map") or {}).get("spawns") or {}
    enemy_keys = ("CT", "defend") if obs.get("side") == "attack" else ("T", "attack")
    raw = next((spawns[k] for k in enemy_keys if spawns.get(k)), [])
    if isinstance(raw, list) and raw and isinstance(raw[0], (int, float)):
        positions = [raw]
    else:
        positions = [x.get("position") if isinstance(x, dict) else x for x in raw]
        positions = [x for x in positions if isinstance(x, (list, tuple)) and len(x) >= 2]
    if not positions:
        return None
    start = tuple(sum(p[i] if len(p) > i else 0 for p in positions) / len(positions) for i in range(3))
    route = pathfind(start, site, MEM["nodes"], MEM["by_id"], MEM["adj"])
    entry = next((p for p in reversed(route) if dist2(p, site) >= 16), start)
    cached[key] = entry
    return entry


def awareness(obs, me):
    """Use only own HP changes and coarse public seat sound cues."""
    tick = MEM["tick"]
    hp = me.get("hp", 100)
    before = MEM.get("previous_hp")
    if before is None:
        before = hp
    MEM["previous_hp"] = hp
    if obs.get("visible") or obs.get("phase") != "live" or me.get("blind_ticks"):
        return
    if tick < MEM.get("alert_until", 0):
        return
    damaged = hp < before - .1
    sounds = obs.get("sounds") or obs.get("heard") or []
    cues = [s for s in sounds if isinstance(s, dict) and
            s.get("distance_band") == "near" and s.get("type") in ("footstep", "land_hard", "land_soft", "fire", "gunshot") and
            s.get("direction") in ("left", "right", "back", "behind", "front")]
    if not damaged and (not cues or tick < MEM.get("sound_check_after", 0)):
        return
    if damaged:
        # No attacker coordinates were received: look back briefly, do not invent a target.
        delta = math.pi
        MEM["alert_until"] = tick + 60
    else:
        direction = cues[0]["direction"]
        delta = {"left":math.pi/2, "right":-math.pi/2, "back":math.pi, "behind":math.pi, "front":0}[direction]
        MEM["alert_until"] = tick + 30
    MEM["alert_yaw"] = wrap(me.get("yaw", 0) + delta)
    MEM["sound_check_after"] = tick + 150


def watch_entry(me, action, look):
    if look is None:
        return
    error = wrap(math.atan2(-(look[1]-me["y"]), look[0]-me["x"]) - me.get("yaw", 0))
    action["yaw_rate"] = clip(error / .15, -1, 1)
    distance = max(.1, math.hypot(look[0]-me["x"], look[1]-me["y"]))
    action["pitch_rate"] = clip((math.atan2(look[2]+1.18-me["z"]-1.62, distance)-me.get("pitch",0))/.1,-1,1)


def init(context):
    if context.get("contract_id") != "rsi-fps-5.5":
        raise ValueError("Expected rsi-fps-5.5")
    MEM.clear()
    MEM["seat"] = context.get("seat")
    MEM["map"] = None
    MEM["nodes"] = []
    MEM["by_id"] = {}
    MEM["adj"] = {}


def act(observation):
    try:
        return _act(observation)
    except Exception:
        return {}


def _act(observation):
    if observation.get("map"):
        MEM["map"] = observation["map"]
        MEM["nodes"], MEM["by_id"], MEM["adj"] = build_nav(observation["map"])

    me = observation.get("me") or {}
    if not me.get("alive", True) or observation.get("phase") in ("post", "finished"):
        return {}

    reset_round(observation)
    round_tick = observation.get("round_tick") or 0
    MEM["tick"] = observation.get("match_tick", round_tick)
    side = observation.get("side") or "attack"
    seat = observation.get("seat", MEM.get("seat", 0))
    action = {}

    item = buy(observation, me, side)
    if item:
        action["buy"] = item

    if observation.get("phase") == "buy" or "x" not in me:
        return action

    pos = (me.get("x", 0), me.get("y", 0), me.get("z", 0))

    departure_tick = seat_departure_tick(seat)
    holding = round_tick < departure_tick

    awareness(observation, me)
    enemy = nearest_enemy(me, [e for e in observation.get("visible") or [] if e.get("alive", True)])
    bomb = observation.get("bomb") or {}
    bomb_state = bomb.get("state")

    if enemy:
        MEM["alert_until"] = 0
        action.update(fight(me, enemy))
        MEM["last_enemy"] = enemy
        MEM["last_enemy_tick"] = round_tick
        MEM["stuck"] = 0
        MEM["progress_anchor"] = None
        return action

    is_carrier = bool(me.get("carrying_bomb"))
    hunting = side == "attack" and not is_carrier and bomb_state != "planted"
    last_enemy = MEM.get("last_enemy")
    chase = (
        hunting
        and last_enemy
        and round_tick - MEM.get("last_enemy_tick", 0) < 160
    )

    site_center = get_site_center(observation, side, seat)
    site_radius = get_site_radius(observation, side, seat)
    entry = opposition_entry(observation, site_center)

    if chase:
        dest = (last_enemy["x"], last_enemy["y"], last_enemy.get("z") or 0)
    elif side == "defend" and bomb_state == "planted" and bomb.get("position"):
        bp = bomb["position"]
        dest = (bp[0], bp[1], bp[2] if len(bp) > 2 else 0)
    elif site_center:
        ox, oy = seat_offset(seat, is_carrier)
        dest = (site_center[0] + ox, site_center[1] + oy, site_center[2])
    else:
        dest = None

    if MEM["tick"] < MEM.get("escape_until", 0) and not holding:
        action.update(escape_action(me))
        action["weapon_slot"] = 0 if _has_primary(me) else 1
        return action

    if dest:
        goal_key = (round(dest[0], 1), round(dest[1], 1), round(dest[2], 1))
        if MEM.get("goal") != goal_key or not MEM.get("route"):
            MEM["goal"] = goal_key
            MEM["route"] = pathfind(pos, dest, MEM["nodes"], MEM["by_id"], MEM["adj"])

        route = MEM["route"]
        while len(route) > 1 and dist2(pos, route[0]) < .65 and abs(pos[2] - route[0][2]) < 2.:
            route.pop(0)

        next_point = route[0] if route else dest
        dist_to_dest = dist3(pos, dest)

        planting = side == "attack" and is_carrier and dist_to_dest < site_radius - 0.5
        defusing = side == "defend" and bomb_state == "planted" and dist_to_dest < 2.8

        if holding:
            action["move_forward"] = 0.0
            action["move_right"] = 0.0
            action["weapon_slot"] = 0 if _has_primary(me) else 1
            if _has_primary(me) and (me.get("ammo") or 0) == 0:
                action["reload"] = True
            return action

        peeking = False
        if not hunting and not planting and not defusing and len(route) > 1 and not MEM.get("peek_done"):
            key = (round(route[0][0], 1), round(route[0][1], 1))
            if (MEM.get("peek") is None and key not in MEM.setdefault("peeked", set())
                    and dist3(pos, route[0]) < PEEK["near"] and corner_ahead(pos, route)):
                MEM["peek"] = {"tick": 0, "dir": 1.0, "key": key, "prev_dir": None, "reversals": 0}
            if MEM.get("peek") is not None:
                peek = jiggle_peek(me, route)
                if peek:
                    action.update(peek)
                    peeking = True
                else:
                    MEM.setdefault("peeked", set()).add(key)

        if not peeking:
            look = entry if entry and side == "defend" and not defusing else None
            if last_enemy and round_tick - MEM.get("last_enemy_tick", 0) < 90:
                look = (last_enemy["x"], last_enemy["y"], last_enemy.get("z", 0))
            move, _ = steer_to(me, next_point, look=look)
            action.update(move)

        if side == "defend" and not defusing and bomb_state != "planted" and dist_to_dest < .9:
            action["move_forward"] = action["move_right"] = 0.
            watch_entry(me, action, entry)

        if planting:
            action["use"] = "plant"
            action["move_forward"] = 0.0
            action["move_right"] = 0.0
            MEM["peek"] = None
        if defusing:
            action["move_forward"] = 0.0
            action["move_right"] = 0.0
            speed = math.hypot(me.get("vx") or 0, me.get("vy") or 0)
            if speed < 0.5:
                action["use"] = "defuse"
                action["move_forward"] = 0.0
                action["move_right"] = 0.0
                MEM["peek"] = None

    if not dest and last_enemy and round_tick - MEM.get("last_enemy_tick", 0) < 40:
        chase_dest = (last_enemy["x"], last_enemy["y"], last_enemy.get("z", 0))
        move, _ = steer_to(me, chase_dest)
        action.update(move)

    if _has_primary(me) and "weapon_slot" not in action:
        action["weapon_slot"] = 0

    if _has_primary(me) and (me.get("ammo") or 0) == 0 and "reload" not in action:
        action["reload"] = True

    if me.get("ladder_id"):
        action["ladder"] = "attach"
        action["move_forward"] = 1.0

    if "move_forward" not in action and "move_right" not in action:
        action["move_forward"] = 0.0
        action["move_right"] = 0.0

    alert = MEM["tick"] < MEM.get("alert_until", 0)
    if not holding:
        recover_navigation(me, pos, action)
    if alert and MEM["tick"] >= MEM.get("escape_until", 0) and action.get("use") not in ("plant", "defuse"):
        action["move_forward"] = action["move_right"] = 0.
        action["yaw_rate"] = clip(wrap(MEM["alert_yaw"]-me.get("yaw",0))/.15,-1,1)

    if _has_primary(me) and (me.get("ammo") or 0) == 0 and "reload" not in action:
        action["reload"] = True

    return action
