"""Game-scoped manual requests shared by the UI and the single coach."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import uuid
from coach.config import ROOT

STATE = ROOT / 'state'
GAMES = ('racing', 'fps')

@contextmanager
def guard():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE/'manual_queue.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)

def read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return None

def audit(event, req, **extra):
    row={'at':datetime.now(timezone.utc).isoformat(),'event':event,
         'game':req.get('game'),'action':req.get('action','improve'),'request_id':req.get('request_id'),**extra}
    with (STATE/'manual_requests.jsonl').open('a') as handle:
        handle.write(json.dumps(row)+'\n')

def running(game):
    return read(STATE/f'manual_running_{game}.json')

def enqueue(game, upload=False, request_id=None, action="improve", draft_id=None):
    if game not in GAMES: raise ValueError('game must be racing or fps')
    if action not in ('improve','upload','session_start','session_stop'): raise ValueError('invalid manual action')
    with guard():
        for name in (f'manual_running_{game}.json', f'manual_improve_{game}.json'):
            existing=read(STATE/name)
            if existing:
                if existing.get('action','improve') != action or (action == 'upload' and existing.get('draft_id') != draft_id):
                    raise ValueError('Another action is already queued or running for this game')
                return {**existing,'duplicate':True}
        req={'game':game,'action':action,'upload':action=='upload','draft_id':draft_id,'request_id':request_id or uuid.uuid4().hex,
             'at':datetime.now(timezone.utc).isoformat()}
        path=STATE/f'manual_improve_{game}.json';tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(req));tmp.replace(path)
        audit('queued',req)
        return req

def claim(game):
    with guard():
        flag=STATE/f'manual_improve_{game}.json'
        req=read(flag)
        if req is None: return None
        if req.get('game') != game:
            audit('rejected',req,expected_game=game,reason='game does not match queue')
            flag.unlink(missing_ok=True)
            return None
        req.setdefault('request_id',uuid.uuid4().hex)
        req.setdefault('action','improve')
        req['upload'] = req['action'] == 'upload'
        req['pid']=os.getpid()
        path=STATE/f'manual_running_{game}.json'
        path.write_text(json.dumps(req));flag.unlink()
        audit('started',req)
        return req

def finish(req, error=None):
    with guard():
        audit('error' if error else 'finished',req,**({'error':str(error)} if error else {}))
        path=STATE/f"manual_running_{req['game']}.json"
        current=read(path)
        if current and current.get('request_id')==req.get('request_id'):
            path.unlink(missing_ok=True)


def recover_interrupted():
    """An old request must not leave its button disabled after a coach restart."""
    with guard():
        for game in GAMES:
            path=STATE/f'manual_running_{game}.json';req=read(path)
            if not req:continue
            try:os.kill(int(req.get('pid',0)),0)
            except (ProcessLookupError, ValueError, TypeError):
                audit('error',req,error='interrupted by coach restart')
                path.unlink(missing_ok=True)
