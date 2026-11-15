import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from coach import kit_env,kit_learning,kit_policy,ml_data,hybrid_ml
from coach.sim import _kit

PARAMETERS={'move_speed_mps':5.,'reaction_seconds':.5}


def sample_map(root,name):
    cells=[[x,y,0.] for x in range(10) for y in range(10) if not (4<=x<=5 and 4<=y<=5)]
    data={'walkable_surface_candidates':{'cells':cells,'origin_xy':[0.,0.],'cell_size_m':1.},
          'wall_cover_contours':[{'polygon_xy':[[4.,4.],[6.,4.],[6.,6.],[4.,6.]]}]}
    path=root/f'fps-{name}.mapdata.json';path.write_text(json.dumps(data));return kit_env.MapWorld(path)


class KitLearningTests(unittest.TestCase):
    def test_transition_adapter_matches_unmodified_kit_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            world=sample_map(Path(tmp),'train');kit=_kit('fps')
            a=kit.GridDuel(world.grid,[[2.5,5.5],[8.5,5.5]],world.polygons,5.,.5)
            b=kit.GridDuel(world.grid,[[2.5,5.5],[8.5,5.5]],world.polygons,5.,.5)
            for actions in ([(1,0),(-1,0)],[(0,1),(0,-1)],[(0,0),(1,0)]):
                self.assertEqual(kit_env.step(world,a,actions),b.step(actions))

    def test_hidden_opponent_coordinates_never_enter_features(self):
        with tempfile.TemporaryDirectory() as tmp:
            world=sample_map(Path(tmp),'train');duel,goal,_=world.episode(np.random.default_rng(4),PARAMETERS)
            a=kit_env.features(world,duel,goal,False,0);duel.positions[1]=(1000,1000)
            b=kit_env.features(world,duel,goal,False,0)
            np.testing.assert_array_equal(a,b)

    def test_teacher_generates_terminal_transitions_through_real_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            world=sample_map(Path(tmp),'train');r,data=kit_env.rollouts(world,PARAMETERS,2,episodes=8,record=True,map_index=7)
            self.assertEqual(r['arrival_rate'],1.)
            self.assertEqual(data['x'].shape[1],len(kit_env.FEATURES))
            self.assertEqual(int(data['done'].sum()),8)
            np.testing.assert_allclose(data['target'].sum(axis=1),1.)
            self.assertTrue(all(np.isfinite(v).all() for v in data.values()))
            self.assertTrue((data['map_index']==7).all())

    def test_neural_policy_learns_teacher_labels(self):
        rng=np.random.default_rng(3);x=rng.normal(size=(512,8)).astype(np.float32)
        target=np.eye(5,dtype=np.float32)[(x[:,0]>0).astype(int)]
        data={'x':x,'target':target,'priority':np.ones(len(x),dtype=np.float32)}
        model=kit_policy.initialize(8,1)
        before=np.mean(np.argmax(kit_policy.forward(model,x),axis=1)==np.argmax(target,axis=1))
        kit_policy.fit(model,data,epochs=12)
        after=np.mean(np.argmax(kit_policy.forward(model,x),axis=1)==np.argmax(target,axis=1))
        self.assertGreater(after,.9);self.assertGreater(after,before+.2)

    def test_resume_keeps_buffer_bounded_and_never_trains_on_holdout_map(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)/'ml'),patch.object(kit_learning,'calibration',return_value=PARAMETERS):
            worlds=[sample_map(Path(tmp),'train'),sample_map(Path(tmp),'mirage')];calls=[];original=kit_env.rollouts
            def observed(world,*args,**kwargs):
                calls.append((world.name,kwargs.get('record',False)));return original(world,*args,**kwargs)
            options=dict(worlds=worlds,warmup_per_map=8,learner_per_map=8,cycles=1,epochs=2,
                         validation_episodes=4,test_episodes=4,max_rows=400)
            with patch.object(kit_env,'rollouts',side_effect=observed):
                first=kit_learning.train(**options);second=kit_learning.train(**options)
            self.assertEqual(second['generation'],2);self.assertEqual(second['next_episode'],32)
            self.assertLessEqual(second['buffer_transitions'],400)
            self.assertEqual(second['unseen_test_maps'],['mirage']);self.assertFalse(second['may_promote'])
            self.assertFalse(any(name=='mirage' and record for name,record in calls))
            self.assertEqual(second['total_generated_transitions'],first['new_transitions']+second['new_transitions'])
            with np.load(Path(tmp)/'ml/fps/kit_policy/dataset.npz',allow_pickle=False) as archive:
                self.assertTrue((archive['map_index']==0).all())
            self.assertEqual(hybrid_ml.train_fps()['matches'],0)

    def test_generation_continues_without_new_real_matches(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(ml_data,'STORE',Path(tmp)),patch.object(hybrid_ml,'train_fps',return_value={'status':'collecting_matches'}),patch.object(hybrid_ml,'train_racing',return_value={'status':'collecting_matches'}),patch.object(kit_learning,'train',return_value={'status':'kit_navigation_model_trained'}) as trainer:
            ml_data.save_json(Path(tmp)/'config.json',{'enabled':True,'simulation_learning':False,'kit_simulation_learning':True})
            hybrid_ml.train(force_kit=True);hybrid_ml.train()
            self.assertEqual(trainer.call_count,2)
            ml_data.save_json(Path(tmp)/'fps/kit_policy/report.json',{'trained_at':'2999-01-01T00:00:00+00:00'})
            hybrid_ml.train();self.assertEqual(trainer.call_count,2)


if __name__=='__main__':unittest.main()
