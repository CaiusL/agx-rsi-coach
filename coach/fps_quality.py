"""One-step control audit fitted to own observations; not a hit or win simulator."""
import math
import statistics
try:
    from .fps_geometry import aim_errors
except ImportError:
    from fps_geometry import aim_errors

VERSION = 2


def angle(value):
    return (value + math.pi) % (2 * math.pi) - math.pi


def fit(rows):
    """Estimate turn response only from adjacent living observations and applied controls."""
    samples = {'yaw': [], 'pitch': []}; intervals = []; previous = None; triggers = {}
    for row in rows:
        obs = row.get('observation')
        if not isinstance(obs, dict): continue
        if previous:
            old = previous['observation']; before = old.get('me') or {}; me = obs.get('me') or {}
            ticks = obs.get('match_tick', 0) - old.get('match_tick', 0)
            if old.get('phase') == 'live' and obs.get('phase') == 'live' and old.get('round_index') == obs.get('round_index') and before.get('alive') and me.get('alive') and 0 < ticks <= 6:
                dt = ticks / 60; intervals.append(dt)
                applied = previous.get('applied_action') or previous.get('action') or {}
                slot=before.get('active_slot'); weapons=before.get('weapons') or []
                held=(weapons[slot] or {}) if type(slot) is int and 0<=slot<len(weapons) else {}
                ready=(held.get('shot_ready') or 0)<=old.get('match_tick',0) and not held.get('reload_state') and not before.get('reload') and (before.get('switch_until') or 0)<=old.get('match_tick',0)
                if applied.get('fire') and ready and before.get('ammo',0)>0 and slot==me.get('active_slot'):
                    counts=triggers.setdefault(held.get('id'),{'held_no_shot':0,'held_shot':0,'released_shot':0})
                    pressed=bool((held.get('trigger_state') or {}).get('pressed'))
                    shot=me.get('ammo',0)<before.get('ammo',0)
                    key='held_shot' if pressed and shot else 'held_no_shot' if pressed else 'released_shot' if shot else None
                    if key:counts[key]+=1
                for axis in samples:
                    command = applied.get(axis + '_rate') or 0
                    if abs(command) < .2: continue
                    delta = me.get(axis, 0) - before.get(axis, 0)
                    if axis == 'yaw': delta = angle(delta)
                    response = delta / (command * dt)
                    # Pitch clamps and recoil transients are not turn-rate identification.
                    if .2 < response < 10: samples[axis].append(response)
        previous = row
    return {'dt': statistics.median(intervals) if intervals else None,
            **{axis: statistics.median(values) if len(values) >= 5 else None for axis, values in samples.items()},
            'samples': {axis: len(values) for axis, values in samples.items()},
            'trigger_evidence':triggers,
            'release_required_weapons':[name for name,c in triggers.items() if name and c['held_no_shot']>=5 and c['held_shot']==0 and c['released_shot']>=2],
            'limitation': 'Effective adjacent-input turn response only; no collision, ballistic, spread or counterfactual damage model.'}


class Audit:
    def __init__(self, calibration):
        self.calibration = calibration
        self.samples = 0; self.error_sum = 0.; self.fire = 0; self.aligned = 0; self.unsafe = 0; self.conflicts = 0;self.trigger_blocked=0

    def step(self, obs, action):
        if not isinstance(action, dict) or obs.get('phase') != 'live': return
        me = obs.get('me') or {}; visible = obs.get('visible') or []; calibration = self.calibration
        if not me.get('alive') or me.get('blind_ticks') or not visible: return
        slot = me.get('active_slot'); weapons = me.get('weapons') or []
        if type(slot) is not int or slot not in (0, 1) or slot >= len(weapons): return
        held = weapons[slot] or {}
        if not held.get('id') or (me.get('ammo') or 0) <= 0: return
        if any(calibration.get(k) is None for k in ('dt', 'yaw', 'pitch')): return
        yaw = me.get('yaw', 0) + calibration['yaw'] * calibration['dt'] * max(-1, min(1, action.get('yaw_rate') or 0))
        pitch = me.get('pitch', 0) + calibration['pitch'] * calibration['dt'] * max(-1, min(1, action.get('pitch_rate') or 0))
        punch = held.get('aim_punch') or {}
        yaw += 2 * (punch.get('yaw') or 0); pitch += 2 * (punch.get('pitch') or 0)
        errors = list(aim_errors(me, visible, yaw, pitch))
        if not errors: return
        error, distance, tolerance, _ = min(errors); self.samples += 1; self.error_sum += error
        if not action.get('fire'): return
        self.fire += 1
        tick = obs.get('match_tick', 0)
        conflict = bool(action.get('reload') or me.get('reload') or held.get('reload_state') or
            (held.get('reload_until') or 0) > tick or (me.get('switch_until') or 0) > tick or
            action.get('weapon_slot', slot) != slot)
        self.conflicts += conflict
        trigger_blocked=bool(held.get('id') in calibration.get('release_required_weapons',[]) and (held.get('trigger_state') or {}).get('pressed'))
        self.trigger_blocked+=trigger_blocked
        ready = not conflict and not trigger_blocked and (held.get('shot_ready') or 0) <= tick
        moving = math.hypot(me.get('vx', 0), me.get('vy', 0)) > .5 or math.hypot(action.get('move_forward') or 0, action.get('move_right') or 0) > .2
        aligned = any(e <= tolerance for e, _, tolerance, _ in errors)
        self.aligned += bool(ready and aligned and not moving)
        self.unsafe += bool(not aligned or moving or conflict or trigger_blocked)

    def summary(self):
        return {'aim_samples': self.samples, 'mean_aim_error_rad': self.error_sum / self.samples if self.samples else None,
                'fire_requests': self.fire, 'aligned_ready_fire': self.aligned, 'unsafe_fire': self.unsafe,
                'conflicting_fire': self.conflicts,'trigger_blocked_fire':self.trigger_blocked,
                'unsafe_fire_fraction': self.unsafe / self.fire if self.fire else None}


def comparison(baseline, candidate, retargeting_validated=False):
    """More fire requests alone never establishes a quality gain."""
    gains = []; regressions = []
    before = baseline.get('mean_aim_error_rad'); after = candidate.get('mean_aim_error_rad')
    if before is not None and after is not None:
        tolerance = max(.001, before * .01)
        if after < before - tolerance: gains.append('aim_control')
        if after > before + tolerance: regressions.append('aim_control')
    # A different body target changes the camera angle required to fire. Fixed
    # inputs never follow that correction. Waive this count only for improved
    # geometry backed by successful closed-loop visible-part/cadence fixtures.
    retargeted = retargeting_validated and before is not None and after is not None and after < before - max(.001, before*.01)
    if candidate['aligned_ready_fire'] < baseline['aligned_ready_fire'] and not retargeted:
        regressions.append('aligned_ready_fire')
    if candidate['conflicting_fire'] > baseline['conflicting_fire']: regressions.append('conflicting_fire')
    # Both policies see the same observations. Dividing by each policy's own
    # fire requests changes the denominator and can reject fewer bad requests
    # with identical useful responses. The percentage is diagnostic only.
    old = baseline.get('unsafe_fire'); new = candidate.get('unsafe_fire')
    if old is not None and new is not None:
        if new > old: regressions.append('unsafe_fire')
        if new < old and candidate['aligned_ready_fire'] >= baseline['aligned_ready_fire']: gains.append('fire_discipline')
    return gains, regressions
