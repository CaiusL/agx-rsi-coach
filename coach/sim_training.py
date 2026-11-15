"""Generate transitions, train a neural skill policy, feed failures back, and test afresh."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import numpy as np

from coach import ml_data, sim_env, sim_policy, sim_learning

SCHEMA=1
KEYS=('x','target','action','reward','next_x','done','episode','priority')


def _copy(model):return {k:v.copy() for k,v in model.items()}


def _merge(old,new,rng,cap):
    data={k:np.concatenate([old[k],new[k]]) for k in KEYS} if old is not None else new
    if len(data['x'])>cap:
        # Mix uniform coverage with difficult learner-visited states.
        uniform=rng.choice(len(data['x']),cap//2,replace=False)
        remaining=np.setdiff1d(np.arange(len(data['x'])),uniform,assume_unique=True)
        probability=np.maximum(data['priority'][remaining],1);probability=probability/probability.sum()
        hard=rng.choice(remaining,cap-len(uniform),replace=False,p=probability)
        ids=np.concatenate([uniform,hard]);data={k:v[ids] for k,v in data.items()}
    return data


def train(seed=137,warmup_episodes=256,rollout_episodes=512,cycles=3,epochs=3,max_rows=120000,calibration=None):
    if min(warmup_episodes,rollout_episodes,cycles,epochs,max_rows)<1:
        raise ValueError('Training sizes, cycles, epochs and buffer capacity must be positive')
    started=time.monotonic();folder=ml_data.STORE/'fps/sim_policy'
    calibration=calibration or sim_learning.turn_calibration()
    identity=hashlib.sha256(b''.join(Path(__file__).with_name(n).read_bytes() for n in ('sim_env.py','sim_policy.py','sim_training.py'))+
        json.dumps({'seed':seed,**{k:round(calibration[k],4) for k in ('dt','yaw','pitch')}},sort_keys=True).encode()).hexdigest()
    previous={};data=None;model=sim_policy.initialize(len(sim_env.FEATURES),seed)
    try:
        previous=json.loads((folder/'report.json').read_text())
        if previous.get('schema')!=SCHEMA or previous.get('world_identity')!=identity:previous={}
        if previous:
            model_path=folder/'model.npz';data_path=folder/'dataset.npz'
            if hashlib.sha256(model_path.read_bytes()).hexdigest()!=previous['model_sha256'] or hashlib.sha256(data_path.read_bytes()).hexdigest()!=previous['dataset_sha256']:
                raise ValueError('Synthetic checkpoint changed')
            with np.load(model_path,allow_pickle=False) as archive:loaded={k:archive[k].copy() for k in model}
            if any(loaded[k].shape!=model[k].shape or not np.isfinite(loaded[k]).all() for k in model):raise ValueError('Invalid neural checkpoint')
            model=loaded
            with np.load(data_path,allow_pickle=False) as archive:data={k:archive[k].copy() for k in KEYS}
            if data['x'].shape[1]!=len(sim_env.FEATURES) or not all(np.isfinite(v).all() for v in data.values()):raise ValueError('Invalid synthetic buffer')
    except (OSError,ValueError,KeyError):previous={};data=None;model=sim_policy.initialize(len(sim_env.FEATURES),seed)
    generation=previous.get('generation',0)+1;episode_offset=previous.get('next_episode',0)
    rng=np.random.default_rng(np.random.SeedSequence([seed,3,generation]))
    validation_world=sim_env.world(seed,192,calibration,split=1,generation=generation)
    initial_validation,_=sim_env.rollout(validation_world,lambda x:sim_policy.forward(model,x))
    initial_model=_copy(model);best=_copy(model);best_validation=initial_validation;history=[]
    # Teacher-labelled warm-up uses synthetic observations, never real future inputs.
    warm=sim_env.world(seed,warmup_episodes,calibration,split=0,generation=generation*100)
    _,generated=sim_env.rollout(warm,record=True,episode_offset=episode_offset)
    new_rows=len(generated['x']);episode_offset+=warmup_episodes
    data=_merge(data,generated,rng,max_rows)
    sim_policy.fit(model,data,epochs=epochs,seed=seed+generation)
    warm_validation,_=sim_env.rollout(validation_world,lambda x:sim_policy.forward(model,x))
    if warm_validation['score']>=best_validation['score'] and warm_validation['mean_misaligned_requests']<=best_validation['mean_misaligned_requests']+.01:
        best=_copy(model);best_validation=warm_validation
    for iteration in range(cycles):
        w=sim_env.world(seed,rollout_episodes,calibration,split=0,generation=generation*100+iteration+1)
        rollout_metrics,generated=sim_env.rollout(w,lambda x:sim_policy.forward(model,x),record=True,episode_offset=episode_offset)
        episode_offset+=rollout_episodes;new_rows+=len(generated['x']);data=_merge(data,generated,rng,max_rows)
        loss=sim_policy.fit(model,data,epochs=epochs,seed=seed+generation*100+iteration)
        validation,_=sim_env.rollout(validation_world,lambda x:sim_policy.forward(model,x))
        accepted=validation['score']>=best_validation['score'] and validation['mean_misaligned_requests']<=best_validation['mean_misaligned_requests']+.01
        if accepted:best=_copy(model);best_validation=validation
        history.append({'cycle':iteration+1,'buffer_rows':len(data['x']),'training_loss':loss,
                        'learner_rollout_score':rollout_metrics['score'],'validation_score':validation['score'],
                        'checkpoint_selected':bool(accepted)})
    # This stream is not used for fitting, failure replay or checkpoint selection.
    test_world=sim_env.world(seed,384,calibration,split=2,generation=generation)
    test,_=sim_env.rollout(test_world,lambda x:sim_policy.forward(best,x))
    initial_test,_=sim_env.rollout(test_world,lambda x:sim_policy.forward(initial_model,x))
    reference,_=sim_env.rollout(test_world)
    failure_feedback=[{'scenario':name,'test_score':test['families'][name]['score'],
        'reference_score':reference['families'][name]['score'],
        'reference_gap':reference['families'][name]['score']-test['families'][name]['score']}
        for name in test['families']]
    failure_feedback.sort(key=lambda row:row['reference_gap'],reverse=True)
    acquired=test['mean_aligned_requests']>=.8*reference['mean_aligned_requests'] and test['hidden_fire_requests']==0
    beats_reference=test['score']>reference['score']+.02 and test['mean_misaligned_requests']<=reference['mean_misaligned_requests']
    model_path=folder/'model.npz';data_path=folder/'dataset.npz'
    ml_data.save_npz(model_path,**best);ml_data.save_npz(data_path,**data)
    result={'schema':SCHEMA,'world_identity':identity,'generation':generation,'next_episode':episode_offset,
        'trained_at':datetime.now(timezone.utc).isoformat(),'source':'synthetic','primary_learning':True,
        'status':'trained_simulation_skill' if acquired else 'learning_partial_skill',
        'method':'neural imitation learning with learner-state aggregation and failure-weighted replay (DAgger-style)',
        'network':{'features':list(sim_env.FEATURES),'hidden_units':[32,32],'outputs':['yaw_rate','pitch_rate','fire_probability']},
        'new_transitions':new_rows,'buffer_transitions':len(data['x']),
        'total_generated_transitions':previous.get('total_generated_transitions',0)+new_rows,
        'new_training_episodes':warmup_episodes+cycles*rollout_episodes,
        'validation_episodes':192,'test_episodes':384,'history':history,
        'failure_feedback':failure_feedback,
        'feedback_protocol':'Teacher labels on learner-visited training states; disagreement, missed fire and negative reward increase replay priority. Final test failures are reporting only, never replayed.',
        'initial_test':initial_test,'learned_test':test,'analytic_reference_test':reference,
        'improved_over_initial_policy':bool(test['score']>initial_test['score']+.1),
        'beats_analytic_reference':bool(beats_reference),'incumbent_retained':not beats_reference,
        'data_keys':list(KEYS),'dataset_bytes':data_path.stat().st_size,'model_bytes':model_path.stat().st_size,
        'dataset_sha256':hashlib.sha256(data_path.read_bytes()).hexdigest(),
        'model_sha256':hashlib.sha256(model_path.read_bytes()).hexdigest(),'calibration':calibration,
        'split_protocol':'Disjoint SeedSequence streams for training, validation selection and fresh final tests; no held-out rows enter the buffer.',
        'control_boundary':'The network controls coarse aim. Analytic fine aiming and a visibility/alignment/cooldown firing guard remain explicit.',
        'wall_seconds':round(time.monotonic()-started,3),'real_match_validated':False,'may_promote':False,
        'limits':'Synthetic skill learning, not full-game reinforcement learning. Hidden target coordinates and motion are not policy features. '
            'No recoil, spread, collision, damage, navigation, economy, real kill or match-win model. '
            'The teacher is an analytical reference, not a top-player policy. Synthetic counts never increase real-match sample counts. '
            'Weights remain local and are not bundled in an AGX entry; oMLX receives diagnostics, not permission to deploy.'}
    ml_data.save_json(folder/'report.json',result);return result
