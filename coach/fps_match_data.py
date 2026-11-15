"""FPS post-match analysis; public truth never becomes a live-agent input."""
from collections import Counter
import math
from coach import training, public_match_data
from coach.config import ROOT
from coach.fps_combat import summarize as combat
from coach.public_match_data import rows

SCHEMA = 1
STORE = ROOT / 'state/recordings/fps'


def read(client, focus, background=True):
    return public_match_data.read(client,'fps',focus,STORE,SCHEMA,analyze,background)


def analyze(files, focus):
    events=list(rows(files['events'])); scene=list(rows(files['static']))
    ends=[e for e in events if e.get('type')=='match_end']
    if len(ends)!=1: raise ValueError('Public export lacks a unique match_end')
    own=focus['seat']; players=[]
    for seat in range(10):
        facts=combat(events,seat,seat//5)
        shots=[e for e in events if e.get('type')=='fire' and e.get('player')==seat]
        hits=[e for e in events if e.get('type')=='hit' and e.get('player')==seat]
        players.append({'seat':seat,'kills':facts['our_kills'],'deaths':facts['our_deaths'],
            'assists':facts['our_assists'],'hp_damage':facts['our_hp_damage'],
            'opening_kills':facts['our_opening_kills'],'opening_deaths':facts['our_opening_deaths'],
            'deaths_without_firing':facts['our_deaths_without_firing'], 'shots':len(shots),'hit_events':len(hits),
            'head_hit_events':sum(e.get('part')=='head' for e in hits),
            'fired_weapons':dict(Counter(e.get('weapon') for e in shots)),
            'purchases':dict(Counter(e.get('kind') for e in events if e.get('type')=='buy' and e.get('seat')==seat))})
    top=max(players,key=lambda p:p['kills'])['seat']
    motion={seat:Counter() for seat in range(10)}; previous=None; frames=0; first_tick=last_tick=None
    incoming=sorted([e for e in events if e.get('type')=='hit' and e.get('target')==own],key=lambda e:e['match_tick'])
    cursor=0; poses=[]; sampled_rounds=set()
    for frame in rows(files['frames']):
        tick=frame.get('match_tick'); idx=frame.get('round_index')
        if not isinstance(tick,(int,float)) or not isinstance(frame.get('players'),list): raise ValueError('Invalid FPS frame')
        if first_tick is None:first_tick=tick
        if last_tick is not None and tick<last_tick:raise ValueError('Non-monotonic public frame stream')
        last_tick=tick; frames+=1
        seats={p['seat']:p for p in frame['players']}
        while cursor<len(incoming) and incoming[cursor]['match_tick']<=tick:
            event=incoming[cursor]; cursor+=1
            if event['round_index'] in sampled_rounds:continue
            snap=previous if previous and previous['round_index']==event['round_index'] else frame
            ps={p['seat']:p for p in snap['players']}; me=ps.get(own); enemy=ps.get(event.get('player'))
            if not me or not enemy:continue
            sampled_rounds.add(event['round_index'])
            angle=abs((math.atan2(-(enemy['y']-me['y']),enemy['x']-me['x'])-me['yaw']+math.pi)%(2*math.pi)-math.pi)
            poses.append({'round':event['round_index']+1,'attacker':event['player'],
                'bearing_from_view_deg':round(math.degrees(angle),1),
                'distance_m':round(math.dist([me[k] for k in ('x','y','z')],[enemy[k] for k in ('x','y','z')]),1),
                'our_weapon':me.get('weapon_id'),'their_weapon':enemy.get('weapon_id'),
                'snapshot_offset_ticks':event['match_tick']-snap['match_tick']})
        if previous and idx==previous['round_index'] and frame.get('phase')==previous.get('phase')=='live':
            dt=(tick-previous['match_tick'])/60; old={p['seat']:p for p in previous['players']}
            if 0<dt<=.2:
                for seat,p in seats.items():
                    before=old.get(seat)
                    if not before or not before.get('alive') or not p.get('alive'):continue
                    dist=math.hypot(p['x']-before['x'],p['y']-before['y']); stats=motion[seat]
                    stats['alive_seconds']+=dt;stats['path_m']+=dist;stats['stationary_seconds']+=dt*(dist<.005)
        previous=frame
    if not frames or first_tick!=0 or last_tick<ends[0]['match_tick']:
        raise ValueError('Incomplete public frame stream')
    map_row=next((r.get('map') for r in scene if isinstance(r.get('map'),dict)),{})
    result={'round_wins':ends[0].get('round_wins'),'match_winner':ends[0].get('match_winner'),
        'map':{'id':map_row.get('id'),'sites':map_row.get('sites')},'frame_rows':frames,
        'players':players,'our_seat':own,'top_killer_seat':top,
        'motion':{str(s):{k:round(v,2) for k,v in stats.items()} for s,stats in motion.items()},
        'first_damage_public_snapshots':poses,
        'limitations':'Public replay is post-match truth, not what any opponent observed or decided. '
            'Bearing and weapon correlations do not prove causality. Hit events per shot are not an accuracy probability. '
            'Opponent source, private decisions and assists without explicit attribution remain unknown.'}
    raw,summary=training.paths('fps',focus['match_id'],own)
    if raw.exists():
        verified=training.summarize(raw,'fps',focus['match_id'],own)
        first_visible={};live_rounds=set();sound_calls=0;buy_wallets={}
        for row in rows(raw):
            obs=row.get('observation')
            if not isinstance(obs,dict):continue
            me=obs.get('me') or {}; idx=obs.get('round_index')
            if obs.get('phase')=='buy':buy_wallets.setdefault(idx,me.get('cash'))
            if obs.get('phase')!='live' or not me.get('alive'):continue
            live_rounds.add(idx);sound_calls+=bool(obs.get('sounds') or obs.get('heard'))
            if obs.get('visible'):first_visible.setdefault(idx,obs['match_tick'])
        deaths=[e for e in events if e.get('type')=='kill' and e.get('target')==own]
        result['own_observed']={'status':verified['status'],'version_id':verified['version_id'],
            'source_hash':verified['source_hash'],'calls':verified['calls'],'counts':verified['counts'],
            'motion':verified['behavior']['recorded_motion'],'sound_calls':sound_calls,
            'deaths_without_visible_contact':sum(e['round_index'] not in first_visible or e['match_tick']<first_visible[e['round_index']] for e in deaths),
            'rounds_damaged_before_visible_contact':len({e['round_index'] for e in incoming if e['round_index'] not in first_visible or e['match_tick']<first_visible[e['round_index']]}),
            'round_start_cash':{str(k+1):v for k,v in buy_wallets.items()}}
    return result
