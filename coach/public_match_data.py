"""Shared background cache for AGX public frame, event and scene exports."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time
import uuid

from coach import training

_executors = {}
_pending = {}


def rows(path):
    with path.open('rb') as handle:
        compressed=handle.read(2)==b'\x1f\x8b'
    opener=gzip.open if compressed else open
    with opener(path,'rt',encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                value=json.loads(line)
                if not isinstance(value,dict):raise ValueError('Replay row must be an object')
                yield value


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2));temp.replace(path)


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def read(client,game,focus,store,schema,analyze,background=True):
    if game not in ('fps','racing'):raise ValueError('Unsupported public replay game')
    if focus.get('status')!='succeeded' or type(focus.get('seat')) is not int:return None
    mid=str(uuid.UUID(focus['match_id']));seat=focus['seat']
    if not 0<=seat<=9:raise ValueError('Invalid own seat')
    from coach.match_retention import archived
    history=archived(game,mid,cache_root=Path(store).parent.parent/'training')
    if history:return {'schema':schema,'status':'summarized','match_id':mid,'history_path':str(history)}
    path=Path(store)/mid/f'seat{seat}.summary.json';key=(game,mid,seat)
    try:cached=json.loads(path.read_text())
    except (OSError,ValueError):cached={}
    future=_pending.get(key)
    if future is not None and future.done():
        _pending.pop(key,None)
        return future.result()
    if cached.get('schema')==schema and cached.get('status')=='verified':
        raw,_=training.paths(game,mid,seat)
        if cached.get('own_observed') or not raw.exists():return cached
    if cached.get('retry_at',0)>time.time():return cached
    normalized={**focus,'match_id':mid}
    if not background:return collect(client,game,normalized,path,schema,analyze)
    if future is None:
        if game not in _executors:_executors[game]=ThreadPoolExecutor(max_workers=1,thread_name_prefix=game+'-public-export')
        _pending[key]=_executors[game].submit(collect,client,game,normalized,path,schema,analyze)
    return {'schema':schema,'status':'preparing','match_id':mid,'background':True}


def collect(client,game,focus,summary_path,schema,analyze):
    mid=focus['match_id'];folder=summary_path.parent;folder.mkdir(parents=True,exist_ok=True)
    try:
        files={}
        for kind in ('events','static','frames'):
            path=folder/(kind+'.download')
            if not path.exists():
                temp=path.with_suffix('.part')
                status,_headers=client.download(f'/api/v1/python-submissions/recordings/{mid}/{kind}',{},temp,timeout=180)
                if status!=200:
                    temp.unlink(missing_ok=True)
                    raise ValueError(f'{kind} export not ready (HTTP {status})')
                temp.replace(path)
            files[kind]=path
        result=analyze(files,focus)
        result.update(schema=schema,status='verified',game=game,match_id=mid,seat=focus['seat'],
                      created_at=focus.get('created_at'),analyzed_at=datetime.now(timezone.utc).isoformat(),
                      source_files={k:{'path':str(p),'bytes':p.stat().st_size,'sha256':sha(p)} for k,p in files.items()})
        save(summary_path,result);return result
    except Exception as exc:
        result={'schema':schema,'status':'unavailable','game':game,'match_id':mid,'error':str(exc)[:300],'retry_at':time.time()+120}
        save(summary_path,result);return result
