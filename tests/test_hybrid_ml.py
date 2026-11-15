import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import numpy as np

from coach import hybrid_ml, ml_data, ml_models, sim_learning, match_retention


class LearningTests(unittest.TestCase):
    def test_logistic_generalizes_to_separate_examples_and_auc_handles_ties(self):
        rng=np.random.default_rng(7);x=rng.normal(size=(400,3));y=(x[:,0]+.5*x[:,1]>0).astype(float)
        model=ml_models.fit_logistic(x[:300],y[:300]);p=ml_models.logistic_predict(model,x[300:])
        metrics=ml_models.binary_metrics(y[300:],p,y[:300].mean())
        self.assertGreater(metrics['auc'],.95);self.assertLess(metrics['brier'],metrics['constant_baseline_brier'])
        self.assertEqual(ml_models.binary_metrics([0,1],[.5,.5],.5)['auc'],.5)

    def test_gp_uncertainty_increases_away_from_observed_setups(self):
        model=ml_models.fit_gp([[0.],[.1]],[0.,1.]);_,sd=ml_models.gp_predict(model,[[0.],[10.]])
        self.assertLess(sd[0],sd[1]);self.assertTrue(np.isfinite(sd).all())

    def dataset(self,root,index,count=400):
        rng=np.random.default_rng(index);x=rng.random((count,len(ml_data.FPS_FEATURES))).astype(np.float32)
        y=(x[:,0]>.65).astype(np.uint8);mid=str(uuid.UUID(int=index+1));path,meta=ml_data.fps_paths(mid,7)
        ml_data.save_npz(path,x=x,y=y)
        ml_data.save_json(meta,{'schema':ml_data.SCHEMA,'match_id':mid,'seat':7,'created_at':f'2026-11-{index+1:02}T00:00:00Z',
            'feature_names':ml_data.FPS_FEATURES,'features_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        return mid

    def test_many_rows_from_one_match_never_pass_independent_match_requirement(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'):
            self.dataset(Path(tmp),0,2000);result=hybrid_ml.train_fps()
            self.assertTrue(result['model_fitted']);self.assertEqual(result['matches'],1)
            self.assertEqual(result['status'],'collecting_matches');self.assertEqual(result['validation_scope'],'none')

    def test_holdout_matches_are_excluded_from_normalization_and_fit(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'):
            ids=[self.dataset(Path(tmp),i) for i in range(5)];result=hybrid_ml.train_fps()
            self.assertEqual(result['training_matches'],ids[:3]);self.assertEqual(result['validation_matches'],ids[3:])
            self.assertEqual(result['status'],'validated_advisory')
            self.assertEqual(len(result['validation']),2)
            fitting=[]
            for mid in ids[:3]:
                path,_=ml_data.fps_paths(mid,7)
                with np.load(path,allow_pickle=False) as data:fitting.append(data['x'])
            with np.load(ml_data.STORE/'fps/model.npz',allow_pickle=False) as model:
                np.testing.assert_allclose(model['mean'],np.concatenate(fitting).mean(axis=0),rtol=1e-5)

    def test_damage_labels_use_future_hp_but_features_do_not(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'):
            raw=Path(tmp)/'raw.jsonl';rows=[]
            for tick in range(0,153,3):
                rows.append({'seat':7,'observation':{'phase':'live','match_tick':tick,'round_index':0,
                    'me':{'alive':True,'hp':100 if tick<63 else 80,'armor':100,'ammo':30,'weapons':[],
                          'x':0.,'y':0.,'z':0.,'yaw':0.,'pitch':0.},'visible':[]},'action':{}})
            raw.write_text(''.join(json.dumps(r)+'\n' for r in rows));summary={'status':'verified','match_id':str(uuid.UUID(int=99)),
                'seat':7,'calls':len(rows),'sha256':'verified-original-decision-hash'}
            result=ml_data.extract_fps(raw,summary);data,_=ml_data.fps_paths(summary['match_id'],7)
            with np.load(data,allow_pickle=False) as archive:
                labels=dict(zip(archive['ticks'],archive['y']));x=archive['x']
                self.assertEqual(labels[0],0);self.assertEqual(labels[30],1)
                self.assertEqual(x[0,0],1.);self.assertEqual(x[1,0],1.)
                self.assertNotIn(120,labels)  # Censored negative, not silently labelled safe.
            self.assertGreater(result['censored_rows'],0)

    def test_changed_feature_archive_is_not_used_to_train(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'):
            mid=self.dataset(Path(tmp),0);path,_=ml_data.fps_paths(mid,7);path.write_bytes(b'corrupt')
            self.assertEqual(hybrid_ml.train_fps()['matches'],0)

    def test_synthetic_rollouts_are_never_counted_as_real_matches(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'):
            ml_data.save_json(ml_data.STORE/'fps/simulation.json',{'synthetic_training_episodes':1000000})
            self.assertEqual(hybrid_ml.train_fps()['matches'],0)

    def test_simulation_learner_remains_separate_and_cannot_promote(self):
        calibration={'dt':.05,'yaw':3.,'pitch':2.,'observed_turn_response':True}
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'):
            result=sim_learning.train(seed=11,calibration=calibration,population=8,iterations=2)
            self.assertFalse(result['may_promote']);self.assertFalse(result['real_match_validated'])
            self.assertEqual(result['synthetic_validation_episodes'],192)
            self.assertTrue(all(np.isfinite(list(result['parameters'].values()))))
            if result['status']=='no_simulated_gain':
                self.assertEqual(result['parameters'],result['baseline_parameters'])

    def test_simulator_detects_deliberately_slow_aim_controller(self):
        world=sim_learning.scenarios(3,100,{'dt':.05,'yaw':3.,'pitch':2.})
        good=sim_learning.evaluate(sim_learning.BASELINE,world);bad=sim_learning.evaluate([2.,2.,1.],world)
        self.assertGreater(good['score'],bad['score'])

    def test_retention_waits_for_durable_compact_capture(self):
        old=str(uuid.UUID(int=300));new=str(uuid.UUID(int=301))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=root/'training';folder=store/'fps';folder.mkdir(parents=True)
            for mid in (old,new):
                (folder/(mid+'-seat7.jsonl')).write_text('raw')
                (folder/(mid+'-seat7.summary.json')).write_text(json.dumps({'status':'verified','match_id':mid,'seat':7,'sha256':mid}))
            with patch.multiple(match_retention,TRAINING=store,RECORDINGS=root/'recordings',HISTORY=root/'history',LOGS=root/'logs'),patch.object(match_retention,'latest_only',return_value=True),patch.object(match_retention,'_created',side_effect=lambda g,m:'1' if m==old else '2'),patch.object(ml_data,'STORE',root/'ml'),patch.object(hybrid_ml,'enabled',return_value=True),patch.object(hybrid_ml,'queue_capture') as capture,patch.object(ml_data,'fps_saved',return_value=False):
                result=match_retention.prune('fps');self.assertEqual(result['pending_ml_capture'],1)
                self.assertTrue((folder/(old+'-seat7.jsonl')).exists());capture.assert_called_once()
                with patch.object(ml_data,'fps_saved',return_value=True):match_retention.prune('fps')
                self.assertFalse((folder/(old+'-seat7.jsonl')).exists())
