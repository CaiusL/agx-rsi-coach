"""Experiment coverage and evidence limits; oMLX owns all proposed values."""
from __future__ import annotations
import json
import re
from pathlib import Path

PRIMARY_CAR_FIELDS = ('fuel_load_kg','front_wing','rear_wing')
CAR_FIELDS = PRIMARY_CAR_FIELDS + ('ballast_kg','radiator_opening','ers_harvest_level','pressure_front_psi','pressure_rear_psi')
CONTROL_FIELDS = ('qual_margin','race_margin','preview','latency','ld_min','ld_time','yaw_gain','kv','brake_gain')

def changes(before, after):
    return sorted(k for k in set(before)|set(after) if k not in ('name','thesis') and before.get(k)!=after.get(k))

def coverage(state: dict, folder: Path) -> dict:
    observations=[]
    for path in folder.glob('*.json'):
        try:
            data=json.loads(path.read_text())
        except (OSError,ValueError):continue
        result=data.get('result') or {};facts=(result.get('recording') or {}).get('facts') or {}
        observations.append({'match_id':result.get('match_id'),'book':data.get('played_strategy') or {},
                             'track':facts.get('track_id'),'wet':facts.get('wet_race')})
    fields={}
    for field in CAR_FIELDS+CONTROL_FIELDS:
        values=sorted({row['book'][field] for row in observations if isinstance(row['book'].get(field),(int,float))})
        local_values=sorted({r['candidate'][field] for r in state.get('racing_experiments',[])
                             if field in r.get('changed',[]) and isinstance((r.get('candidate') or {}).get(field),(int,float))})
        fields[field]={'played_values':values,'local_trial_values':local_values,
                       'unexplored':len(values)<2 and not local_values}
    streak=0
    for decision in state.get('decisions',[]):
        changed=set()
        for line in decision.get('changed',[]):
            match=re.match(r'\s*([a-z_]+):',line)
            if match and match.group(1) not in ('name','thesis'):changed.add(match.group(1))
        if changed & set(CAR_FIELDS):streak=0
        elif changed & set(CONTROL_FIELDS):streak+=1
    starved=[field for field in PRIMARY_CAR_FIELDS if fields[field]['unexplored']]
    weather={}
    for row in observations:
        track=row['track'] or 'unknown'
        counts=weather.setdefault(track,{'dry':0,'wet':0,'unknown':0})
        counts['dry' if row['wet'] is False else 'wet' if row['wet'] is True else 'unknown']+=1
    return {'fields':fields,'controller_only_streak':streak,
            'car_exploration_due':streak>=3 and bool(starved),
            'eligible_car_fields':starved,'observed_weather_by_track':weather,
            'note':'Played values and local trials are distinct. Different opponents/controllers/weather confound causal attribution.'}

def coverage_text(ledger: dict) -> str:
    lines=['Racing experiment coverage (live design fields; unchanged does not mean inert):',json.dumps(ledger,ensure_ascii=False)]
    if ledger['car_exploration_due']:
        lines.append('EXPLORATION REQUIREMENT: Recent proposals kept changing controller gains while car fields remained untested. '
                     'For this next experiment choose ONE of '+', '.join(ledger['eligible_car_fields'])+
                     '. oMLX chooses its value inside the legal band; the harness does not pick a setup. '
                     'An untested draft is not proof of improvement in either weather.')
    else:
        lines.append('Consider fuel and both wings alongside controller gains. Explain why the selected lever is more informative than the alternatives.')
    return '\n'.join(lines)

def experiment_problem(raw: dict, candidate: dict, baseline: dict, ledger: dict) -> str:
    experiment=raw.get('experiment') or {}
    if not isinstance(experiment,dict):return 'experiment must be an object'
    changed=changes(baseline,candidate)
    lever='tyre_schedule' if set(changed)<= {'stops','start_compound'} and changed else changed[0] if len(changed)==1 else None
    if experiment.get('lever')!=lever:return 'experiment.lever must name the exact changed field (or tyre_schedule for the coupled stop change).'
    for field in ('why_this_lever','dry_hypothesis','wet_hypothesis','uncertainty'):
        if not isinstance(experiment.get(field),str) or not experiment[field].strip():
            return f'experiment.{field} must be a nonempty string; address BOTH dry and wet with the same book.'
    if experiment.get('simulation_effect_s') is not None:
        return 'The context simulator does not model book effects. simulation_effect_s must be null, not an invented predicted gain.'
    if ledger.get('car_exploration_due') and lever not in ledger['eligible_car_fields']:
        return 'Car exploration is due. Choose one untested live car field: '+', '.join(ledger['eligible_car_fields'])+'. Do not repeat another controller-only trial.'
    return ''
