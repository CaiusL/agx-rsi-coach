"""Retain one raw match per game; summarize before deleting older cache files."""
import json
from pathlib import Path
import uuid

from coach.config import ROOT

POLICY = ROOT/'state/retention.json'
TRAINING = ROOT/'state/training'
RECORDINGS = ROOT/'state/recordings'
LOGS = ROOT/'state/logs'
HISTORY = ROOT/'state/match-history'


def latest_only(cache_root=None):
    policy=POLICY if cache_root is None or Path(cache_root)==TRAINING else Path(cache_root).parent/'retention.json'
    try: return json.loads(policy.read_text()).get('mode')=='latest_per_game'
    except (OSError,ValueError): return False


def _read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def _read_markdown(path):
    try:
        raw=path.read_text().split('```json\n',1)[1].split('\n```',1)[0]
        return json.loads(raw)
    except (OSError,ValueError,IndexError):return {}


def archived(game,mid,cache_root=None):
    mid=str(uuid.UUID(mid))
    history=HISTORY if cache_root is None or Path(cache_root)==TRAINING else Path(cache_root).parent/'match-history'
    path=history/game/(mid+'.md')
    return path if latest_only(cache_root) and path.exists() else None


def _created(game,mid):
    log=_read(LOGS/game/(mid+'.json'))
    return (log.get('result') or {}).get('created_at') or log.get('created_at') or ''


def prune(game):
    if game not in ('fps','racing'):raise ValueError('Invalid game')
    if not latest_only():return {'status':'off'}
    candidates=[]
    for path in (TRAINING/game).glob('*.summary.json'):
        summary=_read(path)
        if summary.get('status')!='verified':continue
        mid=str(uuid.UUID(summary['match_id']))
        raw=path.with_name(path.name.replace('.summary.json','.jsonl'))
        if raw.exists():
            stat=raw.stat()
            if ('file_size' in summary and summary['file_size']!=stat.st_size or
                    'file_mtime_ns' in summary and summary['file_mtime_ns']!=stat.st_mtime_ns):
                continue
            candidates.append((_created(game,mid),stat.st_mtime,mid,path,raw,summary))
    if not candidates:return {'status':'waiting_for_verified_latest'}
    # Known match chronology takes precedence over download time.
    newest=max(candidates,key=lambda row:(bool(row[0]),row[0],row[1]))
    keep=newest[2]; removed_bytes=0;removed=[];pending_capture=0
    older_ids={row[2] for row in candidates if row[2]!=keep}
    # Also compact public caches left after an earlier raw-training cleanup.
    if (RECORDINGS/game).exists():
        older_ids.update(p.name for p in (RECORDINGS/game).iterdir() if p.is_dir() and p.name!=keep and
                         _created(game,p.name) and newest[0] and _created(game,p.name)<newest[0])
    for mid in sorted(older_ids):
        mid=str(uuid.UUID(mid))
        if game=='fps':
            from coach import hybrid_ml, ml_data
            if hybrid_ml.enabled() and TRAINING==ml_data.STORE.parent/'training':
                pending=False
                for row in candidates:
                    if row[2]==mid and not ml_data.fps_saved(row[5]):
                        hybrid_ml.queue_capture(row[4],row[5]);pending=True
                if pending:
                    pending_capture+=1
                    continue  # Preserve raw inputs until compact training data is durable.
        training_summaries=[row[5] for row in candidates if row[2]==mid]
        folder=RECORDINGS/game/mid
        public=[_read(p) for p in folder.glob('seat*.summary.json')]
        log=_read(LOGS/game/(mid+'.json')); result=log.get('result') or {}
        record={'match_id':mid,'game':game,'created_at':_created(game,mid),
                'result':{k:result.get(k) for k in ('seat','team','rank','round_wins','status')},
                'combat':((result.get('recording') or {}).get('facts') or {}),
                'training_summaries':training_summaries,'public_analysis':public,
                'retention_note':'Raw match files were removed after this summary was saved. '
                    'Aggregates cannot replace historical input replay or held-out validation.'}
        target=HISTORY/game/(mid+'.md');target.parent.mkdir(parents=True,exist_ok=True)
        previous=_read_markdown(target)
        # A late public worker may recreate an old cache after its raw training
        # was pruned. Preserve the already archived facts during that cleanup.
        if previous:
            if not training_summaries:record['training_summaries']=previous.get('training_summaries',[])
            prior_public=previous.get('public_analysis',[])
            if not public or (any(p.get('status')=='verified' for p in prior_public) and
                              not any(p.get('status')=='verified' for p in public)):
                record['public_analysis']=prior_public
            if not record['combat']:record['combat']=previous.get('combat',{})
        if training_summaries or public or not target.exists():
            text=(f'# {game.upper()} match {mid}\n\n'
                  f'[AGX match](https://agx.cool/#/matches/{mid})\n\n'
                  'Raw data removed under the latest-match-only retention policy. '
                  'This is a historical summary, not a replayable training trace.\n\n'
                  '```json\n'+json.dumps(record,ensure_ascii=False,indent=2)+'\n```\n')
            temp=target.with_suffix('.tmp');temp.write_text(text);temp.replace(target)
        # Verify the durable summary before deleting any raw source.
        if not target.exists() or mid not in target.read_text():raise ValueError('History summary was not saved')
        for row in candidates:
            if row[2]==mid:
                for path in (row[3],row[4]):
                    removed_bytes+=path.stat().st_size;path.unlink();removed.append(str(path))
        for path in folder.glob('*'):
            if path.is_file():removed_bytes+=path.stat().st_size;path.unlink();removed.append(str(path))
        if folder.exists() and not any(folder.iterdir()):folder.rmdir()
    return {'status':'retained_latest','game':game,'retained_match':keep,'removed_bytes':removed_bytes,
            'removed_files':len(removed),'pending_ml_capture':pending_capture,'history_directory':str(HISTORY/game)}


def recent(game,limit=5):
    records=[]
    for path in (HISTORY/game).glob('*.md'):
        try:
            row=_read_markdown(path)
            records.append({'match_id':row['match_id'],'created_at':row.get('created_at'),
                'result':row.get('result'),'combat':row.get('combat'),
                'training':[{'source_hash':t.get('source_hash'),'version_id':t.get('version_id'),
                             'counts':t.get('counts'),'behavior':t.get('behavior')} for t in row.get('training_summaries',[])]})
        except (OSError,ValueError,IndexError,KeyError):continue
    return sorted(records,key=lambda r:r.get('created_at') or '',reverse=True)[:limit]
