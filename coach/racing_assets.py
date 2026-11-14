"""Validate and package track-bound numeric assets for the unchanged frozen driver."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets' / 'racing' / 'tracks'
TRACKS = ('parkland', 'highland', 'crossover')


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_plan(path: Path, data: dict):
    pools = {'float': [], 'int': [], 'bool': []}
    def pack(value):
        if isinstance(value, dict):
            return ['dict', [[key, pack(v)] for key, v in value.items()]]
        if isinstance(value, list):
            try:
                arr = np.asarray(value)
            except ValueError:
                arr = None
            if arr is not None and arr.size and arr.dtype.kind in 'biuf':
                kind = 'bool' if arr.dtype.kind == 'b' else 'int' if arr.dtype.kind in 'iu' else 'float'
                start = len(pools[kind]); pools[kind].extend(arr.reshape(-1).tolist())
                return ['array', kind, start, list(arr.shape)]
            return ['list', [pack(v) for v in value]]
        return ['literal', value]
    structure = {'format': 'formula-public-plan-v1', 'root': pack(data)}
    np.savez_compressed(path, float=np.asarray(pools['float'], dtype='<f8'),
                        int=np.asarray(pools['int'], dtype='<i8'), bool=np.asarray(pools['bool'], dtype='?'),
                        structure=np.frombuffer(json.dumps(structure).encode(), dtype=np.uint8))


def validate(data, base, mapdata, rows, columns):
    p = data['plan']; old = base['plan']; n = old['n_nodes']; length = float(mapdata['length_m'])
    assert data['control'] == base['control'] and data['design'] == base['design'], 'Controls/design changed'
    assert p['capabilities'] == old['capabilities'], 'Physical capability tables changed'
    assert p['n_nodes'] == n and abs(p['L'] - length) < 1e-7
    assert abs(p['ds'] - length/n) < 1e-7, 'Station spacing mismatches track'
    for key in ('line_x', 'line_y', 'n_line', 'line_ds', 'v_plan', 'deploy', 'xmode'):
        a = np.asarray(p[key]); assert a.shape == (n,) and np.isfinite(a).all(), key
    for key in ('nx', 'ny', 'left', 'right', 'wall_left', 'wall_right'):
        a = np.asarray(p['road'][key]); assert a.shape == (n,) and np.isfinite(a).all(), key
        if key not in ('nx', 'ny'): assert (a > 0).all() and a.max() < 100, 'Road distance is not a width'
    assert np.allclose(np.hypot(p['road']['nx'], p['road']['ny']), 1, atol=1e-5), 'Invalid normals'
    center = rows[:, [columns.index('x_m'), columns.index('y_m')]]
    line = np.column_stack((p['line_x'], p['line_y']))
    for point in line[::50]:
        assert np.linalg.norm(center - point, axis=1).min() < 3.0, 'Geometry does not match target centerline'
    for compound in old['cold_caps']:
        for key in ('temperatures', 'front_stiffness', 'rear_stiffness', 'a', 'b'):
            assert p['cold_caps'][compound][key] == old['cold_caps'][compound][key], key
        for new, prior in ((p['compound_speeds'][compound], old['compound_speeds'][compound]),
                           (p['cold_caps'][compound]['speeds'], old['cold_caps'][compound]['speeds'])):
            a=np.asarray(new); b=np.asarray(prior)
            assert a.shape == b.shape and np.isfinite(a).all() and (a>0).all()
            assert np.all(a.max(axis=-1) <= b.max(axis=-1) + 1e-6), 'Speed ceiling increased'
    for key in ('wet_speeds', 'v_plan'):
        a=np.asarray(p[key]); b=np.asarray(old[key])
        assert a.shape == b.shape and np.isfinite(a).all() and (a>0).all(), key
        assert np.all(a.max(axis=-1) <= b.max(axis=-1) + 1e-6), key + ' ceiling increased'
    assert p['pit_entry_s'] == mapdata['pit']['entry_s'] and p['pit_exit_s'] == mapdata['pit']['exit_s']


def prepare():
    import shutil
    from coach.bundle import ensure_racing_template
    template = ensure_racing_template()
    frozen = module_at('frozen_asset_validation', template/'agent.py')
    builder = module_at('omlx_numeric_asset_builder', ROOT/'assets/racing/build_tracks.py')
    maps = module_at('public_map_decoder', ROOT/'agx-racing-kit/tools/read_map.py')
    base = frozen.load_plan(template)
    ASSETS.mkdir(parents=True, exist_ok=True)
    binding=[]; report={}
    with np.load(template/'mechanics.npz', allow_pickle=False) as a:
        mechanics={k:a[k] for k in a.files}
    for index, track in enumerate(TRACKS):
        mapfile = ROOT/f'agx-racing-kit/data/racing-{track}.mapdata.npz'
        mapdata=maps.load(mapfile)
        with np.load(mapfile, allow_pickle=False) as a:
            rows=a['centerline']; columns=json.loads(a['centerline_columns_utf8'].tobytes())
        folder=ASSETS/track; folder.mkdir(exist_ok=True)
        if track=='parkland':
            shutil.copyfile(template/'plan.npz', folder/'plan.npz')
            shutil.copyfile(template/'mechanics.npz', folder/'mechanics.npz')
        else:
            data=builder.build(copy.deepcopy(base), rows, columns, mapdata)
            validate(data, base, mapdata, rows, columns)
            write_plan(folder/'plan.npz', data)
            # Physical power curves remain frozen. No undocumented aid zones are transplanted.
            adapted={**mechanics,'length':np.asarray([mapdata['length_m']],dtype=float),
                     'zones':np.empty((0,4),dtype=float)}
            np.savez_compressed(folder/'mechanics.npz', **adapted)
        loaded=frozen.load_plan(folder)
        frozen.load_mechanics(folder, loaded['plan']['L'])
        frozen.Driver(loaded['plan'], loaded['control'], frozen.load_mechanics(folder, loaded['plan']['L']))
        for row in rows[::4]:
            binding.append([index,row[columns.index('x_m')],row[columns.index('y_m')]])
        report[track]={'length_m':loaded['plan']['L'], 'points':loaded['plan']['n_nodes'],
                       'plan_sha256':hashlib.sha256((folder/'plan.npz').read_bytes()).hexdigest(),
                       'mechanics_sha256':hashlib.sha256((folder/'mechanics.npz').read_bytes()).hexdigest()}
    np.savez_compressed(ASSETS/'bindings.npz', points=np.asarray(binding,dtype=float))
    manifest={'tracks':report,'frozen_driver_sha256':hashlib.sha256((template/'agent.py').read_bytes()).hexdigest(),
              'builder_sha256':hashlib.sha256((ROOT/'assets/racing/build_tracks.py').read_bytes()).hexdigest(),
              'limitations':'Non-Parkland speed envelopes are model-authored approximations; no engine score. Undocumented aid zones disabled.'}
    manifest['bindings_sha256'] = hashlib.sha256((ASSETS/'bindings.npz').read_bytes()).hexdigest()
    (ASSETS/'manifest.json').write_text(json.dumps(manifest,indent=2))
    release = hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()[:12]
    wrapper = ROOT/'runtime/racing/agent.py'
    source = wrapper.read_text()
    source = re.sub(r'_ASSET_RELEASE = "[^"]+"', f'_ASSET_RELEASE = "{release}"', source)
    wrapper.write_text(source)
    return manifest


def bundled_files():
    manifest=json.loads((ASSETS/'manifest.json').read_text())
    from coach.bundle import ensure_racing_template
    frozen=ensure_racing_template()/'agent.py'
    if hashlib.sha256(frozen.read_bytes()).hexdigest()!=manifest['frozen_driver_sha256']:
        raise ValueError('Frozen driver changed after asset validation')
    if hashlib.sha256((ASSETS/'bindings.npz').read_bytes()).hexdigest()!=manifest['bindings_sha256']:
        raise ValueError('Binding geometry changed after validation')
    files={'weights/bindings.npz': ASSETS/'bindings.npz'}
    for track in TRACKS:
        folder=ASSETS/track
        if hashlib.sha256((folder/'plan.npz').read_bytes()).hexdigest()!=manifest['tracks'][track]['plan_sha256']:
            raise ValueError('Track plan changed after validation')
        if hashlib.sha256((folder/'mechanics.npz').read_bytes()).hexdigest()!=manifest['tracks'][track]['mechanics_sha256']:
            raise ValueError('Track mechanics changed after validation')
        for name in ('plan.npz','mechanics.npz'):
            files[f'weights/{track}/{name}']=folder/name
    return files

if __name__=='__main__':
    print(json.dumps(prepare(),indent=2))
