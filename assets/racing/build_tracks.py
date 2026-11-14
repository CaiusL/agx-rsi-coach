import copy
import numpy as np

def build(base, rows, columns, mapdata):
    plan = copy.deepcopy(base['plan'])
    control = copy.deepcopy(base['control'])
    design = copy.deepcopy(base['design'])
    n = plan['n_nodes']
    L_old = plan['L']
    ds_old = plan['ds']
    xy_old = np.column_stack([plan['line_x'], plan['line_y']])
    delta = np.roll(xy_old, -1, axis=0) - xy_old
    seg = np.linalg.norm(delta, axis=1)
    headings = np.arctan2(delta[:, 1], delta[:, 0])
    h_diff = np.arctan2(np.sin(headings - np.roll(headings, 1)), np.cos(headings - np.roll(headings, 1)))
    orig_curv = h_diff / ((seg + np.roll(seg, 1)) / 2)
    length = mapdata['length_m']
    s_m = rows[:, columns.index('s_m')]
    x_m = rows[:, columns.index('x_m')]
    y_m = rows[:, columns.index('y_m')]
    h_rad = rows[:, columns.index('heading_rad')]
    hw_l = rows[:, columns.index('half_width_left_m')]
    hw_r = rows[:, columns.index('half_width_right_m')]
    s_mod = s_m % length
    order = np.argsort(s_mod)
    s_mod = s_mod[order]
    x_mod = x_m[order]
    y_mod = y_m[order]
    h_mod = h_rad[order]
    hw_l_mod = hw_l[order]
    hw_r_mod = hw_r[order]
    unique_mask = np.diff(np.concatenate([s_mod, [s_mod[0] + length]])) > 1e-9
    s_mod = s_mod[unique_mask]
    x_mod = x_mod[unique_mask]
    y_mod = y_mod[unique_mask]
    h_mod = h_mod[unique_mask]
    hw_l_mod = hw_l_mod[unique_mask]
    hw_r_mod = hw_r_mod[unique_mask]
    stations = np.arange(n) * length / n
    def interp_periodic(vals):
        return np.interp(stations, s_mod, vals, period=length)
    new_x = interp_periodic(x_mod)
    new_y = interp_periodic(y_mod)
    sin_h = interp_periodic(np.sin(h_mod))
    cos_h = interp_periodic(np.cos(h_mod))
    new_headings = np.arctan2(sin_h, cos_h)
    new_hw_l = interp_periodic(hw_l_mod)
    new_hw_r = interp_periodic(hw_r_mod)
    new_xy = np.column_stack([new_x, new_y])
    new_seg = np.linalg.norm(np.roll(new_xy, -1, axis=0) - new_xy, axis=1)
    new_delta = np.roll(new_xy, -1, axis=0) - new_xy
    new_headings2 = np.arctan2(new_delta[:, 1], new_delta[:, 0])
    new_h_diff = np.arctan2(np.sin(new_headings2 - np.roll(new_headings2, 1)), np.cos(new_headings2 - np.roll(new_headings2, 1)))
    new_curv = new_h_diff / ((new_seg + np.roll(new_seg, 1)) / 2)
    plan['L'] = length
    plan['ds'] = length / n
    plan['line_x'] = new_x.tolist()
    plan['line_y'] = new_y.tolist()
    plan['line_ds'] = new_seg.tolist()
    plan['n_line'] = [0.0] * n
    road = plan['road']
    road['nx'] = (-np.sin(new_headings)).tolist()
    road['ny'] = (np.cos(new_headings)).tolist()
    road['left'] = new_hw_l.tolist()
    road['right'] = new_hw_r.tolist()
    road['wall_left'] = new_hw_l.tolist()
    road['wall_right'] = new_hw_r.tolist()
    plan['track_id'] = mapdata['track_id']
    pit = mapdata['pit']
    plan['pit_entry_s'] = pit['entry_s']
    plan['pit_exit_s'] = pit['exit_s']
    plan['pit_speed_mps'] = pit['speed_limit_kph'] / 3.6
    def transform_profiles(arr):
        arr = np.asarray(arr, dtype=float)
        orig_shape = arr.shape
        flat = arr.reshape(-1, n)
        out = np.zeros_like(flat)
        for i in range(flat.shape[0]):
            v = flat[i].copy()
            mask = np.abs(orig_curv) > np.percentile(np.abs(orig_curv), 75)
            if np.sum(mask) < 2:
                mask = np.abs(orig_curv) > 1e-9
            if np.sum(mask) < 2:
                raise ValueError("Insufficient points in segment to compute speed profile")
            ay = np.percentile(v[mask] ** 2 * np.abs(orig_curv)[mask], 25)
            dv2 = (np.roll(v, -1) ** 2 - v ** 2) / (2 * seg)
            pos_acc = dv2[dv2 > 0]
            if len(pos_acc) == 0 or not np.all(np.isfinite(pos_acc)):
                raise ValueError("Inference failed for acceleration")
            accel = np.percentile(pos_acc, 50)
            dv2_brake = -(np.roll(v, -1) ** 2 - v ** 2) / (2 * seg)
            pos_brake = dv2_brake[dv2_brake > 0]
            if len(pos_brake) == 0 or not np.all(np.isfinite(pos_brake)):
                raise ValueError("Inference failed for braking")
            brake = np.percentile(pos_brake, 50)
            new_v = np.minimum(float(np.max(v)), np.sqrt(ay / np.maximum(np.abs(new_curv), 1e-9)))
            for _ in range(50):
                old_v = new_v.copy()
                for j in range(n):
                    i_prev = (j - 1) % n
                    new_v[j] = min(new_v[j], np.sqrt(new_v[i_prev] ** 2 + 2 * accel * new_seg[i_prev]))
                for i_idx in reversed(range(n)):
                    j_next = (i_idx + 1) % n
                    new_v[i_idx] = min(new_v[i_idx], np.sqrt(new_v[j_next] ** 2 + 2 * brake * new_seg[i_idx]))
                if np.max(np.abs(new_v - old_v)) < 1e-7:
                    break
            out[i] = new_v
        return out.reshape(orig_shape)
    plan['v_plan'] = transform_profiles(plan['v_plan']).tolist()
    if 'wet_speeds' in plan:
        plan['wet_speeds'] = transform_profiles(plan['wet_speeds']).tolist()
    if 'compound_speeds' in plan:
        for comp in plan['compound_speeds']:
            plan['compound_speeds'][comp] = transform_profiles(plan['compound_speeds'][comp]).tolist()
    if 'cold_caps' in plan:
        for comp in plan['cold_caps']:
            plan['cold_caps'][comp]['speeds'] = transform_profiles(plan['cold_caps'][comp]['speeds']).tolist()
    return {'plan': plan, 'control': control, 'design': design}
