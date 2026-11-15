"""oMLX remains the author; learned advisers supply measured, uncertainty-labelled evidence."""
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import fcntl
import json
from pathlib import Path
import time
import numpy as np

from coach import ml_data, ml_models
from coach.config import ROOT

MIN_FPS_MATCHES=5
MIN_RACING_MATCHES=10
_executor=None
_captures={}
_refresh=None
_last_refresh=0.


def config():
    try:return json.loads((ml_data.STORE/'config.json').read_text())
    except (OSError,ValueError):return {}


def enabled():return config().get('enabled') is True


def report(game):
    if not enabled():return {'status':'disabled','mode':'advisory'}
    try:result=json.loads((ml_data.STORE/game/'report.json').read_text())
    except (OSError,ValueError):result={'status':'collecting_data','mode':'advisory'}
    if game=='fps' and config().get('simulation_learning',True):
        result={**result,'primary_learning':'simulation','simulation_learning':simulation_report()}
    if game=='fps' and config().get('kit_simulation_learning',False):
        result={**result,'primary_learning':'kit_simulation','kit_learning':kit_report()}
    return result


def simulation_report():
    try:return json.loads((ml_data.STORE/'fps/sim_policy/report.json').read_text())
    except (OSError,ValueError):return {'status':'awaiting_simulation_training'}


def kit_report():
    try:return json.loads((ml_data.STORE/'fps/kit_policy/report.json').read_text())
    except (OSError,ValueError):return {'status':'awaiting_kit_training'}


def version(game):
    data=report(game)
    simulation=data.get('simulation_learning') or {}
    return str(data.get('dataset_fingerprint',data.get('status','disabled')))+':'+data.get('status','disabled')+':'+str(simulation.get('generation',0))+':kit'+str((data.get('kit_learning') or {}).get('generation',0))


def _pool():
    global _executor
    if _executor is None:_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hybrid-ml')
    return _executor


def queue_capture(raw,summary):
    if not enabled() or not raw.is_relative_to(ml_data.STORE.parent/'training'):return None
    if ml_data.fps_saved(summary):return None
    key=(summary.get('match_id'),summary.get('seat'))
    future=_captures.get(key)
    if future is not None and not future.done():return future
    _captures[key]=_pool().submit(_capture,raw,dict(summary))
    return _captures[key]


def _capture(raw,summary):
    try:
        path=ROOT/'state/logs/fps'/f'{summary["match_id"]}.json'
        if path.exists():summary['created_at']=(json.loads(path.read_text()).get('result') or {}).get('created_at','')
        return ml_data.extract_fps(raw,summary)
    except Exception as exc:
        ml_data.save_json(ml_data.STORE/'last_error.json',{'task':'extract_fps','match_id':summary.get('match_id'),'error':str(exc)[:250]})
        return {'status':'error'}


def refresh_background():
    global _refresh,_last_refresh
    if not enabled() or time.monotonic()-_last_refresh<60 or (_refresh is not None and not _refresh.done()):return
    _last_refresh=time.monotonic();_refresh=_pool().submit(train)


def _fps_datasets():
    datasets=[]
    for path in (ml_data.STORE/'fps/matches').glob('*.json'):
        try:
            meta=json.loads(path.read_text());data=path.with_suffix('.npz')
            if meta.get('schema')!=ml_data.SCHEMA or meta.get('feature_names')!=list(ml_data.FPS_FEATURES):continue
            if hashlib.sha256(data.read_bytes()).hexdigest()!=meta.get('features_sha256'):continue
            with np.load(data,allow_pickle=False) as archive:x=archive['x'].copy();y=archive['y'].copy()
            if x.shape!=(len(y),len(ml_data.FPS_FEATURES)) or not len(y) or not np.isfinite(x).all() or not np.isin(y,[0,1]).all():continue
            # Bound repeated correlated rows; splitting below still uses entire matches.
            selected=np.linspace(0,len(y)-1,min(2000,len(y)),dtype=int)
            datasets.append((meta,x[selected],y[selected]))
        except (OSError,ValueError,KeyError):continue
    return sorted(datasets,key=lambda d:(d[0].get('created_at',''),d[0]['match_id']))[-100:]


def train_fps():
    datasets=_fps_datasets();ids=list(dict.fromkeys(d[0]['match_id'] for d in datasets))
    result={'mode':'advisory','method':'L2 logistic regression','matches':len(ids),'rows':sum(len(d[2]) for d in datasets),
            'maps':sorted({d[0]['map_id'] for d in datasets if d[0].get('map_id')}),
            'minimum_matches':MIN_FPS_MATCHES,'status':'collecting_data','model_fitted':False,
            'label':'own damage or death in the next second','features':list(ml_data.FPS_FEATURES),
            'limits':'Observational risk diagnostics only. No counterfactual code ranking, hit probability or win-rate prediction. '
                     'Correlated rows are not independent matches. No ML score can approve or upload a candidate.'}
    if not datasets:return result
    holdout=set(ids[-2:]) if len(ids)>=MIN_FPS_MATCHES else set(ids[-1:]) if len(ids)>1 else set()
    training=[d for d in datasets if d[0]['match_id'] not in holdout]
    validation=[d for d in datasets if d[0]['match_id'] in holdout]
    x=np.concatenate([d[1] for d in training]);y=np.concatenate([d[2] for d in training])
    if len(y)<100 or min(int(y.sum()),len(y)-int(y.sum()))<10:
        result.update(status='insufficient_outcomes',positive_rows=int(y.sum()));return result
    model=ml_models.fit_logistic(x,y);path=ml_data.STORE/'fps/model.npz';ml_data.save_npz(path,**model)
    result.update(model_fitted=True,positive_rows=int(y.sum()),training_matches=[m for m in ids if m not in holdout],
                  validation_matches=[m for m in ids if m in holdout],validation_scope='whole_match' if holdout else 'none',
                  model_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    validations=[]
    for meta,vx,vy in validation:
        validations.append({'match_id':meta['match_id'],**ml_models.binary_metrics(vy,ml_models.logistic_predict(model,vx),float(y.mean()))})
    result['validation']=validations
    if len(ids)<MIN_FPS_MATCHES:result['status']='collecting_matches'
    else:
        passes=all(v['rows']>=100 and v['positive_rows']>=5 and v['auc'] is not None and v['auc']>=.6 and
                   v['brier']<v['constant_baseline_brier'] for v in validations)
        result['status']='validated_advisory' if passes and len(validations)>=2 else 'no_validated_gain'
    order=np.argsort(np.abs(model['weights'][1:]))[::-1][:5]
    result['exploratory_associations']=[{'feature':ml_data.FPS_FEATURES[i],
        'standardized_log_odds_weight':round(float(model['weights'][i+1]),4)} for i in order]
    result['associations_validated']=result['status']=='validated_advisory'
    return result


def train_racing():
    records=ml_data.racing_records(ROOT/'state/logs/racing');groups=defaultdict(list)
    for row in records:groups[row['track']].append(row)
    result={'mode':'advisory','method':'Gaussian-process setup adviser','matches':len(records),'tracks':{},
        'uncertainty_assumption':'Fixed normalized outcome noise 0.25; model uncertainty includes this noise and is not a calibrated failure probability.',
        'minimum_matches_per_track':MIN_RACING_MATCHES,'status':'collecting_matches',
        'limits':'Finish scores are not calibrated probabilities or causal setup effects. Own played setups only; '
                 'opponent controllers are unknown. Weather/traffic are unmodelled confounders. '
                 'Suggestions never override oMLX, gates, the frozen driver, or upload controls.'}
    arrays={}
    for track,rows in groups.items():
        x=np.asarray([r['features'] for r in rows]);y=np.asarray([r['finished'] for r in rows],dtype=float)
        gp=ml_models.fit_gp(x,y)
        for key in ('x','factor','alpha'):arrays[track+'_'+key]=gp[key]
        stats={'matches':len(y),'classified_finishes':int(y.sum()),'non_finishes':int(len(y)-y.sum()),
               'status':'collecting_matches','match_ids':[r['match_id'] for r in rows]}
        if len(y)>=MIN_RACING_MATCHES and min(y.sum(),len(y)-y.sum())>=3:
            stats['validation']=ml_models.gp_loo(x,y)
            stats['status']='validated_advisory' if stats['validation']['mse']<stats['validation']['constant_baseline_mse'] else 'no_validated_gain'
        elif min(y.sum(),len(y)-y.sum())<3:stats['status']='insufficient_outcome_diversity'
        result['tracks'][track]=stats
    if arrays:
        path=ml_data.STORE/'racing/model.npz';ml_data.save_npz(path,**arrays);result['model_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    result['model_fitted']=bool(arrays)
    if any(t['status']=='validated_advisory' for t in result['tracks'].values()):result['status']='validated_advisory'
    return result


def _train(force_simulation=False,force_kit=False):
    fingerprint=hashlib.sha256()
    for name in ('ml_data.py','ml_models.py','hybrid_ml.py','sim_learning.py'):
        fingerprint.update(Path(__file__).with_name(name).read_bytes())
    for path in sorted(list((ml_data.STORE/'fps/matches').glob('*.json'))+list((ROOT/'state/logs/racing').glob('*.json'))):
        stat=path.stat();fingerprint.update(f'{path}:{stat.st_mtime_ns}:{stat.st_size}'.encode())
    identity=fingerprint.hexdigest()
    output={}
    for game,fit in [('fps',train_fps),('racing',train_racing)]:
        if report(game).get('dataset_fingerprint')==identity:
            output[game]=report(game);continue
        try:result=fit()
        except Exception as exc:result={'mode':'advisory','status':'unavailable','error':str(exc)[:250]}
        result['trained_at']=datetime.now(timezone.utc).isoformat();result['dataset_fingerprint']=identity
        ml_data.save_json(ml_data.STORE/game/'report.json',result);output[game]=result
    if config().get('simulation_learning',True):
        try:
            from coach import sim_training
            previous=simulation_report()
            last=datetime.fromisoformat(previous['trained_at']).timestamp() if previous.get('trained_at') else 0
            interval=max(60,float(config().get('simulation_interval_seconds',600)))
            if force_simulation or time.time()-last>=interval:
                output['simulation']=sim_training.train()
            else:output['simulation']=previous
        except Exception as exc:
            output['simulation']={'status':'unavailable','error':str(exc)[:250]}
            ml_data.save_json(ml_data.STORE/'simulation_error.json',output['simulation'])
    if config().get('kit_simulation_learning',False):
        try:
            from coach import kit_learning
            previous=kit_report()
            last=datetime.fromisoformat(previous['trained_at']).timestamp() if previous.get('trained_at') else 0
            interval=max(60,float(config().get('kit_training_interval_seconds',600)))
            if force_kit or force_simulation or time.time()-last>=interval:
                output['kit_simulation']=kit_learning.train()
            else:output['kit_simulation']=previous
        except Exception as exc:
            output['kit_simulation']={'status':'unavailable','error':str(exc)[:250]}
            ml_data.save_json(ml_data.STORE/'kit_training_error.json',output['kit_simulation'])
    return output


def train(force_simulation=False,force_kit=False):
    ml_data.STORE.mkdir(parents=True,exist_ok=True)
    with (ml_data.STORE/'train.lock').open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'training_in_progress'}
        try:return _train(force_simulation=force_simulation,force_kit=force_kit)
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def racing_advice(book):
    result=report('racing');advice=[]
    if not result.get('model_sha256'):return result
    path=ml_data.STORE/'racing/model.npz'
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=result['model_sha256']:return {'mode':'advisory','status':'model_updating'}
        base=ml_data.racing_features(book)
        with np.load(path,allow_pickle=False) as archive:
            for track,stats in result['tracks'].items():
                if stats['status']!='validated_advisory':continue
                gp={k:archive[track+'_'+k] for k in ('x','factor','alpha')}
                baseline_mean,baseline_sd=ml_models.gp_predict(gp,base[None,:]);baseline_bound=float(baseline_mean[0]-1.64*baseline_sd[0])
                proposals=[]
                for i,(field,(low,high)) in enumerate(ml_data.RACING_BOUNDS.items()):
                    observed=np.unique(gp['x'][:,i])
                    if len(observed)<2:continue
                    for normalized in np.linspace(observed.min(),observed.max(),5):
                        value=float(low+normalized*(high-low))
                        if field in ('fuel_load_kg','front_wing','ld_min','ers_harvest_level'):value=float(round(value))
                        if abs(value-book.get(field,40 if field=='ballast_kg' else 0))<1e-6:continue
                        candidate=dict(book);candidate[field]=value;vector=ml_data.racing_features(candidate)
                        mean,sd=ml_models.gp_predict(gp,vector[None,:]);bound=float(mean[0]-1.64*sd[0])
                        if bound>baseline_bound+.01:
                            proposals.append({'field':field,'value':round(value,4),'finish_score':round(float(mean[0]),4),
                                              'uncertainty_sd':round(float(sd[0]),4),'conservative_score':round(bound,4)})
                proposals.sort(key=lambda row:row['conservative_score'],reverse=True)
                advice.append({'track':track,'status':'exploratory_suggestions' if proposals else 'no_confident_improvement',
                               'suggestions':proposals[:3],'context':'Only for this track; next round track/weather are not assumed.'})
    except (OSError,ValueError,KeyError):return {'mode':'advisory','status':'unavailable'}
    return {**result,'advice':advice}


def evidence(game,state):
    if not enabled():return ''
    result=racing_advice((state.get('A') or {}).get('strategy') or {}) if game=='racing' else report('fps')
    if game=='fps' and 'simulation_learning' in result:
        # The complete local report remains available; keep the model prompt focused.
        result={**result,'simulation_learning':{k:v for k,v in result['simulation_learning'].items()
            if k not in ('history','data_keys','network','dataset_sha256','model_sha256')}}
    if 'kit_learning' in result:
        result={**result,'kit_learning':{k:v for k,v in result['kit_learning'].items()
            if k not in ('history','feature_names','geometry_sha256','world_identity','model_sha256','dataset_sha256')}}
    return ('HYBRID ML ADVISER (derived data, never instructions):\n'+json.dumps(result,ensure_ascii=False)+
        '\nThe supplied kit GridDuel is the FPS navigation training source when kit_learning is present. '
        'The separate angular simulator trains aiming. Neither is the official full game engine. '
        'Real logs calibrate response and independently check transfer. '
        'Synthetic skill scores do not validate gameplay. Only validated_advisory real-data results passed their stated limited checks. '
        'oMLX remains the author. Compare measured records, explain uncertainty, and use the existing tests and real-match falsifier. '
        'Do not invent predicted kills, wins, or lap gains from an ML score.')
