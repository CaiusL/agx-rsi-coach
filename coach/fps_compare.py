"""Isolated paired replay on real inputs. Scores responses, never counterfactual wins."""
import copy
import importlib.util
import json
import statistics
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from fps_signals import Contexts, Responses, action_key, SCORE_KEYS
from fps_quality import Audit, fit, comparison as quality_comparison


def load(folder,name):
    sys.path.insert(0,str(folder));sys.modules.pop('strategy',None)
    book_spec=importlib.util.spec_from_file_location('strategy',folder/'strategy.py')
    book=importlib.util.module_from_spec(book_spec);book_spec.loader.exec_module(book);sys.modules['strategy']=book
    spec=importlib.util.spec_from_file_location(name,folder/'agent.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module._comparison_strategy=book
    spec=importlib.util.spec_from_file_location('monitor',Path(__file__).with_name('gate_fps.py'))
    monitor=importlib.util.module_from_spec(spec);spec.loader.exec_module(monitor);monitor.monitor_empty_errors(module)
    module._comparison_action_error=monitor.action_error
    return module


def compare(base,candidate,traces,goal='balanced',holdout=()):
    policies=[load(base,'baseline'),load(candidate,'candidate')]
    total=changes=0;errors=[0,0];facts=[]
    scores=[{k:0 for k in SCORE_KEYS} for _ in policies]
    calibrations={}
    for trace in traces:
        with trace.open() as calibration_stream:
            calibrations[trace.name]=fit(json.loads(line) for line in calibration_stream)
    # Sparse traces may lack pitch motion. Transfer only mechanics measured in
    # development matches under this same contract, recording that assumption.
    for trace in traces:
        calibration=calibrations[trace.name];calibration['transferred_axes']=[]
        for axis in ('dt','yaw','pitch'):
            if calibration.get(axis) is not None:continue
            known=[c[axis] for name,c in calibrations.items() if name not in holdout and c.get(axis) is not None and axis not in c.get('transferred_axes',[])]
            if known:
                calibration[axis]=statistics.median(known);calibration['transferred_axes'].append(axis)
    for trace in traces:
        contexts=Contexts();responses=[Responses(),Responses()];calls=changed=0;combat_examples=[]
        calibration=calibrations[trace.name]
        audits=[Audit(calibration),Audit(calibration)]
        with trace.open() as stream:
            for line in stream:
                row=json.loads(line)
                if row.get('type')=='metadata':
                    p=row['participants'][0]
                    for folder,policy in zip((base,candidate),policies):
                        if policy._comparison_strategy is not None:sys.modules['strategy']=policy._comparison_strategy
                        policy.init({'contract_id':row['rules_version'],'seat':p['seat'],'role':p.get('role',0),'seed':row.get('seed',1),'weights_dir':str(folder)})
                if not isinstance(row.get('observation'),dict):continue
                context=contexts.step(row);actions=[]
                for i,policy in enumerate(policies):
                    try:
                        if policy._comparison_strategy is not None:sys.modules['strategy']=policy._comparison_strategy
                        action=policy.act(copy.deepcopy(row['observation']))
                        if policy._comparison_action_error(action):
                            errors[i]+=1;action={}
                    except Exception as exc:
                        action={'error':type(exc).__name__};errors[i]+=1
                    responses[i].step(action,context);audits[i].step(row['observation'],action);actions.append(action)
                if context['blocked'] is not None and contexts.examples:
                    example=contexts.examples[-1]
                    if example.get('episode')==context['blocked'] and 'baseline_next_waypoint' not in example:
                        memory=getattr(policies[0],'MEM',{})
                        route=memory.get('route') or []
                        if route:
                            example['baseline_next_waypoint']=route[0]
                            example['baseline_goal']=memory.get('goal')
                changed+=action_key(actions[0],row['observation']) != action_key(actions[1],row['observation']);calls+=1
                if context['ready_window'] is not None and len(combat_examples)<3 and not actions[0].get('fire') and not any(e['window']==context['ready_window'] for e in combat_examples):
                    combat_examples.append({'step':row.get('step'),'window':context['ready_window'],'weapon':context['weapon'],
                        'distance_m':context['distance_m'],'baseline_action':actions[0],'candidate_action':actions[1],
                        'note':'Visible, aligned, loaded, stationary, unblinded and recorded cooldown ready. A response opportunity, not a guaranteed hit.'})
        for i,policy in enumerate(policies):errors[i]+=len(getattr(policy,'_gate_empty_errors',[]));policy._gate_empty_errors=[]
        summary=[r.summary() for r in responses]
        for i in range(2):
            for key in SCORE_KEYS:scores[i][key]+=summary[i][key]
        quality=[a.summary() for a in audits]
        quality_gains,quality_regressions=quality_comparison(*quality)
        facts.append({'trace':trace.name,'split':'holdout' if trace.name in holdout else 'development',
            'calls':calls,'changed_actions':changed,'calibration':calibration,
            'quality':{'baseline':quality[0],'candidate':quality[1],'gains':quality_gains,'regressions':quality_regressions},
            'opportunities':{k:len(v) for k,v in contexts.opportunities.items()},'baseline':summary[0],'candidate':summary[1],
            'blocked_examples':contexts.examples,'combat_examples':combat_examples})
        total+=calls;changes+=changed
    # Engagement-count proxies remain diagnostic. More firing is not a promotion criterion.
    protected=('blocked_responses','plant_responses','defuse_responses')
    regressions=[f'{trace["trace"]}:{k}' for trace in facts for k in protected
                 if trace['candidate'][k]<trace['baseline'][k]]
    gains=[k for k in protected if scores[1][k]>scores[0][k]]
    gains+=sorted({k for trace in facts if trace['split']=='development' for k in trace['quality']['gains']})
    regressions += [f'{trace["trace"]}:{k}' for trace in facts for k in trace['quality']['regressions']]
    if errors[0]>errors[1]:gains.append('runtime_errors')
    reasons=[]
    if not total:reasons.append('No verified inputs')
    if errors[1]:reasons.append('Candidate runtime errors')
    if not changes:reasons.append('No demonstrated behavior change: identical actions on the evaluation corpus')
    if regressions:reasons.append('Response regressions: '+', '.join(regressions))
    if not gains:reasons.append('No measured gain in captured navigation/objective/engagement response checks')
    if goal in ('combat_kda','quality') and not any(k in gains for k in ('aim_control','fire_discipline')):
        reasons.append('Combat-focused revision needs improved aim control or fire discipline; extra fire commands do not count')
    if goal in ('combat_kda','quality') and not any(t['quality']['candidate']['aim_samples'] for t in facts):
        reasons.append('Insufficient calibrated combat observations; quality is unknown')
    if goal in ('combat_kda','quality') and not any(t['quality']['candidate']['aligned_ready_fire'] for t in facts):
        reasons.append('No aligned ready stationary firing response demonstrated on the corpus')
    if holdout and not any(t['quality']['candidate']['aim_samples'] for t in facts if t['split']=='holdout'):
        reasons.append('No calibrated combat coverage in held-out matches')
    blocked_total=sum(t['opportunities']['blocked_responses'] for t in facts)
    if blocked_total>=3 and scores[0]['blocked_responses']<blocked_total*.5 and scores[1]['blocked_responses']<=scores[0]['blocked_responses'] and not (errors[0]>errors[1]):
        reasons.append('Captured blocked-motion bottleneck did not improve')
    return {'ok':not reasons,'goal':goal,'validation_stage':'offline_experiment','match_validated':False,
        'calls':total,'changed_actions':changes,'runtime_errors':errors,
        'baseline':scores[0],'candidate':scores[1],'gains':gains,'regressions':regressions,'reasons':reasons,'traces':facts,
        'limitation':'Fixed-input response and calibrated one-step angular control audits only. No changed-world rollout, raycast, damage, escape, survival or win-rate estimate.'}


if __name__=='__main__':
    try:
        goal='combat_kda' if '--combat-kda' in sys.argv else 'quality' if '--quality' in sys.argv else 'balanced'
        holdout=[p.split('=',1)[1] for p in sys.argv[3:] if p.startswith('--holdout=')]
        result=compare(Path(sys.argv[1]),Path(sys.argv[2]),[Path(p) for p in sys.argv[3:] if not p.startswith('--')],goal,holdout)
    except Exception as exc:result={'ok':False,'reasons':[f'{type(exc).__name__}: {exc}']}
    print(json.dumps(result))
