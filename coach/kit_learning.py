"""Resumable local navigation learning from the supplied kit's GridDuel engine."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from coach import kit_env, kit_policy, ml_data
from coach.config import ROOT

SCHEMA = 1
HOLDOUT_MAP = 'mirage'
KEYS = ('x', 'target', 'action', 'reward', 'next_x', 'done', 'episode', 'priority', 'map_index')


def _merge(old,new,rng,capacity):
    data={k:np.concatenate([old[k],new[k]]) for k in KEYS} if old is not None else new
    if len(data['x'])>capacity:
        broad=rng.choice(len(data['x']),capacity//2,replace=False)
        remaining=np.setdiff1d(np.arange(len(data['x'])),broad)
        priority=np.maximum(data['priority'][remaining],1);priority=priority/priority.sum()
        hard=rng.choice(remaining,capacity-len(broad),replace=False,p=priority)
        indices=np.concatenate([broad,hard]);data={k:v[indices] for k,v in data.items()}
    return data


def calibration():
    choices = []
    for path in (ml_data.STORE.parent/'training/fps').glob('*.summary.json'):
        try:
            row = json.loads(path.read_text()); fitted = row.get('calibration') or {}
            speed = (fitted.get('parameters') or {}).get('move_speed_mps')
            count = (fitted.get('samples') or {}).get('speed', 0)
            if row.get('status') == 'verified' and isinstance(speed, (float, int)) and np.isfinite(speed) and speed > 0 and count >= 50:
                choices.append((path.stat().st_mtime_ns, speed, count, row['match_id']))
        except (OSError, ValueError, KeyError): continue
    chosen = json.loads((ROOT/'sim/params/fps.json').read_text())
    result = {'move_speed_mps': chosen['parameters']['move_speed_mps'],
              'reaction_seconds': chosen['parameters']['reaction_seconds'],
              'speed_basis': 'documented fallback estimate',
              'reaction_basis': 'Chosen spotting-duration assumption; not identified by the kit fitter.'}
    if choices:
        _, speed, count, mid = max(choices)
        result.update(move_speed_mps=speed, speed_basis='verified own-seat effective motion envelope',
                      speed_samples=count, source_match=mid)
    return result


def _evaluate(worlds, parameters, seed, count, model):
    policy = None if model is None else lambda x: kit_policy.forward(model, x)
    return [kit_env.rollouts(w, parameters, [*seed, i], episodes=count, policy=policy)[0] for i, w in enumerate(worlds)]


def _score(rows):
    return float(np.mean([r['arrival_rate']-.001*r['mean_steps']/96 for r in rows]))


def _copy(model):
    return {k: v.copy() for k, v in model.items()}


def train(seed=251, warmup_per_map=96, learner_per_map=96, cycles=3, epochs=6,
          max_rows=100000, validation_episodes=24, test_episodes=48, worlds=None):
    if min(warmup_per_map, learner_per_map, cycles, epochs, max_rows, validation_episodes, test_episodes) < 1:
        raise ValueError('Training sizes must be positive')
    started = time.monotonic(); parameters = calibration(); worlds = worlds or kit_env.maps()
    training = [w for w in worlds if w.name != HOLDOUT_MAP]; unseen = [w for w in worlds if w.name == HOLDOUT_MAP]
    if not training or not unseen: raise ValueError('Separate training maps and held-out Mirage map are required')
    source = b''.join(Path(__file__).with_name(n).read_bytes() for n in ('kit_env.py','kit_policy.py','kit_learning.py'))
    source += (ROOT/'agx-fps-kit/sim/optimize.py').read_bytes()
    identity = hashlib.sha256(source+json.dumps([seed, [(w.name,w.identity) for w in worlds]]).encode()).hexdigest()
    folder = ml_data.STORE/'fps/kit_policy'; model = kit_policy.initialize(len(kit_env.FEATURES),seed)
    previous = {}; data = None
    try:
        previous = json.loads((folder/'report.json').read_text())
        if previous.get('schema') != SCHEMA or previous.get('world_identity') != identity: previous = {}
        if previous:
            for name in ('model','dataset'):
                if hashlib.sha256((folder/f'{name}.npz').read_bytes()).hexdigest() != previous[f'{name}_sha256']:
                    raise ValueError('Kit checkpoint integrity mismatch')
            with np.load(folder/'model.npz', allow_pickle=False) as archive:
                loaded = {k: archive[k].copy() for k in model}
            if any(loaded[k].shape != model[k].shape or not np.isfinite(loaded[k]).all() for k in model):
                raise ValueError('Invalid navigation checkpoint')
            with np.load(folder/'dataset.npz', allow_pickle=False) as archive:
                data = {k: archive[k].copy() for k in KEYS}
            if data['x'].shape[1] != len(kit_env.FEATURES) or not all(np.isfinite(v).all() for v in data.values()):
                raise ValueError('Invalid navigation replay')
            model = loaded
    except (OSError,ValueError,KeyError):
        previous = {}; data = None; model = kit_policy.initialize(len(kit_env.FEATURES),seed)
    generation = previous.get('generation',0)+1; offset = previous.get('next_episode',0)
    rng = np.random.default_rng([seed, generation]); initial = _copy(model); best = _copy(model)
    validation_seed = [seed, generation, 1]
    best_validation = _evaluate(training, parameters, validation_seed, validation_episodes, model)
    history = []; generated_rows = 0; generated_episodes = 0
    for iteration in range(cycles+1):
        rows = []
        for index, world in enumerate(training):
            count = warmup_per_map if iteration == 0 else learner_per_map
            policy = None if iteration == 0 else lambda x: kit_policy.forward(model,x)
            _, generated = kit_env.rollouts(world, parameters, [seed,generation,0,iteration,index],
                episodes=count, policy=policy, record=True, episode_offset=offset,map_index=index)
            offset += count; generated_episodes += count; generated_rows += len(generated['x']); rows.append(generated)
        batch = {k: np.concatenate([r[k] for r in rows]) for k in KEYS}
        data = _merge(data,batch,rng,max_rows)
        loss = kit_policy.fit(model,data,epochs=epochs,seed=seed+generation*100+iteration)
        validation = _evaluate(training,parameters,validation_seed,validation_episodes,model)
        selected = _score(validation) >= _score(best_validation)
        if selected: best = _copy(model); best_validation = validation
        history.append({'cycle':iteration,'teacher_warmup':iteration==0,'buffer_rows':len(data['x']),
                        'training_loss':loss,'validation':validation,'selected':selected})
    # Neither final-test stream influences fitting, failure replay or checkpoint selection.
    test_seed = [seed,generation,2]
    known_test = _evaluate(training,parameters,test_seed,test_episodes,best)
    unknown_test = _evaluate(unseen,parameters,[seed,generation,3],test_episodes,best)
    initial_known = _evaluate(training,parameters,test_seed,test_episodes,initial)
    initial_unknown = _evaluate(unseen,parameters,[seed,generation,3],test_episodes,initial)
    reference = (_evaluate(training,parameters,test_seed,test_episodes,None)+
                 _evaluate(unseen,parameters,[seed,generation,3],test_episodes,None))
    model_path = folder/'model.npz'; dataset_path = folder/'dataset.npz'
    ml_data.save_npz(model_path,**best); ml_data.save_npz(dataset_path,**data)
    result = {'schema':SCHEMA,'world_identity':identity,'generation':generation,'next_episode':offset,
        'trained_at':datetime.now(timezone.utc).isoformat(),'status':'kit_navigation_model_trained',
        'engine':'Supplied agx-fps-kit/sim/optimize.py GridDuel.step (2D approximation)',
        'method':'Neural imitation learning with learner-state aggregation and failure-weighted replay',
        'device':'local CPU / NumPy; no Colab or external training service',
        'training_maps':[w.name for w in training],'unseen_test_maps':[w.name for w in unseen],
        'map_index_names':{str(i):w.name for i,w in enumerate(training)},
        'geometry_sha256':{w.name:w.identity for w in worlds},
        'kit_source_sha256':hashlib.sha256((ROOT/'agx-fps-kit/sim/optimize.py').read_bytes()).hexdigest(),
        'network':{'features':len(kit_env.FEATURES),'hidden_units':[64,64],'actions':list(kit_env.ACTIONS)},
        'feature_names':list(kit_env.FEATURES),'calibration':parameters,'speed_randomization':[.85,1.15],
        'new_transitions':generated_rows,'buffer_transitions':len(data['x']),
        'total_generated_transitions':previous.get('total_generated_transitions',0)+generated_rows,
        'new_training_episodes':generated_episodes,'total_training_episodes':offset,
        'validation':best_validation,'known_map_test':known_test,'unseen_map_test':unknown_test,
        'initial_known_map_test':initial_known,'initial_unseen_map_test':initial_unknown,'analytic_reference':reference,
        'improved_on_known_maps':_score(known_test)>_score(initial_known)+.01,
        'improved_on_unseen_maps':_score(unknown_test)>_score(initial_unknown)+.01,
        'history':history,'model_bytes':model_path.stat().st_size,'dataset_bytes':dataset_path.stat().st_size,
        'model_sha256':hashlib.sha256(model_path.read_bytes()).hexdigest(),
        'dataset_sha256':hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        'wall_seconds':round(time.monotonic()-started,3),'may_promote':False,'real_match_validated':False,
        'split_protocol':'Training maps use stream 0; stream 1 selects checkpoints; fresh stream 2 tests known maps; Mirage stream 3 never enters fitting or replay. Episode IDs cover training only.',
        'feedback_protocol':'Teacher labels all optimal four-neighbour moves on learner-visited states; non-progress, disagreement and stalls increase replay priority. Reward is recorded feedback, not a full-game RL objective.',
        'control_boundary':'The neural policy chooses a legal next-cell move. Public-grid legality masks and a goal-navigation no-idle guard are explicit. No analytical path planner runs for learner action selection.',
        'limits':'Not the official AGX engine. Kit floors collapse to XY; no heights, ladders, jumps, body collision, shooting, damage, economy or bomb mechanics. '
                 'Opponent follows an analytical shortest path, not a learned or top-player strategy; this is not self-play. '
                 'Visibility uses the kit contours, not engine raycasts or the real view cone. Local crop observation may alias global maze states. '
                 'Arrival rates measure sampled connected local routes of 8–32 cells, not full spawn-to-site reachability or win rates. '
                 'Stored weights are experimental and do not replace the published FPS hotfix or frozen racing driver.'}
    ml_data.save_json(folder/'report.json',result)
    return result
