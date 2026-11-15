"""Race replay diagnostics with qualifying and race phases kept separate."""
from collections import Counter, deque
import math

from coach import public_match_data, recording, training
from coach.config import ROOT
from coach.public_match_data import rows

SCHEMA = 2
STORE = ROOT/'state/recordings/racing'


def read(client,focus,background=True):
    return public_match_data.read(client,'racing',focus,STORE,SCHEMA,analyze,background)


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def analyze(files,focus):
    events=list(rows(files['events']));scene=list(rows(files['static']))
    initial=next((r.get('metadata') for r in scene if r.get('type')=='initial_metadata'),None)
    terminal=next((r.get('metadata') for r in scene if r.get('type')=='terminal_metadata'),None)
    closed=[e for e in events if e.get('kind')=='race_closed']
    if not initial or not terminal or len(closed)!=1:raise ValueError('Incomplete racing scene/events')
    if initial.get('game_id')!='racing':raise ValueError('Public scene belongs to another game')
    own=focus['seat'];states={};weather={};previous=None;first_tick=last_tick=None;frames=0
    retirements=[e for e in events if e.get('kind')=='retired' and own in (e.get('seats') or [])]
    incident_events=[e for e in events if e.get('kind')=='incident' and own in (e.get('seats') or [])]
    history=deque(maxlen=61);retirement_windows=[];pending=iter(sorted(retirements,key=lambda e:e['tick']));next_retire=next(pending,None)
    for frame in rows(files['frames']):
        tick=frame.get('tick');phase=frame.get('phase');stamp=frame.get('time')
        if frame.get('game_id')!='racing' or not number(tick) or not number(stamp) or not isinstance(frame.get('cars'),list):
            raise ValueError('Invalid racing frame')
        if first_tick is None:first_tick=tick
        if last_tick is not None and tick<last_tick:raise ValueError('Non-monotonic racing frames')
        last_tick=tick;frames+=1;wet=(frame.get('weather') or {}).get('track_wetness')
        condition=(frame.get('weather') or {}).get('condition') or 'unknown'
        phase_weather=weather.setdefault(phase,{'start':condition,'end':condition,'peak_wetness':None,'observed_conditions':set()})
        phase_weather['end']=condition;phase_weather['observed_conditions'].add(condition)
        if number(wet):phase_weather['peak_wetness']=max(phase_weather['peak_wetness'] or 0,wet)
        dt=stamp-previous['time'] if previous and previous['phase']==phase else 0
        old={c['seat']:c for c in previous['cars']} if previous else {}
        for car in frame['cars']:
            seat=car['seat'];key=(seat,phase)
            state=states.setdefault(key,{'active_seconds':0.,'speed_time':0.,'stationary_seconds':0.,
                'peak_speed_mps':None,'fuel_min_kg':None,'fuel_max_kg':None,'ers_min_mj':None,
                'peak_abs_g_lat':None,'max_tyre_wear':None,'compounds':set(),'weather_seconds':Counter(),
                'last_status':None,'last_lap':None})
            state['last_status']=car.get('status');state['last_lap']=car.get('lap')
            if car.get('status')=='racing':
                for source,target,op in [('speed_mps','peak_speed_mps',max),('fuel_kg','fuel_min_kg',min),
                    ('fuel_kg','fuel_max_kg',max),('ers_soc_mj','ers_min_mj',min)]:
                    value=car.get(source)
                    if number(value):state[target]=value if state[target] is None else op(state[target],value)
                if number(car.get('g_lat')):state['peak_abs_g_lat']=max(state['peak_abs_g_lat'] or 0,abs(car['g_lat']))
                wear=[v for v in car.get('tyre_wear') or [] if number(v)]
                if wear:state['max_tyre_wear']=max(state['max_tyre_wear'] or 0,max(wear))
                if car.get('compound'):state['compounds'].add(car['compound'])
                if 0<dt<=.2 and (old.get(seat) or {}).get('status')=='racing' and number(car.get('speed_mps')):
                    state['active_seconds']+=dt;state['speed_time']+=dt*car['speed_mps']
                    state['stationary_seconds']+=dt*(car['speed_mps']<.5 and not car.get('in_pit'))
                    state['weather_seconds'][condition]+=dt
            if seat==own:
                history.append({'tick':tick,'phase':phase,'time_s':stamp,'wetness':wet,
                    **{k:car.get(k) for k in ('status','lap','s','speed_mps','fuel_kg','compound','tyre_wear','g_lat','steer','throttle','brake','in_pit')}})
        while next_retire is not None and next_retire['tick']<=tick:
            before=[r for r in history if r['phase']==next_retire.get('phase') and r['tick']<=next_retire['tick']]
            samples=[before[i] for i in sorted({0,len(before)//2,len(before)-1})] if before else []
            retirement_windows.append({'phase':next_retire.get('phase'),'tick':next_retire['tick'],
                'reported_reason':(next_retire.get('payload') or {}).get('reason'),
                'reported_lap':(next_retire.get('payload') or {}).get('lap'),'preceding_public_samples':samples})
            next_retire=next(pending,None)
        previous=frame
    if not frames or first_tick!=0 or last_tick<closed[0]['tick']:raise ValueError('Incomplete racing frame stream')
    cars=initial.get('cars') or [];seats=sorted({c['seat'] for c in cars})
    if own not in seats:raise ValueError('Own seat missing from racing scene')
    results={str(seat):recording._racing(events,seat) for seat in seats}
    finishers=[e for e in events if e.get('kind')=='finished' and e.get('phase')=='race' and (e.get('payload') or {}).get('position') is not None]
    winner=min(finishers,key=lambda e:e['payload']['position'],default=None)
    summaries={}
    for (seat,phase),state in states.items():
        active=state.pop('active_seconds');speed_time=state.pop('speed_time')
        summaries.setdefault(str(seat),{})[phase]={
            **{k:round(v,4) if number(v) else v for k,v in state.items() if k not in ('compounds','weather_seconds')},
            'active_seconds':round(active,3),'mean_active_speed_mps':round(speed_time/active,3) if active else None,
            'compounds':sorted(state['compounds']),'weather_seconds':{k:round(v,3) for k,v in state['weather_seconds'].items()}}
    result={'track':{k:(initial.get('track') or {}).get(k) for k in ('id','length_m')},
        'rules_version':initial.get('rules_version'),'frame_rows':frames,'first_tick':first_tick,'last_tick':last_tick,
        'our_seat':own,'winner_seat':(winner.get('seats') or [None])[0] if winner else None,
        'our_result':results.get(str(own)),'field_results':results,'telemetry_by_seat_and_phase':summaries,
        'weather_by_phase':{p:{**v,'observed_conditions':sorted(v['observed_conditions'])} for p,v in weather.items()},
        'public_designs':recording.static_cars(None,None,initial),'retirement_windows':retirement_windows,
        'our_incidents':[{'tick':e['tick'],'phase':e.get('phase'),'payload':e.get('payload')} for e in incident_events],
        'limitations':'Published replay telemetry is post-match evidence, not live inputs or opponent controller code. '
            'Qualifying and race conditions are separate. Mean speed and field setups are confounded by traffic, '
            'pit stops, controller behavior and weather; they do not prove causal effects of fuel or wings. '
            'A reported damage retirement is a measured failure mode, not proof of its mechanical cause.'}
    raw,_=training.paths('racing',focus['match_id'],own)
    if raw.exists():
        verified=training.summarize(raw,'racing',focus['match_id'],own)
        result['own_observed']={k:verified[k] for k in ('status','version_id','source_hash','calls','counts','phases')}
        controls={};previous_row=None;tail=deque(maxlen=3)
        for row in rows(raw):
            obs=row.get('observation')
            if not isinstance(obs,dict):continue
            phase=obs.get('phase');me=obs.get('self') or {};action=row.get('applied_action') or row.get('action') or {}
            stats=controls.setdefault(phase,{'max_abs_lateral_offset_m':None,'max_abs_heading_error_rad':None,
                                            'max_steer_command_step':0.,'sliding_calls':0})
            for source,target in [('n','max_abs_lateral_offset_m'),('heading_error','max_abs_heading_error_rad')]:
                value=me.get(source)
                if number(value):stats[target]=max(stats[target] or 0,abs(value))
            stats['sliding_calls']+=bool(me.get('front_sliding') or me.get('rear_sliding'))
            if previous_row and previous_row['phase']==phase and number(action.get('steer')) and number(previous_row['steer']):
                stats['max_steer_command_step']=max(stats['max_steer_command_step'],abs(action['steer']-previous_row['steer']))
            previous_row={'phase':phase,'steer':action.get('steer')}
            tail.append({'tick':obs.get('tick'),'phase':phase,'weather':obs.get('weather'),
                'self':{k:me.get(k) for k in ('lap','s','n','heading_error','speed_mps','fuel_kg','front_sliding','rear_sliding')},
                'own_action':{k:action.get(k) for k in ('steer','throttle','brake')}})
        result['own_observed'].update(phase_controls=controls,last_recorded_decisions=list(tail))
    return result
