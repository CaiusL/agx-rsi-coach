"""Local counted-series numbering; platform IDs and trial evidence stay intact."""
from datetime import datetime,timezone


def timestamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Series start requires a timezone')
    return result.astimezone(timezone.utc)


def configure(state,start_at):
    start=timestamp(start_at);identity=start.strftime('counted-%Y%m%dT%H%M%SZ')
    current=state.get('counted_series') or {}
    if current.get('id')==identity:return False
    state['counted_series']={'id':identity,'starts_at':start.isoformat(),'next_version':1,'releases':[]}
    draft=state.get('draft') or {}
    if draft:
        draft['trial_revision']=draft.get('revision');draft['revision']=1
        stamp(state,draft)
    return True


def version(state,identity):
    current=state.get('counted_series') or {}
    return next((r['version'] for r in current.get('releases',[]) if r['draft_id']==identity),current.get('next_version',1))


def stamp(state,draft):
    current=state.get('counted_series') or {}
    if current:
        draft.update(series_id=current['id'],series_version=version(state,draft.get('id')))


def admitted(state,draft):
    current=state.get('counted_series') or {}
    if not current:return
    identity=draft['id'];number=version(state,identity)
    if not any(r['draft_id']==identity for r in current['releases']):
        current['releases'].append({'draft_id':identity,'version':number,'submission_id':state['B'].get('submission_id'),
            'active_from':draft.get('active_from')})
        current['next_version']=number+1
    stamp(state,draft)


def view(state,now=None):
    current=state.get('counted_series') or {}
    if not current:return None
    start=timestamp(current['starts_at']);now=now or datetime.now(timezone.utc)
    rows=[]
    for row in state.get('match_outcomes',[]):
        try:created=timestamp(row.get('created_at') or '')
        except (ValueError,TypeError):continue
        if created>=start and row.get('outcome') in ('win','loss','draw'):rows.append(row)
    rows.sort(key=lambda row:(timestamp(row['created_at']).timestamp(),row['match_id']))
    streak=0
    for row in reversed(rows):
        if row['outcome']!='loss':break
        streak+=1
    return {'id':current['id'],'starts_at':current['starts_at'],'phase':'counted' if now>=start else 'trial',
        'next_version':current['next_version'],'matches':len(rows),'wins':sum(r['outcome']=='win' for r in rows),
        'losses':sum(r['outcome']=='loss' for r in rows),'draws':sum(r['outcome']=='draw' for r in rows),'loss_streak':streak}
