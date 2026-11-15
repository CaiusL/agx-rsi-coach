"""Learning adapter for the supplied FPS GridDuel, preserving its transition rules."""
from collections import OrderedDict, deque
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from coach.config import ROOT
from coach.sim import _kit

ACTIONS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
RADIUS = 5
FEATURES = tuple(f'free_{x}_{y}' for x in range(-RADIUS, RADIUS+1) for y in range(-RADIUS, RADIUS+1)) + (
    'goal_dx', 'goal_dy', 'goal_range', 'enemy_visible', 'visible_enemy_dx', 'visible_enemy_dy',
    'previous_stay', 'previous_east', 'previous_west', 'previous_north', 'previous_south')


class MapWorld:
    def __init__(self, path):
        self.path = Path(path)
        self.identity = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.name = self.path.stem.removeprefix('fps-').removesuffix('.mapdata')
        self.data = json.loads(self.path.read_text())
        self.grid = self.data['walkable_surface_candidates']
        self.free = {(int(x), int(y)) for x, y, z in self.grid['cells']}
        self.cells = sorted(self.free)
        self.adjacency = {p: [(p[0]+dx, p[1]+dy) for dx, dy in ACTIONS[1:]
                              if (p[0]+dx, p[1]+dy) in self.free] for p in self.cells}
        self.polygons = [row['polygon_xy'] for row in self.data['wall_cover_contours']]
        self.boxes = np.asarray([[min(p[0] for p in poly), min(p[1] for p in poly),
                                  max(p[0] for p in poly), max(p[1] for p in poly)] for poly in self.polygons])
        self._distances = OrderedDict()

    def distances(self, goal):
        if goal not in self._distances:
            distances = {goal: 0}; queue = deque([goal])
            while queue:
                here = queue.popleft()
                for there in self.adjacency[here]:
                    if there not in distances:
                        distances[there] = distances[here]+1; queue.append(there)
            self._distances[goal] = distances
            if len(self._distances) > 8:
                self._distances.popitem(last=False)
        self._distances.move_to_end(goal)
        return self._distances[goal]

    def contours(self, a, b):
        """Exact broad phase: discarded contours cannot intersect the sight segment."""
        if not len(self.polygons): return []
        low = np.minimum(a, b); high = np.maximum(a, b)
        mask = ((self.boxes[:, :2] <= high).all(axis=1) & (self.boxes[:, 2:] >= low).all(axis=1))
        return [self.polygons[i] for i in np.flatnonzero(mask)]

    def episode(self, rng, parameters):
        kit = _kit('fps')
        for _ in range(50):
            goal = self.cells[int(rng.integers(len(self.cells)))]
            distances = self.distances(goal)
            candidates = [p for p, d in distances.items() if 8 <= d <= 32]
            if candidates: break
        else: raise ValueError(f'No usable connected local routes in {self.name}')
        start = candidates[int(rng.integers(len(candidates)))]
        enemy = candidates[int(rng.integers(len(candidates)))]
        spacing = self.grid['cell_size_m']; origin = self.grid['origin_xy']
        points = [[origin[i]+(p[i]+.5)*spacing for i in (0, 1)] for p in (start, enemy)]
        duel = kit.GridDuel(self.grid, points, [], parameters['move_speed_mps']*rng.uniform(.85, 1.15),
                            parameters['reaction_seconds'])
        duel.free = self.free
        return duel, goal, distances


def mask(world, cell, goal):
    # This task is goal-directed navigation: staying is reserved for arrival or isolation.
    legal = np.asarray([(cell[0]+dx, cell[1]+dy) in world.free for dx, dy in ACTIONS])
    legal[0] = cell == goal or not legal[1:].any()
    return legal


def teacher(world, cell, goal, distances):
    values = np.asarray([distances.get((cell[0]+dx, cell[1]+dy), 10**9) for dx, dy in ACTIONS])
    legal = mask(world, cell, goal); best = np.min(values[legal])
    target = (legal & (values == best)).astype(np.float32)
    return target/target.sum()


def features(world, duel, goal, visible, previous):
    cell = duel.positions[0]; enemy = duel.positions[1]
    patch = [float((cell[0]+x, cell[1]+y) in world.free)
             for x in range(-RADIUS, RADIUS+1) for y in range(-RADIUS, RADIUS+1)]
    dx, dy = goal[0]-cell[0], goal[1]-cell[1]
    target = [np.clip(dx/32, -2, 2), np.clip(dy/32, -2, 2), min(math.hypot(dx, dy)/64, 2)]
    observed_enemy = [float(visible), np.clip((enemy[0]-cell[0])/32, -2, 2) if visible else 0.,
                      np.clip((enemy[1]-cell[1])/32, -2, 2) if visible else 0.]
    return np.asarray(patch+target+observed_enemy+[float(previous == i) for i in range(5)], dtype=np.float32)


def step(world, duel, actions):
    # Supply exactly the contours potentially intersecting the kit's next sight line.
    planned = [(p[0]+a[0], p[1]+a[1]) for p, a in zip(duel.positions, actions)]
    planned = [q if q in world.free else p for p, q in zip(duel.positions, planned)]
    points = [duel.point(p) for p in planned]
    duel.polygons = world.contours(*points)
    return duel.step(actions)


def rollouts(world, parameters, seed, episodes=32, horizon=96, policy=None, record=False, episode_offset=0, map_index=0):
    rng = np.random.default_rng(seed); kit = _kit('fps')
    keys = ('x', 'target', 'action', 'reward', 'next_x', 'done', 'episode', 'priority', 'map_index')
    data = {k: [] for k in keys}; success = steps_total = stalls = exposure = 0; returns = []
    for episode in range(episodes):
        duel, goal, distances = world.episode(rng, parameters)
        previous = 0; reward_sum = 0.
        points = [duel.point(p) for p in duel.positions]
        visible = kit.visible(*points, world.contours(*points))
        for tick in range(horizon):
            x = features(world, duel, goal, visible, previous)
            target = teacher(world, duel.positions[0], goal, distances)
            if policy is None: action = int(np.argmax(target))
            else:
                logits = np.asarray(policy(x[None, :]))[0]
                if logits.shape != (5,) or not np.isfinite(logits).all():raise ValueError('Invalid navigation policy logits')
                action = int(np.argmax(np.where(mask(world, duel.positions[0], goal), logits, -np.inf)))
            opponent = int(np.argmax(teacher(world, duel.positions[1], goal, distances)))
            before = duel.positions[0]; old_distance = distances[before]
            result = step(world, duel, [ACTIONS[action], ACTIONS[opponent]])
            visible = result['visible']; arrived = duel.positions[0] == goal
            progress = old_distance-distances[duel.positions[0]]
            stalled = before == duel.positions[0] and not arrived
            reward = float(progress-.05-1.*stalled+5.*arrived)
            reward_sum += reward; stalls += stalled; exposure += bool(visible); steps_total += 1
            done = arrived or tick == horizon-1
            if record:
                next_x = features(world, duel, goal, visible, action)
                priority = 1.+3.*(target[action] == 0)+2.*stalled+2.*(progress <= 0)
                for key, value in zip(keys, (x, target, action, reward, next_x, done, episode_offset+episode, priority, map_index)):
                    data[key].append(value)
            previous = action
            if done: break
        success += arrived; returns.append(reward_sum)
    arrays = None
    if record:
        arrays = {k: np.asarray(v, dtype=np.float32 if k in ('x','target','reward','next_x','priority') else np.int64)
                  for k, v in data.items()}
    return {'map': world.name, 'episodes': episodes, 'arrivals': int(success), 'arrival_rate': success/episodes,
            'mean_return': float(np.mean(returns)), 'mean_steps': steps_total/episodes,
            'stationary_steps': int(stalls), 'visible_fraction': exposure/max(1, steps_total)}, arrays


def maps():
    return [MapWorld(p) for p in sorted((ROOT/'agx-fps-kit/data').glob('fps-*.mapdata.json'))]
