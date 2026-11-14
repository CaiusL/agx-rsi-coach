"""Deterministic local match fixture for a candidate FPS agent.

This is an approximation of the engine, not the engine. It exists so a rewrite
can be rejected before it spends an upload. Run as a script:

    python3 -I coach/gate_fps.py <candidate_dir>

It prints one JSON object. The candidate directory holds agent.py and strategy.py.
"""

from __future__ import annotations

import importlib.util
import functools
import types
import json
import math
import sys
import traceback
from pathlib import Path

DT = 0.05
SPEED = 5.0
TURN = 3.0
FREEZE_TICKS = 120
BUY_WINDOW_TICKS = 500
LIVE_TICKS = 2300
PLANT_TICKS = 60
DEFUSE_TICKS = 200
KIT_TICKS = 100
RELOAD_TICKS = 40
WALL = (3.0, 3.0, 15.0, 15.0)
BOUNDS = 30.0
RECOIL_YAW = 0.05
RECOIL_PITCH = 0.18

ACTION_KEYS = {
    "weapon_slot", "buy", "yaw_rate", "pitch_rate", "fire", "reload",
    "move_forward", "move_right", "jump", "use", "ladder",
}

PRICES = {
    "armor": 650, "helmet": 1000, "defusekit": 400, "decoy": 50,
    "flashbang": 200, "smokegrenade": 300, "molotov": 400, "incgrenade": 500,
    "ak47": 2700, "m4a1": 2900, "galilar": 1800, "mac10": 1050,
    "mp9": 1250, "tec9": 500, "fiveseven": 500,
}


def clip(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:
        return low
    return max(low, min(high, number))


def in_wall(x, y):
    return WALL[0] <= x <= WALL[2] and WALL[1] <= y <= WALL[3]


def build_map():
    nodes = []
    index = {}
    for ix in range(-4, 5):
        for iy in range(-4, 5):
            x, y = ix * 6.0, iy * 6.0
            if in_wall(x, y):
                continue
            index[(ix, iy)] = len(nodes)
            nodes.append({"id": len(nodes), "position": [x, y, 0.0]})
    edges = []
    for (ix, iy), source in index.items():
        for dx, dy in ((1, 0), (0, 1)):
            target = index.get((ix + dx, iy + dy))
            if target is not None:
                edges.append({"from": source, "to": target, "kind": "walk"})
                edges.append({"from": target, "to": source, "kind": "walk"})
    return {
        "name": "gate_fixture",
        "nav": {"nodes": nodes, "edges": edges},
        "sites": [
            {"id": "A", "center": [24.0, 24.0, 0.0], "radius": 4.0},
            {"id": "B", "center": [-24.0, 24.0, 0.0], "radius": 4.0},
        ],
        "spawns": {"attack": [0.0, -24.0, 0.0], "defend": [0.0, -24.0, 0.0]},
    }


MAP = build_map()
SITES = {site["id"]: site for site in MAP["sites"]}
SEEN_KEYS = set()


class Seat:
    def __init__(self, side, cash, weapons, kit=False, carrying=False):
        self.side = side
        self.x, self.y = MAP["spawns"][side][:2]
        self.yaw = math.pi / 2
        self.pitch = 0.0
        self.vx = self.vy = 0.0
        self.cash = cash
        self.armor = 0
        self.kit = kit
        self.weapons = weapons
        self.carrying = carrying
        self.reload_left = 0
        self.goal = None
        self.plant_ticks = 0
        self.defuse_ticks = 0
        self.travel = 0.0
        self.idle = 0
        self.move_ticks = 0
        self.toward_ticks = 0
        self.plant_outside = 0
        self.plant_outside_still = 0
        self.shots = 0
        self.hits = 0
        self.reloads = 0
        self.bought = []
        self.keys = set()
        self.hist = {}
        self.empty = 0
        self.peek_out = 0
        self.peek_switches = 0
        self._last_strafe = 0.0
        self.closest = {name: 1e9 for name in SITES}

    def primary(self):
        for weapon in self.weapons:
            if isinstance(weapon, dict) and weapon.get("id") not in (None, "knife", "pistol"):
                return weapon
        return None

    def ammo(self):
        weapon = self.primary()
        return int(weapon.get("ammo") or 0) if weapon else 0

    def observation(self, tick, phase, bomb, enemy, mates):
        seen = None
        if enemy:
            seen = [{key: enemy[key] for key in ("seat", "x", "y", "z", "yaw", "alive") if key in enemy}]
        primary = self.primary()
        return {
            "phase": phase,
            "round_index": 0,
            "round_tick": tick,
            "match_tick": tick,
            "buy_deadline_tick": FREEZE_TICKS + BUY_WINDOW_TICKS,
            "side": self.side,
            "seat": 0,
            "map": MAP,
            "me": {
                "alive": True, "x": self.x, "y": self.y, "z": 0.0,
                "yaw": self.yaw, "pitch": self.pitch, "vx": self.vx, "vy": self.vy,
                "cash": self.cash, "armor": self.armor, "helmet": False,
                "has_kit": self.kit, "weapons": self.weapons,
                "slots": {"primary": primary["id"] if primary else None, "secondary": "pistol", "knife": "knife"},
                "ammo": self.ammo(), "throwable_kinds": ["flashbang"],
                "carrying_bomb": self.carrying, "ladder_id": None,
            },
            "visible": seen,
            "teammates": [{key: mate[key] for key in ("seat", "x", "y", "z", "alive") if key in mate} for mate in mates],
            "bomb": bomb,
        }

    def apply(self, action, bomb, enemy):
        SEEN_KEYS.update(action.keys())
        self.keys.update(action.keys())
        for key, value in action.items():
            if key in ("use", "buy", "ladder"):
                label = f"{key}={value}"
            elif value:
                label = key
            else:
                continue
            self.hist[label] = self.hist.get(label, 0) + 1
        moving = clip(action.get("move_forward", 0), -1, 1) or clip(action.get("move_right", 0), -1, 1)
        acting = action.get("fire") or action.get("use") or action.get("reload") or action.get("buy")
        if not moving and not acting:
            self.idle += 1
        if not action:
            self.empty += 1

        item = action.get("buy")
        if item in PRICES and self.cash >= PRICES[item] and item not in self.bought:
            self.cash -= PRICES[item]
            self.bought.append(item)
            if item == "armor":
                self.armor = 100
            elif item == "defusekit":
                self.kit = True
            elif item in ("ak47", "m4a1", "galilar", "mac10", "mp9"):
                self.weapons[0] = {"id": item, "ammo": 30, "recoil_yaw": RECOIL_YAW, "recoil_pitch": RECOIL_PITCH}

        self.yaw += clip(action.get("yaw_rate", 0), -1, 1) * TURN * DT
        self.pitch = clip(self.pitch + clip(action.get("pitch_rate", 0), -1, 1) * TURN * DT, -1.2, 1.2)
        forward = clip(action.get("move_forward", 0), -1, 1) * SPEED * DT
        right = clip(action.get("move_right", 0), -1, 1) * SPEED * DT
        strafe = clip(action.get("move_right", 0), -1, 1)
        if strafe and not forward:
            self.peek_out += 1
            if self._last_strafe and strafe * self._last_strafe < 0:
                self.peek_switches += 1
            self._last_strafe = strafe
        elif forward:
            self._last_strafe = 0.0
        if forward or right:
            self.move_ticks += 1
        dx = math.cos(self.yaw) * forward + math.sin(self.yaw) * right
        dy = -math.sin(self.yaw) * forward + math.cos(self.yaw) * right
        if self.goal and (dx or dy):
            if dx * (self.goal[0] - self.x) + dy * (self.goal[1] - self.y) > 0:
                self.toward_ticks += 1
        nx, ny = self.x + dx, self.y + dy
        if in_wall(nx, ny) or abs(nx) > BOUNDS or abs(ny) > BOUNDS:
            self.vx = self.vy = 0.0
        else:
            self.travel += math.hypot(nx - self.x, ny - self.y)
            self.x, self.y = nx, ny
            self.vx, self.vy = dx / DT, dy / DT
        for name, site in SITES.items():
            self.closest[name] = min(self.closest[name], math.hypot(self.x - site["center"][0], self.y - site["center"][1]))

        if self.reload_left:
            self.reload_left -= 1
            if self.reload_left == 0:
                weapon = self.primary()
                if weapon:
                    weapon["ammo"] = 30
        elif action.get("reload") and self.ammo() < 30:
            self.reload_left = RELOAD_TICKS
            self.reloads += 1

        if action.get("fire") and self.ammo() > 0 and self.reload_left == 0:
            weapon = self.primary()
            weapon["ammo"] = int(weapon["ammo"]) - 1
            self.shots += 1
            if enemy:
                dist = math.hypot(enemy["x"] - self.x, enemy["y"] - self.y)
                want = math.atan2(-(enemy["y"] - self.y), enemy["x"] - self.x)
                error = abs((want - self.yaw + math.pi) % (2 * math.pi) - math.pi)
                dz = (enemy.get("z") or 0) - 0.0
                want_pitch = math.atan2(dz, max(0.4, dist))
                pitch_error = abs(want_pitch - self.pitch)
                if dist < 35 and error < 0.15 and pitch_error < 0.15:
                    self.hits += 1
                    enemy["hp"] = max(0, enemy.get("hp", 100) - 27)
            # Every shot kicks the view. An agent that does not compensate drifts off target.
            self.yaw += weapon.get("recoil_yaw") or 0.0
            self.pitch = clip(self.pitch + (weapon.get("recoil_pitch") or 0.0), -1.2, 1.2)

        use = action.get("use")
        if use == "plant" and not self.site_here():
            self.plant_outside += 1
            if not forward and not right:
                self.plant_outside_still += 1
        if use == "plant" and self.carrying and bomb["state"] in ("carried", "dropped"):
            here = self.site_here()
            if here and math.hypot(self.vx, self.vy) < 0.5:
                self.plant_ticks += 1
                if self.plant_ticks >= PLANT_TICKS:
                    bomb["state"] = "planted"
                    bomb["position"] = [self.x, self.y, 0.0]
                    self.carrying = False
            else:
                self.plant_ticks = 0
        else:
            self.plant_ticks = 0
        if use == "defuse" and bomb["state"] == "planted":
            spot = bomb.get("position") or [0, 0, 0]
            if math.hypot(spot[0] - self.x, spot[1] - self.y) <= 2.8 and math.hypot(self.vx, self.vy) < 0.5:
                self.defuse_ticks += 1
                if self.defuse_ticks >= (KIT_TICKS if self.kit else DEFUSE_TICKS):
                    bomb["state"] = "defused"
            else:
                self.defuse_ticks = 0
        else:
            self.defuse_ticks = 0

    def site_here(self):
        for name, site in SITES.items():
            if math.hypot(self.x - site["center"][0], self.y - site["center"][1]) <= site["radius"]:
                return name
        return None


def rifle(ammo=30):
    return [
        {"id": "ak47", "ammo": ammo, "recoil_yaw": RECOIL_YAW, "recoil_pitch": RECOIL_PITCH},
        {"id": "pistol", "ammo": 12, "recoil_yaw": RECOIL_YAW * 0.5, "recoil_pitch": RECOIL_PITCH * 0.5},
        {"id": "knife"},
    ]


def load_agent(folder: Path):
    sys.path.insert(0, str(folder))
    for name in ("strategy", "agent"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("candidate_agent", folder / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monitor_empty_errors(module)
    return module


def monitor_empty_errors(module):
    """Observe helper exceptions swallowed into empty live actions; do not alter behavior."""
    original_act = module.act
    pending = []
    module._gate_empty_errors = []
    for name, function in list(vars(module).items()):
        if name == "act" or not isinstance(function, types.FunctionType) or function.__globals__ is not vars(module):
            continue
        def observed(*args, _function=function, **kwargs):
            try:
                return _function(*args, **kwargs)
            except Exception as exc:
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                pending.append(f"function={frame.name} line={frame.lineno} {type(exc).__name__}: {exc}")
                raise
        setattr(module, name, functools.wraps(function)(observed))
    def act(observation):
        pending.clear()
        action = original_act(observation)
        if (not action and pending and observation.get("phase") == "live"
                and (observation.get("me") or {}).get("alive", True)):
            module._gate_empty_errors.append(tuple(dict.fromkeys(pending)))
        return action
    module.act = act


def run_round(agent, seat, bomb, enemy=None, ticks=FREEZE_TICKS + LIVE_TICKS, goal=None, seat_id=0):
    agent.init({"contract_id": "rsi-fps-5.5", "seat": seat_id, "role": 0, "seed": 1, "weights_dir": "."})
    seat.goal = goal
    mates = [{"seat": 1, "alive": True, "x": seat.x + 2, "y": seat.y, "zone": "mid"}]
    for tick in range(ticks):
        phase = "buy" if tick < FREEZE_TICKS else "live"
        if enemy is not None and enemy.get("hp", 100) <= 0:
            enemy = None
        observation = seat.observation(tick, phase, bomb, enemy, mates)
        observation["seat"] = seat_id
        action = agent.act(observation)
        if not isinstance(action, dict):
            raise TypeError(f"act returned {type(action).__name__}, expected dict")
        if (phase == "live" and enemy is None and not action.get("use")
                and tick < 360 + (seat_id % 5) * 90
                and (action.get("move_forward") or action.get("move_right"))):
            seat.early_moves = getattr(seat, "early_moves", 0) + 1
        seat.apply(action, bomb, enemy)
        if bomb["state"] == "defused" or seat.plant_ticks >= PLANT_TICKS:
            return tick
    return ticks


def main():
    folder = Path(sys.argv[1])
    checks = []
    facts = {}

    def record(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    source = (folder / "agent.py").read_text()
    try:
        compile(source, "agent.py", "exec")
        record("syntax", True, f"{len(source.encode())} bytes compiles")
    except SyntaxError as exc:
        record("syntax", False, f"line {exc.lineno}: {exc.msg}")
        return finish(checks, facts)

    try:
        agent = load_agent(folder)
    except Exception:
        record("import", False, traceback.format_exc(limit=3).strip())
        return finish(checks, facts)
    for hook in ("init", "act"):
        if not callable(getattr(agent, hook, None)):
            record("import", False, f"agent.py does not define {hook}()")
            return finish(checks, facts)
    try:
        agent.init({"contract_id": "rsi-fps-5.5", "seat": 0, "role": 0, "seed": 1, "weights_dir": str(folder)})
        record("init", True, "accepted rsi-fps-5.5")
    except Exception:
        record("init", False, traceback.format_exc(limit=3).strip())
        return finish(checks, facts)

    strategy = {}
    try:
        import strategy as book
        strategy = dict(getattr(book, "STRATEGY", {}))
    except Exception:
        record("strategy", False, "strategy.py did not import")
    facts["strategy"] = strategy.get("name")
    site = strategy.get("default_site") or "B"

    try:
        buy_seat = Seat("attack", 800, [None, {"id": "pistol", "ammo": 12}, {"id": "knife"}])
        bomb = {"state": "carried", "position": [buy_seat.x, buy_seat.y, 0.0], "carrier_seat": 0}
        run_round(agent, buy_seat, bomb, ticks=FREEZE_TICKS + 60)
        record("buy", bool(buy_seat.bought), f"bought {buy_seat.bought or 'nothing'} with 800 cash")
        facts["bought"] = buy_seat.bought

        attack = Seat("attack", 16000, rifle(), carrying=True)
        attack.carrying = True
        bomb = {"state": "carried", "position": [attack.x, attack.y, 0.0], "carrier_seat": 0}
        run_round(agent, attack, bomb, goal=SITES[site]["center"])
        target = SITES[site]["center"]
        final = math.hypot(attack.x - target[0], attack.y - target[1])
        facts["attack"] = {
            "travel_m": round(attack.travel, 1), "idle_ticks": attack.idle,
            "empty_ticks": attack.empty, "final_dist_to_site": round(final, 1),
            "closest_dist_to_site": round(attack.closest[site], 1), "bomb": bomb["state"],
            "move_ticks": attack.move_ticks, "toward_site_ticks": attack.toward_ticks,
        }
        record(
            "attack_moves", attack.travel > 20.0,
            f"travelled {attack.travel:.1f} m; {attack.idle} ticks with no move/fire/use; "
            f"{attack.empty} ticks returned an empty action; ended {final:.1f} m from site {site}. "
            f"Of {FREEZE_TICKS + LIVE_TICKS} ticks, {attack.move_ticks} issued movement and only "
            f"{attack.toward_ticks} moved toward the site. "
            f"Action counts: {top_counts(attack.hist)}.",
        )
        record(
            "plant_gated", attack.plant_outside < 50,
            f"sent use=plant on {attack.plant_outside} ticks while outside every site, "
            f"and {attack.plant_outside_still} of those ticks had no move_forward or move_right. "
            f"A plant sent out of range does nothing and replaces the movement the seat needed.",
        )
        record(
            "attack_reaches_site", attack.closest[site] <= SITES[site]["radius"],
            f"closest approach to site {site} was {attack.closest[site]:.1f} m, radius {SITES[site]['radius']}",
        )
        record("attack_plants", bomb["state"] == "planted", f"bomb state {bomb['state']}")

        defend = Seat("defend", 16000, rifle(), kit=True)
        bomb = {"state": "planted", "position": [24.0, 24.0, 0.0], "carrier_seat": None}
        run_round(agent, defend, bomb, goal=bomb["position"])
        left = math.hypot(defend.x - 24.0, defend.y - 24.0)
        facts["defend"] = {
            "travel_m": round(defend.travel, 1), "idle_ticks": defend.idle,
            "bomb": bomb["state"], "defuse_ticks": defend.defuse_ticks,
            "final_dist_to_bomb": round(left, 1), "toward_bomb_ticks": defend.toward_ticks,
        }
        record(
            "defend_defuses", bomb["state"] == "defused",
            f"bomb state {bomb['state']}; travelled {defend.travel:.1f} m; "
            f"ended {left:.1f} m from the bomb; {defend.defuse_ticks} defuse ticks accumulated; "
            f"{defend.toward_ticks} of {defend.move_ticks} movement ticks went toward the bomb. "
            f"Action counts over the round: {top_counts(defend.hist)}",
        )

        duel = Seat("attack", 16000, rifle())
        duel.x, duel.y, duel.yaw = 0.0, 0.0, 0.0
        enemy = {"seat": 7, "x": 10.0, "y": 0.0, "z": 0.0, "yaw": math.pi, "hp": 100,
                 "alive": True, "parts": ["head", "chest"]}
        bomb = {"state": "dropped", "position": [0.0, -24.0, 0.0], "carrier_seat": None}
        run_round(agent, duel, bomb, enemy=enemy, ticks=FREEZE_TICKS + 400)
        facts["duel"] = {"shots": duel.shots, "hits": duel.hits, "enemy_hp": enemy.get("hp", 0)}
        record("duel_fires", duel.shots > 0 and duel.hits > 0,
               f"{duel.shots} shots, {duel.hits} hits, enemy hp {enemy.get('hp', 0)}")

        dry = Seat("attack", 16000, rifle(ammo=0))
        dry.x, dry.y, dry.yaw = 0.0, 0.0, 0.0
        enemy = {"seat": 7, "x": 10.0, "y": 0.0, "z": 0.0, "yaw": math.pi, "hp": 100,
                 "alive": True, "parts": ["head", "chest"]}
        bomb = {"state": "dropped", "position": [0.0, -24.0, 0.0], "carrier_seat": None}
        run_round(agent, dry, bomb, enemy=enemy, ticks=FREEZE_TICKS + 300)
        facts["reload"] = {"reloads": dry.reloads, "ammo_after": dry.ammo()}
        record("duel_reloads", dry.reloads > 0,
               f"started with 0 rifle ammo, issued {dry.reloads} reload actions, "
               f"ended with {dry.ammo()} ammo")

        spray = Seat("attack", 16000, rifle())
        spray.x, spray.y, spray.yaw = 0.0, 0.0, 0.0
        enemy = {"seat": 7, "x": 18.0, "y": 0.0, "z": 0.0, "yaw": math.pi, "hp": 1000,
                 "alive": True, "parts": ["head", "chest"]}
        bomb = {"state": "dropped", "position": [0.0, -24.0, 0.0], "carrier_seat": None}
        run_round(agent, spray, bomb, enemy=enemy, ticks=FREEZE_TICKS + 600)
        accuracy = spray.hits / spray.shots if spray.shots else 0.0
        facts["aim"] = {"shots": spray.shots, "hits": spray.hits, "accuracy": round(accuracy, 3)}
        record("aim_control", spray.shots >= 12 and accuracy >= 0.6,
               f"{spray.shots} shots, {spray.hits} hits, {accuracy:.0%} accuracy under "
               f"{RECOIL_YAW}/{RECOIL_PITCH} recoil per shot. Holding fire without compensating "
               "drifts off target; burst discipline and recoil compensation keep it on.")

        record("corner_peek", attack.peek_switches >= 2 and attack.peek_out >= 8,
               f"requires >=8 stationary strafe ticks and >=2 direction reversals; observed {attack.peek_out} strafe ticks with no forward movement and "
               f"{attack.peek_switches} direction reversals. A jiggle peek strafes out from cover "
               "to gain line of sight and back again, instead of standing in the open or walking "
               "blind into a corner.")

        # Every seat can become the carrier; the production seat is not fixed at zero.
        for seat_id in range(10):
            carrier = Seat("attack", 16000, rifle(), carrying=True)
            bomb = {"state": "carried", "position": [carrier.x, carrier.y, 0.0],
                    "carrier_seat": seat_id}
            run_round(agent, carrier, bomb, goal=SITES[site]["center"], seat_id=seat_id)
            record(f"carrier_seat_{seat_id}", bomb["state"] == "planted",
                   f"seat {seat_id}: bomb {bomb['state']}, closest {carrier.closest[site]:.2f}m")
            record(f"departure_seat_{seat_id}", getattr(carrier, "early_moves", 0) == 0,
                   f"seat {seat_id}: {getattr(carrier, 'early_moves', 0)} movement ticks before "
                   f"departure {360 + (seat_id % 5) * 90}; intentional waiting must not trigger unstick")

        suppressed = getattr(agent, "_gate_empty_errors", [])
        examples = list(dict.fromkeys(item for group in suppressed for item in group))[:5]
        record("runtime_errors", not suppressed,
               f"{len(suppressed)} live decisions returned empty actions after suppressed exceptions. "
               + "; ".join(examples))
        unknown = sorted(SEEN_KEYS - ACTION_KEYS)
        facts["unknown_keys"] = unknown
        record("action_keys", not unknown,
               f"unsupported keys {unknown}" if unknown else "every action key is in the engine vocabulary")
    except Exception:
        record("runtime", False, traceback.format_exc(limit=4).strip())

    return finish(checks, facts)


def top_counts(hist, limit=7):
    ranked = sorted(hist.items(), key=lambda item: item[1], reverse=True)[:limit]
    return ", ".join(f"{name} x{count}" for name, count in ranked) or "none"


def finish(checks, facts):
    failed = [item for item in checks if not item["ok"]]
    lines = [
        "LOCAL GATE: deterministic fixture, an approximation of the engine, not the engine.",
        f"Fixture map: {len(MAP['nav']['nodes'])} nav nodes, {len(MAP['nav']['edges'])} directed edges, "
        f"{len(MAP['sites'])} sites whose center is an [x,y,z] list. There is no landmark, spawn-name "
        f"or point-of-interest field. A solid wall blocks the straight line from spawn to site A.",
    ]
    for item in checks:
        lines.append(f"{'PASS' if item['ok'] else 'FAIL'} {item['name']}: {item['detail']}")
    lines.append(f"{len(checks) - len(failed)} of {len(checks)} checks passed.")
    print(json.dumps({"ok": not failed, "checks": checks, "facts": facts, "report": "\n".join(lines)}))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())