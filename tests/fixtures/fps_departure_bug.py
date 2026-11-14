"""METHOD
Category: rules; search
Approach: One seat. The bomb carrier plants. An attacker without the bomb
hunts kills until it is planted. Defenders hold, then retake a planted bomb.
Data & training: Public match score. The coach rewrites strategy.py.
Changes: Fixed navigation route trimming, plant threshold, and ensured
movement toward site even when pathfinding fails.
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
    """Build adjacency list and node lookup from map.nav."""
    nodes = []
    by_id = {}
    adj = {}
    if not map_obj or "nav" not in map_obj:
        return nodes, by_id, adj
    nav = map_obj["nav"]
    for n in nav.get("nodes", []):
        nodes.append(n)
        by_id[n["id"]] = n
    for e in nav.get("edges", []):
        adj.setdefault(e["from"], []).append(e)
        if e.get("kind") in ("walk", "crouch", "jump", "source-adjacency"):
            adj.setdefault(e["to"], []).append({"from": e["to"], "to": e["from"], "kind": e["kind"]})
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
    """Dijkstra pathfinding on the nav graph. Returns list of positions."""
    if not nodes:
        return [list(end_pos)]
    start_node = find_nearest_node(start_pos, nodes)
    end_node = find_nearest_node(end_pos, nodes)
    if not start_node or not end_node:
        return [list(end_pos)]
    
    start_id = start_node["id"]
    end_id = end_node["id"]
    
    if start_id == end_id:
        return [list(end_pos)]
    
    cost = {start_id: 0}
    prev = {}
    open_set = [start_id]
    closed = set()
    
    while open_set:
        open_set.sort(key=lambda x: cost.get(x, float('inf')))
        node_id = open_set.pop(0)
        if node_id in closed:
            continue
        closed.add(node_id)
        if node_id == end_id:
            break
        node = by_id.get(node_id)
        if not node:
            continue
        for edge in adj.get(node_id, []):
            next_id = edge["to"]
            if next_id in closed:
                continue
            next_node = by_id.get(next_id)
            if not next_node:
                continue
            edge_cost = dist3(node.get("position", [0,0,0]), next_node.get("position", [0,0,0]))
            new_cost = cost[node_id] + edge_cost
            if next_id not in cost or new_cost < cost[next_id]:
                cost[next_id] = new_cost
                prev[next_id] = node_id
                if next_id not in open_set:
                    open_set.append(next_id)
    
    if end_id not in cost:
        return [list(end_pos)]
    
    ids = [end_id]
    while ids[0] != start_id and ids[0] in prev:
        ids.insert(0, prev[ids[0]])
    
    positions = [list(by_id[i].get("position", [0,0,0])) for i in ids if i in by_id]
    positions.append(list(end_pos))
    return positions


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
    style = STRATEGY.get("buy_style", "full_rifle")
    items = []
    for item in WISH.get(style, WISH["full_rifle"]):
        need = SIDE_OK.get(item)
        if need and need != side:
            continue
        items.append(item)
    if "armor" not in items:
        items.append("armor")
    return items


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
    for item in wish_list(side):
        if item in have or item in attempted:
            continue
        if item in ("ak47", "m4a1", "galilar", "mac10", "mp9") and have & {"ak47", "m4a1", "galilar", "mac10", "mp9"}:
            continue
        if cash < PRICES.get(item, 10**9):
            continue
        attempted.add(item)
        return item
    return None


def get_site_center(obs, side, seat):
    """Get the target site center from map.sites."""
    map_obj = MEM.get("map")
    if not map_obj:
        return None
    sites = map_obj.get("sites", [])
    if not sites:
        return None
    if side == "attack":
        letter = STRATEGY.get("default_site", "B")
        idx = 0 if letter == "A" else 1
    else:
        letter = STRATEGY.get("expect_site", STRATEGY.get("default_site", "B"))
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
        letter = STRATEGY.get("default_site", "B")
        idx = 0 if letter == "A" else 1
    else:
        letter = STRATEGY.get("expect_site", STRATEGY.get("default_site", "B"))
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


AIM = {"burst_max": 6, "burst_pause": 6}
PEEK = {"out": 5, "back": 5, "max": 2, "turn": 0.6, "near": 6.0}


def fight(me, enemy):
    """Aim at the enemy and fire in short bursts so recoil drift stays inside tolerance."""
    dx = enemy["x"] - me["x"]
    dy = enemy["y"] - me["y"]
    dz = (enemy.get("z") or 0) + 1.64 - ((me.get("z") or 0) + 1.64)
    dist = max(0.4, math.hypot(dx, dy))
    base_yaw = math.atan2(-dy, dx)
    base_pitch = clip(math.atan2(dz, dist), -1.2, 1.2)
    speed = math.hypot(me.get("vx") or 0, me.get("vy") or 0)

    slot = 0 if _has_primary(me) else 1
    desired_yaw = base_yaw
    desired_pitch = clip(base_pitch, -1.2, 1.2)

    yaw_error = wrap(desired_yaw - (me.get("yaw") or 0))
    pitch_error = desired_pitch - (me.get("pitch") or 0)
    aligned = abs(yaw_error) < 0.15 and abs(pitch_error) < 0.18
    should_stop = dist < 12 or (aligned and dist < 25)

    burst = MEM.get("burst", 0)
    pause = MEM.get("burst_pause", 0)
    ready = aligned and speed < 0.6 and dist < 30 and pause == 0 and (me.get("ammo") or 0) > 0
    fire = ready and burst < AIM["burst_max"]
    if fire:
        MEM["burst"] = burst + 1
    elif burst >= AIM["burst_max"]:
        MEM["burst"] = 0
        MEM["burst_pause"] = AIM["burst_pause"]
    elif not ready:
        MEM["burst"] = 0
    if pause:
        MEM["burst_pause"] = pause - 1

    return {
        "yaw_rate": clip(yaw_error / 0.2, -1, 1),
        "pitch_rate": clip(pitch_error / 0.15, -1, 1),
        "move_forward": 0.0 if should_stop else (1.0 if abs(yaw_error) < 0.8 else 0.5),
        "fire": fire,
        "weapon_slot": slot,
        "reload": (me.get("ammo") or 0) == 0 and _has_primary(me),
    }


def corner_ahead(pos, route):
    """True when the route turns sharply at the next waypoint."""
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
    """Strafe out from cover to gain line of sight, then back, pre-aiming the corner."""
    peek = MEM.setdefault("peek", {"count": 0, "tick": 0, "dir": 1.0})
    cycle = PEEK["out"] + PEEK["back"]
    if peek["tick"] >= cycle:
        peek["tick"] = 0
        peek["count"] += 1
        peek["dir"] = -peek["dir"]
    if peek["count"] >= PEEK["max"]:
        MEM.setdefault("peeked", set()).add(peek.get("key"))
        MEM["peek"] = None
        return None
    out = peek["tick"] < PEEK["out"]
    peek["tick"] += 1
    target = route[1] if len(route) > 1 else route[0]
    dx, dy = target[0] - me["x"], target[1] - me["y"]
    want = math.atan2(-dy, dx)
    error = wrap(want - (me.get("yaw") or 0))
    return {
        "yaw_rate": clip(error / 0.25, -1, 1),
        "move_forward": 0.0,
        "move_right": peek["dir"] if out else -peek["dir"],
        "weapon_slot": 0 if _has_primary(me) else 1,
    }


def steer_to(me, point):
    """Steer toward a point."""
    dx = point[0] - me["x"]
    dy = point[1] - me["y"]
    dist = math.hypot(dx, dy)
    desired = math.atan2(-dy, dx)
    error = wrap(desired - (me.get("yaw") or 0))
    action = {
        "yaw_rate": clip(error / 0.3, -1, 1),
        "move_forward": 1.0 if abs(error) < 0.5 else (0.5 if abs(error) < 1.0 else 0.0),
    }
    return action, dist


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
    # Cache map
    if observation.get("map"):
        MEM["map"] = observation["map"]
        MEM["nodes"], MEM["by_id"], MEM["adj"] = build_nav(observation["map"])
    
    me = observation.get("me") or {}
    if not me.get("alive", True) or observation.get("phase") in ("post", "finished"):
        return {}
    
    reset_round(observation)
    MEM["round_tick"] = observation.get("round_tick") or 0
    side = observation.get("side") or "attack"
    seat = observation.get("seat", MEM.get("seat", 0))
    action = {}
    
    # Buy
    item = buy(observation, me, side)
    if item:
        action["buy"] = item
    
    if observation.get("phase") == "buy" or "x" not in me:
        return action
    
    pos = (me.get("x", 0), me.get("y", 0), me.get("z", 0))
    live_seconds = (MEM["round_tick"] or 0) / 20.0
    
    # Staggered departure: hold movement until round_tick >= 360 + (seat % 5) * 90
    depart_tick = 360 + (seat % 5) * 90
    
    # Check for visible enemy
    enemy = nearest_enemy(me, observation.get("visible") or [])
    bomb = observation.get("bomb") or {}
    
    if enemy:
        action.update(fight(me, enemy))
        MEM["last_enemy"] = enemy
        MEM["last_enemy_tick"] = MEM["round_tick"]
    else:
        # Determine destination with per-seat offset to spread the team
        bomb_state = bomb.get("state")
        hunting = side == "attack" and not me.get("carrying_bomb") and bomb_state != "planted"
        last_enemy = MEM.get("last_enemy")
        chase = (
            hunting
            and last_enemy
            and MEM["round_tick"] - MEM.get("last_enemy_tick", 0) < 160
        )
        
        if chase:
            dest = (last_enemy["x"], last_enemy["y"], last_enemy.get("z") or 0)
        elif side == "defend" and bomb_state == "planted" and bomb.get("position"):
            bp = bomb["position"]
            dest = (bp[0], bp[1], bp[2] if len(bp) > 2 else 0)
        else:
            dest = get_site_center(observation, side, seat)
            if dest:
                # Per-seat lateral offset to fan out teammates
                # Carrier (seat 0) gets a small offset; others get wider
                offset_mag = 0.3 if seat == 0 else 1.0
                angle_off = (seat * 2 * math.pi / 5)
                dest = (dest[0] + offset_mag * math.cos(angle_off),
                         dest[1] + offset_mag * math.sin(angle_off),
                         dest[2])
        
        if dest:
            # Build or reuse route
            goal_key = (round(dest[0], 1), round(dest[1], 1), round(dest[2], 1))
            if MEM.get("goal") != goal_key or not MEM.get("route"):
                MEM["goal"] = goal_key
                MEM["route"] = pathfind(pos, dest, MEM["nodes"], MEM["by_id"], MEM["adj"])
            
            # Trim route
            route = MEM["route"]
            while len(route) > 1 and dist3(pos, route[0]) < 1.4:
                route.pop(0)
            
            next_point = route[0] if route else dest
            dist_to_dest = dist3(pos, dest)
            site_radius = get_site_radius(observation, side, seat)
            planting = side == "attack" and me.get("carrying_bomb") and dist_to_dest < site_radius - 0.5
            defusing = side == "defend" and bomb_state == "planted" and dist_to_dest < 2.8
            
            peeking = False
            if not hunting and not planting and not defusing and len(route) > 1:
                key = (round(route[0][0], 1), round(route[0][1], 1))
                if (MEM.get("peek") is None and key not in MEM.setdefault("peeked", set())
                        and dist3(pos, route[0]) < PEEK["near"] and corner_ahead(pos, route)):
                    MEM["peek"] = {"count": 0, "tick": 0, "dir": 1.0, "key": key}
                if MEM.get("peek") is not None:
                    peek = jiggle_peek(me, route)
                    if peek:
                        action.update(peek)
                        peeking = True
            
            if not peeking:
                move, _ = steer_to(me, next_point)
                action.update(move)
            
            # Objective: plant or defuse
            if planting:
                action["use"] = "plant"
                action["move_forward"] = 0.0
                action.pop("move_right", None)
                MEM["peek"] = None
            if defusing:
                speed = math.hypot(me.get("vx") or 0, me.get("vy") or 0)
                if speed < 0.5:
                    action["use"] = "defuse"
                    action["move_forward"] = 0.0
                    action.pop("move_right", None)
                    MEM["peek"] = None
        
        # Chase last seen enemy briefly if no destination
        if not dest and last_enemy and MEM["round_tick"] - MEM.get("last_enemy_tick", 0) < 40:
            chase_dest = (last_enemy["x"], last_enemy["y"], last_enemy.get("z", 0))
            move, _ = steer_to(me, chase_dest)
            action.update(move)
    
    # Weapon slot
    if _has_primary(me) and "weapon_slot" not in action:
        action["weapon_slot"] = 0
    
    # Reload
    if _has_primary(me) and (me.get("ammo") or 0) == 0 and "reload" not in action:
        action["reload"] = True
    
    # Staggered departure: hold movement until the seat's depart tick
    if MEM["round_tick"] < depart_tick and not enemy and not action.get("use"):
        action["move_forward"] = 0.0
        action.pop("move_right", None)
    
    # Stuck detection with per-seat varied sidestep
    last = MEM.get("last_pos")
    if last and pos[0] is not None:
        moved = math.hypot(pos[0] - last[0], pos[1] - last[1])
        if moved < 0.03 and not action.get("fire") and action.get("use") not in ("plant", "defuse"):
            MEM["stuck"] = MEM.get("stuck", 0) + 1
        else:
            MEM["stuck"] = 0
    else:
        MEM["stuck"] = 0
    MEM["last_pos"] = pos
    
    if enemy:
        MEM["stuck"] = 0
    elif MEM.get("stuck", 0) > 10 and action.get("use") not in ("plant", "defuse"):
        # Vary sidestep direction by seat and escape count
        escapes = MEM.get("stuck", 0) // 8
        turn = 1.0 if (seat + escapes) % 2 == 0 else -1.0
        action["move_right"] = turn
        action["jump"] = MEM["stuck"] % 8 == 0
        action["move_forward"] = 0.5
    
    # Ladder
    if me.get("ladder_id"):
        action["ladder"] = "attach"
        action["move_forward"] = 1.0
    
    return action
