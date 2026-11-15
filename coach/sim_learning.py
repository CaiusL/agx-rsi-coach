"""Simulation-only aim-parameter learning. No map, damage or win simulation."""
import json
import math
import numpy as np

from coach import ml_data

NAMES=('yaw_gain','pitch_gain','fire_tolerance_scale')
BASELINE=np.asarray([1/.15,1/.1,1.])
LOW=np.asarray([2.,2.,.4]);HIGH=np.asarray([12.,18.,1.25])


def turn_calibration():
    from coach import training
    from coach.fps_quality import fit
    candidates=[]
    for path in (training.STORE/'fps').glob('*.summary.json'):
        try:
            meta=json.loads(path.read_text());raw,_=training.paths('fps',meta['match_id'],meta['seat']);stat=raw.stat()
            if meta.get('status')=='verified' and (meta.get('file_size'),meta.get('file_mtime_ns'))==(stat.st_size,stat.st_mtime_ns):
                candidates.append((stat.st_mtime,raw,meta))
        except (OSError,ValueError,KeyError):continue
    if candidates:
        _,raw,meta=max(candidates)
        with raw.open() as handle:result=fit(json.loads(line) for line in handle)
        if all(result.get(k) is not None for k in ('dt','yaw','pitch')):
            return {k:result[k] for k in ('dt','yaw','pitch','samples')} | {'source_match':meta['match_id'],'observed_turn_response':True}
    return {'dt':.05,'yaw':3.,'pitch':2.,'observed_turn_response':False,
            'note':'Explicit fixture assumptions; real turn calibration unavailable.'}


def scenarios(seed,count,calibration):
    rng=np.random.default_rng(seed);distance=rng.uniform(6,50,count);crouch=rng.random(count)<.4
    part=rng.integers(0,3,count);heights=np.asarray([1.64,1.18,.88])[part]*np.where(crouch,.6,1)
    radius=np.asarray([.12,.25,.25])[part]*np.where(crouch,.6,1)
    return {'distance':distance,'want_pitch':np.arctan2(heights-1.62,distance),
            'want_yaw':rng.uniform(-.45,.45,count),'target_motion':rng.uniform(-.4,.4,count)/distance,
            'radius':radius,'yaw_response':calibration['yaw']*rng.uniform(.9,1.1,count),
            'pitch_response':calibration['pitch']*rng.uniform(.9,1.1,count),
            'cooldown':rng.integers(2,4,count),'dt':calibration['dt']}


def evaluate(parameters,world):
    count=len(world['distance']);yaw=np.zeros(count);pitch=np.zeros(count);last=np.full(count,-100)
    hits=np.zeros(count);bad=np.zeros(count);first=np.full(count,2.);dt=world['dt']
    steps=max(1,int(2/dt));tolerance=np.arctan2(world['radius'],world['distance'])
    for step in range(steps):
        want=world['want_yaw']+world['target_motion']*step*dt
        dyaw=(want-yaw+math.pi)%(2*math.pi)-math.pi;dpitch=world['want_pitch']-pitch
        requested=(np.abs(dyaw)<=tolerance*parameters[2])&(np.abs(dpitch)<=tolerance*parameters[2])&(step-last>=world['cooldown'])
        yaw+=np.clip(dyaw*parameters[0],-1,1)*world['yaw_response']*dt
        pitch+=np.clip(dpitch*parameters[1],-1,1)*world['pitch_response']*dt
        error=np.hypot((want-yaw+math.pi)%(2*math.pi)-math.pi,world['want_pitch']-pitch)
        aligned=requested&(error<=tolerance);bad+=requested&~aligned;hits+=aligned
        first=np.where(aligned&(first==2.),step*dt,first);last=np.where(requested,step,last)
    return {'score':float(np.mean(hits-2*bad-.5*first)), 'mean_aligned_requests':float(hits.mean()),
            'mean_misaligned_requests':float(bad.mean()),'mean_first_aligned_seconds':float(first.mean())}


def train(seed=73,calibration=None,population=24,iterations=5):
    calibration=calibration or turn_calibration();rng=np.random.default_rng(seed)
    training=scenarios(seed,48,calibration);validation=scenarios(seed+10000,192,calibration)
    mean=BASELINE.copy();spread=np.asarray([2.5,4.,.16]);best=BASELINE.copy();best_score=evaluate(best,training)['score']
    for _ in range(iterations):
        candidates=np.clip(rng.normal(mean,spread,(population,3)),LOW,HIGH)
        candidates[0]=BASELINE
        scores=np.asarray([evaluate(p,training)['score'] for p in candidates]);elite=candidates[np.argsort(scores)[-max(3,population//5):]]
        index=int(np.argmax(scores))
        if scores[index]>best_score:best=candidates[index].copy();best_score=float(scores[index])
        mean=elite.mean(axis=0);spread=np.maximum(elite.std(axis=0),[.1,.1,.01])
    baseline=evaluate(BASELINE,validation);candidate=evaluate(best,validation)
    improved=candidate['score']>baseline['score']+.02 and candidate['mean_misaligned_requests']<=baseline['mean_misaligned_requests']
    result={'status':'simulation_only_candidate' if improved else 'no_simulated_gain','method':'cross-entropy parameter search',
        'parameters':dict(zip(NAMES,map(float,best if improved else BASELINE))),
        'searched_parameters':dict(zip(NAMES,map(float,best))),'baseline_parameters':dict(zip(NAMES,map(float,BASELINE))),
        'recommendation_available':bool(improved),
        'synthetic_training_episodes':population*iterations*48,'synthetic_validation_episodes':192,
        'unique_training_scenarios':48,
        'calibration':calibration,'baseline_validation':baseline,'candidate_validation':candidate,
        'randomized_assumptions':{'turn_response_scale':[.9,1.1],'distance_m':[6,50],
                                  'all_targets_visible':True,'cooldown_decision_steps':[2,3],'duration_s':2},
        'real_match_validated':False,'may_promote':False,
        'limits':'Visible-target angular-control exercise only. Yaw/pitch response may be calibrated from own logs; '
            'target motion, jitter, cooldowns and all firing rewards are explicit approximations. '
            'No recoil, collision, ballistics, damage, economy, navigation or win simulation. '
            'Synthetic episodes never count as real matches or validate the FPS risk/racing setup models. '
            'oMLX may test these parameters; real-input gates and actual matches still decide.'}
    ml_data.save_json(ml_data.STORE/'fps/simulation.json',result);return result
