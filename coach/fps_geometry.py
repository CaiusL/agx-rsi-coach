"""Published body sample geometry; angular checks do not establish ray visibility."""
import math

HEIGHTS = {"head": 1.64, "chest": 1.18, "abdomen": .88}
RADII = {"head": .12, "chest": .25, "abdomen": .25}


def targets(me, enemy):
    """Yield only reported visible parts, adjusted for both players' stances."""
    if not enemy.get('alive', True) or any(k not in enemy for k in ('x', 'y', 'z')):
        return
    dx, dy = enemy['x'] - me.get('x', 0), enemy['y'] - me.get('y', 0)
    distance = math.hypot(dx, dy)
    eye = .95 if me.get('crouch') else 1.62
    scale = .6 if enemy.get('crouch') else 1.
    # Older sparse observations omit parts: use known body samples, flagging no
    # visibility claim. An explicitly empty list provides no shootable sample.
    for part in enemy.get('parts', HEIGHTS):
        if part not in HEIGHTS:
            continue
        dz = enemy['z'] + HEIGHTS[part] * scale - me.get('z', 0) - eye
        yield (math.atan2(-dy, dx), math.atan2(dz, max(.01, distance)),
               distance, RADII[part] * scale, part)


def aim_errors(me, visible, yaw, pitch):
    for enemy in visible:
        for want_yaw, want_pitch, distance, radius, part in targets(me, enemy):
            dyaw = (want_yaw - yaw + math.pi) % (2 * math.pi) - math.pi
            error = math.hypot(dyaw, want_pitch - pitch)
            yield error, distance, math.atan2(radius, max(.01, distance)), part
