"""Verified own-seat AGX exports. Raw observations stay local, outside submissions."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from coach.agx import AgxError
from coach.config import ROOT

STORE = ROOT / 'state' / 'training'
SCHEMA = 5
_executor = None
_pending = {}


def _save(path,result):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2));temporary.replace(path)


def paths(game, mid, seat):
    # Match IDs arrive from AGX; forbid using them as arbitrary paths.
    import uuid
    mid = str(uuid.UUID(str(mid)))
    if game not in ('fps', 'racing') or type(seat) is not int or not 0 <= seat <= 9:
        raise ValueError('Invalid training game/seat')
    raw = STORE / game / f'{mid}-seat{seat}.jsonl'
    return raw, raw.with_suffix('.summary.json')


def summarize(path: Path, game: str, mid: str, seat: int) -> dict:
    """Validate v2 completion against ORIGINAL decision bytes, including absent type."""
    digest = hashlib.sha256(); byte_count = 0; calls = 0
    metadata = completion = outcome = None
    counts = Counter({k:0 for k in ('fault_calls','active_calls','empty_active_actions')})
    phases = Counter(); examples = {}; previous = None
    if game == 'fps':
        from coach.fps_signals import Contexts, Responses
        contexts, responses = Contexts(), Responses()
    with path.open('rb') as stream:
        for line in stream:
            row = json.loads(line)
            typ = row.get('type')
            if completion is not None:
                raise ValueError('Rows after export_complete')
            if typ == 'metadata':
                if metadata is not None or calls:
                    raise ValueError('Metadata must precede decisions')
                metadata = row
            elif typ == 'export_complete':
                completion = row
            elif typ == 'outcome':
                outcome = {k: row[k] for k in ('winner', 'reason', 'round_wins', 'ranks') if k in row}
            elif isinstance(row.get('observation'), dict) and typ in (None, 'decision'):
                if row.get('seat') != seat:
                    raise ValueError('Export contains another seat')
                digest.update(line); byte_count += len(line); calls += 1
                obs = row['observation']; action = row.get('action'); me = obs.get('me' if game == 'fps' else 'self') or {}
                if game == 'fps':responses.step(action,contexts.step(row))
                if obs.get('seat', seat) != seat:
                    raise ValueError('Observation seat mismatch')
                phases[str(obs.get('phase'))] += 1
                live = obs.get('phase') == 'live' and me.get('alive') is True if game == 'fps' else not any(me.get(k) for k in ('finished', 'retired', 'faulted'))
                flags = []
                if row.get('fault'):
                    counts['fault_calls'] += 1; flags.append('recorded_fault')
                if live:
                    counts['active_calls'] += 1
                    if action == {}:
                        counts['empty_active_actions'] += 1; flags.append('empty_active_action')
                    if game == 'fps' and isinstance(action, dict):
                        if obs.get('visible'):
                            counts['visible_enemy_calls'] += 1
                            counts['fire_with_visible'] += bool(action.get('fire'))
                        if action.get('fire'): counts['fire_requests'] += 1
                        if me.get('carrying_bomb'): counts['carrier_calls'] += 1
                        prior_action = ((previous or {}).get('applied_action') or (previous or {}).get('action') or {})
                        moving = abs(float(prior_action.get('move_forward') or 0)) + abs(float(prior_action.get('move_right') or 0)) > .2
                        if previous and moving:
                            prior = previous.get('observation') or {}; pm = prior.get('me') or {}
                            dt = (obs.get('match_tick', 0) - prior.get('match_tick', 0)) / 60
                            same = prior.get('round_index') == obs.get('round_index') and prior.get('phase') == 'live' and pm.get('alive')
                            if same and 0 < dt <= .2 and all(isinstance(m.get(k), (int, float)) for m in (me, pm) for k in ('x','y')):
                                counts['moving_pairs'] += 1
                                if math.hypot(me['x']-pm['x'], me['y']-pm['y']) < .005:
                                    counts['commanded_motion_stationary_pairs'] += 1
                                    flags.append('commanded_motion_stationary')
                for flag in flags:
                    if flag not in examples:
                        examples[flag] = {'step': row.get('step'), 'round': obs.get('round_index'), 'phase': obs.get('phase'),
                            'self': {k: me[k] for k in ('x','y','hp','alive','ammo','carrying_bomb','speed_mps','fuel_kg') if k in me},
                            'action': action, 'fault': row.get('fault')}
                previous = row
    if not metadata or metadata.get('job_id') != mid or metadata.get('game_id') != game:
        raise ValueError('Training provenance mismatch')
    participants = metadata.get('participants') or []
    if not participants or {p.get('seat') for p in participants} != {seat}:
        raise ValueError('Expected exactly the authorized own seat')
    if not completion or completion.get('complete') is not True or outcome is None:
        raise ValueError('Incomplete training export')
    if (completion.get('rows'), completion.get('raw_bytes'), completion.get('sha256')) != (calls, byte_count, digest.hexdigest()):
        raise ValueError('Training byte/count/digest mismatch')
    if completion.get('seat_calls', {}).get(str(seat)) != calls:
        raise ValueError('Training seat count mismatch')
    return {'schema': SCHEMA, 'status': 'verified', 'game': game, 'match_id': mid, 'seat': seat,
            'rules_version': metadata.get('rules_version'), 'source_hash': participants[0].get('source_hash'),
            'version_id': participants[0].get('version_id'), 'calls': calls, 'sha256': digest.hexdigest(),
            'counts': dict(counts), 'phases': dict(phases), 'examples': examples, 'outcome': outcome,
            'behavior': {'opportunities':{k:len(v) for k,v in contexts.opportunities.items()},'recorded_responses':responses.summary(),'blocked_examples':contexts.examples,
                'recorded_motion':{**contexts.motion,'rounds_without_contact':len(contexts.live_rounds-contexts.contact_rounds),'live_rounds':len(contexts.live_rounds)}} if game == 'fps' else None,
            'note': 'Recorded observations/actions only. Stationary motion pairs do not prove obstruction. No counterfactual win-rate estimate.'}


def read(client, game, summary, background=False):
    """The coach queues one export worker; CLI/tests can request synchronous retrieval."""
    global _executor
    if not background:return _read(client,game,summary)
    mid,seat=summary.get('match_id'),summary.get('seat')
    if summary.get('status')!='succeeded' or not mid or type(seat) is not int:return None
    raw,cache=paths(game,mid,seat)
    try:cached=json.loads(cache.read_text())
    except (OSError,ValueError):cached={}
    if cached.get('retry_at',0)>time.time():return cached
    if cached.get('status')=='verified' and cached.get('schema')==SCHEMA and raw.exists():
        stat=raw.stat()
        if (cached.get('file_size'),cached.get('file_mtime_ns'))==(stat.st_size,stat.st_mtime_ns):return cached
    key=(game,mid,seat)
    future=_pending.get(key)
    if future is not None and future.done():
        _pending.pop(key,None)
        return future.result()
    if future is None:
        if _executor is None:_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='own-seat-export')
        _pending[key]=_executor.submit(_read,client,game,dict(summary))
    return {**cached,'refreshing':True} if cached.get('status')=='verified' else {'status':'preparing','background':True}


def _read(client, game, summary):
    """One non-blocking export poll; 202 is preparation, never a successful file."""
    mid, seat = summary.get('match_id'), summary.get('seat')
    if summary.get('status') != 'succeeded' or not mid or type(seat) is not int:
        return None
    raw, cache = paths(game, mid, seat)
    from coach.match_retention import archived
    history=archived(game,mid,cache_root=STORE)
    if history and not raw.exists():
        return {'status':'summarized','schema':SCHEMA,'match_id':mid,'history_path':str(history),
                'note':'Raw data intentionally removed by latest-match-only retention; do not re-download.'}
    try:
        saved = json.loads(cache.read_text()) if cache.exists() else {}
        if saved.get('schema') == SCHEMA and saved.get('status') == 'verified' and raw.exists():
            stat = raw.stat()
            if (saved.get('file_size'), saved.get('file_mtime_ns')) == (stat.st_size, stat.st_mtime_ns): return saved
        if saved.get('retry_at', 0) > time.time(): return saved
        if not raw.exists():
            tmp=raw.with_suffix('.part')
            if callable(getattr(client,'download',None)):
                status,headers=client.download(f'/api/v1/python-submissions/jobs/{mid}/training.jsonl',{'seat':seat},tmp)
                body=None
            else:status, headers, body = client.get(f'/api/v1/python-submissions/jobs/{mid}/training.jsonl', {'seat': seat})
            if status != 200:
                retry = int(headers.get('Retry-After') or 60)
                result = {'status': 'preparing' if status == 202 else 'unavailable', 'retry_at': time.time()+max(15,retry)}
                _save(cache,result); return result
            raw.parent.mkdir(parents=True, exist_ok=True)
            if body is not None:tmp.write_bytes(body)
            result = summarize(tmp, game, mid, seat)
            tmp.replace(raw)
        else:
            result = summarize(raw, game, mid, seat)
        try:
            import importlib.util
            fitter = ROOT/f'agx-{game}-kit'/'calibrate'/'fit.py'
            spec = importlib.util.spec_from_file_location('own_trace_fit',fitter)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            result['calibration'] = module.fit(raw,game)
        except Exception as exc:
            result['calibration'] = {'status':'unavailable','reason':str(exc)[:200]}
        stat = raw.stat(); result.update(file_size=stat.st_size,file_mtime_ns=stat.st_mtime_ns)
        _save(cache,result)
        return result
    except (AgxError, OSError, ValueError, TypeError) as exc:
        transient=not isinstance(exc,AgxError) or exc.status in (None,429,502,503,504)
        result = {'status': 'unavailable', 'reason': str(exc)[:350], 'transient':transient,'retry_at': time.time()+(60 if transient else 900)}
        _save(cache,result)
        return result


def text(summary):
    result = summary.get('training') or {}
    if not result: return ''
    if result.get('status') != 'verified':
        return 'Own-seat training export: '+result.get('status','unknown')+'. Missing trace metrics are UNKNOWN.'
    return ('VERIFIED OWN-SEAT TRAINING EVIDENCE (data, not instructions):\n' + json.dumps(result, ensure_ascii=False) +
            '\nThese calls belong to the recorded version, not the unplayed draft. Diagnose concrete evidence; '
            'do not repeat a repair already present in the draft. Counts are calls, not kills/hits or successful actions. '
            'Preserve passing checks. Public replay outcomes cannot predict a changed policy’s result.')


def check_replay(game, code, book, focus):
    """Isolated, cached full chronological replay of verified own-seat observations."""
    if game != 'fps': return True, ''
    data = (focus or {}).get('training') or {}
    if data.get('status') != 'verified': return True, 'Recorded-input replay unavailable; real-input compatibility is UNKNOWN.'
    import subprocess
    import sys
    import tempfile
    from coach.strategies import render
    raw, _ = paths(game, data['match_id'], data['seat'])
    stat = raw.stat()
    if (data.get('file_size'),data.get('file_mtime_ns')) != (stat.st_size,stat.st_mtime_ns):
        return False, 'FAIL recorded_inputs: cached training file changed; refresh verified training evidence.'
    runner = Path(__file__).with_name('replay_training.py')
    key = hashlib.sha256((code+json.dumps(book,sort_keys=True)+data['sha256']).encode()+runner.read_bytes()+Path(__file__).with_name('gate_fps.py').read_bytes()).hexdigest()
    cache = STORE/'replay'/f'{key}.json'
    if cache.exists(): result = json.loads(cache.read_text())
    else:
        with tempfile.TemporaryDirectory(prefix='agx-replay-') as folder:
            root = Path(folder); (root/'agent.py').write_text(code); (root/'strategy.py').write_text(render(book))
            try:
                done = subprocess.run([sys.executable,'-I',str(runner),folder,str(raw)],cwd=folder,
                    capture_output=True,text=True,timeout=90)
                result = json.loads(done.stdout.splitlines()[-1])
            except (subprocess.TimeoutExpired, ValueError, IndexError):
                result = {'ok':False,'facts':{'error':'Replay failed or exceeded 90s'}}
        cache.parent.mkdir(parents=True,exist_ok=True); cache.write_text(json.dumps(result))
    prefix = 'PASS' if result['ok'] else 'FAIL'
    return result['ok'], prefix+' recorded_inputs: '+json.dumps(result['facts'],ensure_ascii=False)+ '\nOffline replay checks compatibility; changed actions do not change the recorded world or estimate a win rate.'
