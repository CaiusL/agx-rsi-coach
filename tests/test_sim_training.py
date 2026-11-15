import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from coach import hybrid_ml, ml_data, sim_env, sim_policy, sim_training

CALIBRATION={'dt':.05,'yaw':3.,'pitch':2.}


class SimulationTrainingTests(unittest.TestCase):
    def test_hidden_world_state_is_absent_from_features_and_actions(self):
        w=sim_env.world(9,16,CALIBRATION)
        w['family'][:]=3;w['cover_start'][:]=0;w['cover_end'][:]=100
        x,context=sim_env.observe(w,np.zeros(16),np.zeros(16),np.full(16,-100),0)
        self.assertTrue((x[:,0]==0).all());self.assertTrue((x[:,1:7]==0).all())
        self.assertTrue((x[:,8:]==0).all())
        np.testing.assert_array_equal(sim_env.project(np.ones((16,3)),w,context),np.zeros((16,3)))

    def test_transition_data_has_feedback_and_terminal_boundaries(self):
        w=sim_env.world(2,12,CALIBRATION)
        model=sim_policy.initialize(len(sim_env.FEATURES),3)
        metrics,data=sim_env.rollout(w,lambda x:sim_policy.forward(model,x),record=True,episode_offset=70)
        self.assertEqual(set(data),set(sim_training.KEYS));self.assertEqual(len(data['x']),480)
        self.assertEqual(int(data['done'].sum()),12)
        self.assertEqual(set(data['episode']),set(range(70,82)))
        self.assertGreater(float(data['priority'].max()),1)
        self.assertTrue(all(np.isfinite(v).all() for v in data.values()))
        self.assertEqual(metrics['hidden_fire_requests'],0)

    def test_model_learns_and_resumes_with_bounded_replay_and_fresh_test_stream(self):
        original=sim_env.rollout;calls=[]
        def observe_rollout(w,*args,**kwargs):
            calls.append((w,kwargs.get('record',False)))
            return original(w,*args,**kwargs)
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)),patch.object(sim_env,'rollout',side_effect=observe_rollout):
            options=dict(calibration=CALIBRATION,warmup_episodes=128,rollout_episodes=128,cycles=3,epochs=3,max_rows=15000)
            first=sim_training.train(**options)
            self.assertTrue(first['improved_over_initial_policy'])
            self.assertFalse(first['may_promote']);self.assertFalse(first['real_match_validated'])
            self.assertLessEqual(first['buffer_transitions'],15000)
            self.assertEqual(first['new_transitions'],20480)
            # Training worlds have 128 episodes. Validation/test worlds never produce replay rows.
            self.assertTrue(all(len(w['distance'])==128 for w,record in calls if record))
            self.assertTrue(all(not record for w,record in calls if len(w['distance']) in (192,384)))
            second=sim_training.train(**options)
            self.assertEqual(second['generation'],2)
            self.assertEqual(second['next_episode'],1024)
            self.assertEqual(second['total_generated_transitions'],40960)
            self.assertEqual(hybrid_ml.train_fps()['matches'],0)
            with np.load(Path(tmp)/'fps/sim_policy/dataset.npz') as data:
                self.assertLessEqual(len(data['x']),15000)
                self.assertGreaterEqual(data['episode'].min(),0)
                self.assertLess(data['episode'].max(),1024)

    def test_simulation_runs_on_schedule_even_when_real_dataset_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)),patch.object(hybrid_ml,'train_fps',return_value={'status':'collecting_matches'}),patch.object(hybrid_ml,'train_racing',return_value={'status':'collecting_matches'}),patch.object(sim_training,'train',return_value={'status':'trained_simulation_skill'}) as fit:
            ml_data.save_json(Path(tmp)/'config.json',{'enabled':True,'simulation_learning':True})
            hybrid_ml.train(force_simulation=True);hybrid_ml.train()
            self.assertEqual(fit.call_count,2)
            ml_data.save_json(Path(tmp)/'fps/sim_policy/report.json',{'trained_at':'2999-01-01T00:00:00+00:00'})
            hybrid_ml.train();self.assertEqual(fit.call_count,2)


if __name__=='__main__':unittest.main()
