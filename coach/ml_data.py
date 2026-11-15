"""Derived own-seat features survive raw-log retention; raw observations do not."""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import uuid
import numpy as np

from coach.config import ROOT
from coach.fps_geometry import aim_errors

STORE=ROOT/'state/ml'
SCHEMA=2
FPS_FEATURES=('hp','armor','speed','ammo','primary_equipped','visible_count','nearest_range',
              'aim_error','weapon_ready','blind','near_sound','moving_command','fire_command','recent_damage')
RACING_BOUNDS={
    'fuel_load_kg':(7,14),'front_wing':(18,25),'rear_wing':(18,26),'ballast_kg':(20,40),
    'radiator_opening':(.55,.85),'ers_harvest_level':(4,6),'pressure_front_psi':(27,30),'pressure_rear_psi':(26,30),
    'qual_margin':(.97,1),'race_margin':(.97,1),'preview':(.12,.4),'latency':(.02,.1),
    'ld_min':(6,10),'ld_time':(.25,.45),'yaw_gain':(.5,1.2),'kv':(1,2.2),'brake_gain':(.95,1.15)}
RACING_FEATURES=tuple(RACING_BOUNDS)+('stop_lap','stop_hard','stop_medium','stop_soft','start_soft','start_medium','start_hard')


def save_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False));temporary.replace(path)


def save_npz(path,**arrays):
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('wb') as handle:np.savez_compressed(handle,**arrays)
    temporary.replace(path)


def fps_paths(mid,seat):
    mid=str(uuid.UUID(str(mid)))
    if type(seat) is not int or not 0<=seat<=9:raise ValueError('Invalid own seat')
    base=STORE/'fps/matches'/f'{mid}-seat{seat}'
    return base.with_suffix('.npz'),base.with_suffix('.json')


def fps_saved(summary):
    try:
        data_path,meta_path=fps_paths(summary['match_id'],summary['seat']);meta=json.loads(meta_path.read_text())
        return (data_path.exists() and meta.get('schema')==SCHEMA and
                meta.get('decision_sha256')==summary.get('sha256') and
                hashlib.sha256(data_path.read_bytes()).hexdigest()==meta.get('features_sha256'))
    except (OSError,ValueError,KeyError):return False


def _clip(value,maximum=1.):
    return float(np.clip(value,0,maximum)) if type(value) in (int,float) and math.isfinite(value) else 0.


def fps_features(obs,action,recent_damage):
    me=obs.get('me') or {};visible=[v for v in obs.get('visible') or [] if v.get('alive',True) and 'x' in v and 'y' in v]
    distances=[math.hypot(v['x']-me.get('x',0),v['y']-me.get('y',0)) for v in visible]
    errors=list(aim_errors(me,visible,me.get('yaw',0),me.get('pitch',0)))
    slot=me.get('active_slot');weapons=me.get('weapons') or []
    gun=(weapons[slot] or {}) if type(slot) is int and 0<=slot<len(weapons) else {}
    tick=obs.get('match_tick',0)
    ready=bool((me.get('ammo') or 0)>0 and (gun.get('shot_ready') or 0)<=tick and
               not me.get('reload') and not gun.get('reload_state') and (me.get('switch_until') or 0)<=tick)
    return [_clip((me.get('hp') or 0)/100),_clip((me.get('armor') or 0)/100),
            _clip(math.hypot(me.get('vx') or 0,me.get('vy') or 0)/6),_clip((me.get('ammo') or 0)/40),
            float(slot==0),_clip(len(visible)/5),_clip(min(distances,default=100)/100),
            _clip(min((r[0] for r in errors),default=math.pi)/math.pi),float(ready),float(bool(me.get('blind_ticks'))),
            float(any(isinstance(s,dict) and s.get('distance_band')=='near' for s in obs.get('sounds') or obs.get('heard') or [])),
            _clip(math.hypot(action.get('move_forward') or 0,action.get('move_right') or 0)),
            float(bool(action.get('fire'))),_clip(recent_damage/100)]


def extract_fps(raw,summary):
    if summary.get('status')!='verified':raise ValueError('ML extraction requires a verified own-seat export')
    data_path,meta_path=fps_paths(summary['match_id'],summary['seat'])
    if fps_saved(summary):return json.loads(meta_path.read_text())
    stat=raw.stat()
    if 'file_size' in summary and (summary['file_size'],summary.get('file_mtime_ns'))!=(stat.st_size,stat.st_mtime_ns):
        raise ValueError('Training file changed after verification')
    segments=defaultdict(list);last_sample={};previous=None;calls=0;map_id=None
    with raw.open() as handle:
        for line in handle:
            row=json.loads(line);obs=row.get('observation')
            if not isinstance(obs,dict):continue
            if row.get('seat')!=summary['seat']:raise ValueError('Another seat in own training export')
            calls+=1;me=obs.get('me') or {};map_id=obs.get('map_id') or map_id
            if obs.get('phase')!='live':previous=None;continue
            tick=obs['match_tick'];round_id=obs['round_index'];hp=me.get('hp',0);alive=bool(me.get('alive'))
            if type(hp) not in (int,float) or not math.isfinite(hp):raise ValueError('Invalid HP label')
            recent_damage=max(0,previous[1]-hp) if previous and previous[0]==round_id else 0
            previous=(round_id,hp);feature=None
            if alive and tick-last_sample.get(round_id,-1000)>=30:
                feature=fps_features(obs,row.get('applied_action') or row.get('action') or {},recent_damage)
                last_sample[round_id]=tick
            segments[round_id].append((tick,float(hp),alive,feature))
    if calls!=summary['calls']:raise ValueError('Decision count changed during feature extraction')
    x=[];labels=[];ticks=[];rounds=[];censored=0
    for round_id,records in segments.items():
        times=np.asarray([r[0] for r in records]);health=np.asarray([r[1] if r[2] else 0 for r in records])
        if np.any(np.diff(times)<=0):raise ValueError('Non-monotonic own decision ticks')
        for i,(tick,hp,alive,feature) in enumerate(records):
            if feature is None:continue
            end=int(np.searchsorted(times,tick+60,side='right'));future=health[i+1:end]
            harmed=bool(len(future) and np.min(future)<hp-.1)
            complete=end>i+1 and times[end-1]>=tick+60 and np.max(np.diff(times[i:end]))<=6
            if not harmed and not complete:censored+=1;continue
            x.append(feature);labels.append(int(harmed));ticks.append(tick);rounds.append(round_id)
    features=np.asarray(x,dtype=np.float32).reshape(-1,len(FPS_FEATURES));target=np.asarray(labels,dtype=np.uint8)
    if not np.isfinite(features).all():raise ValueError('Non-finite ML feature')
    save_npz(data_path,x=features,y=target,ticks=np.asarray(ticks,dtype=np.int32),rounds=np.asarray(rounds,dtype=np.int16))
    meta={'schema':SCHEMA,'match_id':summary['match_id'],'seat':summary['seat'],'version_id':summary.get('version_id'),
          'source_hash':summary.get('source_hash'),'decision_sha256':summary['sha256'],'created_at':summary.get('created_at',''),
          'map_id':map_id,'feature_names':FPS_FEATURES,'rows':len(target),'positive_rows':int(target.sum()),
          'censored_rows':censored,'label':'own_hp_loss_or_death_within_next_1s','sampling_hz':2,
          'bytes':data_path.stat().st_size,'features_sha256':hashlib.sha256(data_path.read_bytes()).hexdigest(),
          'limitations':'Own legal observations and own actions only. Future HP supplies the label, never features. '
              'Action/risk associations are observational, not effects of untried actions.'}
    save_json(meta_path,meta);return meta


def racing_features(book):
    values=[]
    for key,(low,high) in RACING_BOUNDS.items():
        value=book.get(key,40 if key=='ballast_kg' else None)
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Missing racing field '+key)
        values.append((value-low)/(high-low))
    stops=book.get('stops') or [];stop=stops[0] if stops else {};lap=stop.get('lap')
    if type(lap) not in (int,float) or not math.isfinite(lap):raise ValueError('Missing stop lap')
    values.append(lap/4)
    values.extend(float(stop.get('compound')==c) for c in ('hard','medium','soft'))
    values.extend(float(book.get('start_compound')==c) for c in ('soft','medium','hard'))
    return np.asarray(values,dtype=float)


def racing_records(folder):
    found={}
    for path in folder.glob('*.json'):
        try:
            data=json.loads(path.read_text());result=data.get('result') or {};facts=(result.get('recording') or {}).get('facts') or {}
            if result.get('status')!='succeeded' or type(facts.get('finished')) is not bool:continue
            mid=str(uuid.UUID(result.get('match_id') or path.stem));book=data.get('played_strategy') or {};vector=racing_features(book)
            if not facts.get('track_id'):continue
            found[mid]={'match_id':mid,'created_at':result.get('created_at',''),'track':facts['track_id'],
                'features':vector.tolist(),'finished':int(facts['finished']),'finish_laps':facts.get('finish_laps'),
                'reported_retirements':facts.get('retirements') or [],'source':'own_played_strategy_and_recorded_result',
                'weather_note':'Realized weather is retained as diagnostics, not an available next-round input.',
                'observed_wet_race':facts.get('wet_race')}
        except (OSError,ValueError,KeyError,TypeError):continue
    records=sorted(found.values(),key=lambda r:(r['created_at'],r['match_id']))
    save_json(STORE/'racing/dataset.json',{'schema':SCHEMA,'feature_names':RACING_FEATURES,'records':records})
    return records
