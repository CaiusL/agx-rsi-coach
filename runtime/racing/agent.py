"""METHOD
Category: rules; search
Approach: The official driver and its NPZ stay frozen. strategy.py only
changes the pit stop, the pace margins, and the sealed car book.
Data & training: AGX formula template. Match rank decides the next book.
Changes: First version.
END
"""

from strategy import STRATEGY

import formula_driver as drv
from pathlib import Path
import numpy as np

_driver = None
_weights_dir = None
_track_id = None
_bindings = None
_drivers = {}
_ASSET_RELEASE = "8c22f4ba271a"
_TRACKS = ("parkland", "highland", "crossover")

_CONTROL = (
    "qual_margin",
    "race_margin",
    "preview",
    "latency",
    "ld_min",
    "ld_time",
    "yaw_gain",
    "kv",
    "brake_gain",
)


def _apply(control):
    for key in _CONTROL:
        control[key] = STRATEGY[key]
    stops = []
    for stop in STRATEGY["stops"]:
        item = {"lap": int(stop["lap"]), "compound": stop["compound"]}
        item["request_ahead_m"] = int(stop.get("request_ahead_m", 160))
        stops.append(item)
    control["stops"] = stops
    control["wet_stops"] = [dict(item) for item in stops]


def _idle():
    return {
        "throttle": 0.0,
        "brake": 1.0,
        "steer": 0.0,
        "ers_mode": 0,
        "aero_mode": 0,
        "override": False,
        "aid": False,
        "pit_request": False,
        "pit_compound": None,
    }


def init(context):
    """Initialize asset dispatch; the controller implementation stays frozen."""
    global _driver, _weights_dir, _track_id, _bindings, _drivers
    _driver, _track_id, _drivers = None, None, {}
    _weights_dir = Path(context["weights_dir"])
    with np.load(_weights_dir / "weights" / "bindings.npz", allow_pickle=False) as archive:
        _bindings = archive["points"].copy()
    for track in _TRACKS:
        folder = _weights_dir / "weights" / track
        data = drv.load_plan(folder)
        _apply(data["control"])
        mechanics = drv.load_mechanics(folder, data["plan"]["L"])
        _drivers[track] = drv.Driver(data["plan"], data["control"], mechanics)


def select_track(observation):
    """Bind public world coordinates to one published track; never default to Parkland."""
    car = observation.get("self") or {}
    if "x" not in car or "y" not in car:
        return None
    distances = []
    for index, name in enumerate(_TRACKS):
        points = _bindings[_bindings[:, 0] == index, 1:3]
        distance = float(np.min(np.sum((points - [car["x"], car["y"]]) ** 2, axis=1)))
        distances.append((distance, name))
    distances.sort()
    # Published maps use the same world frame as self.x/y. Ambiguous geometry fails closed.
    if distances[0][0] > 50.0 ** 2 or distances[1][0] - distances[0][0] < 50.0 ** 2:
        return None
    return distances[0][1]


def act(observation):
    """Select numeric assets once, then delegate every action to the unchanged driver."""
    global _driver, _track_id
    try:
        if _driver is None:
            track = select_track(observation)
            if track is None:
                return _idle()
            _driver = _drivers[track]
            _track_id = track
        return _driver.act(observation)
    except Exception:
        return _idle()
