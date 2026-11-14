import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from coach import loop, recording
from coach.strategies import seed

class RacingEvidenceTests(unittest.TestCase):
    def test_retirement_only_recording_is_not_discarded(self):
        events=[{'kind':'retired','phase':'qualifying','tick':40,'seats':[9],
                 'payload':{'reason':'damage','lap':0}},
                {'kind':'qualifying_lap','phase':'qualifying','seats':[9],
                 'payload':{'valid':False,'lap_time_s':None}},
                {'kind':'retired','phase':'race','tick':60,'seats':[9],
                 'payload':{'reason':'damage','lap':0}}]
        with patch.object(recording,'fetch_events',return_value=events),patch.object(recording,'static_metadata',return_value={'track':{'id':'crossover','length_m':5803.1}}):
            rec=recording.read(None,'racing','job',9,9)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['facts']['track_id'],'crossover')
        self.assertFalse(rec['facts']['finished'])
        self.assertEqual(len(rec['facts']['retirements']),2)
        self.assertIn('RETIRED during qualifying: damage',rec['text'])
        self.assertIsNone(loop._finish_place({'rank':9,'recording':rec}))

    def test_missing_weather_is_unknown(self):
        f=recording._racing([{'kind':'retired','seats':[0],'payload':{'reason':'damage'}}],0)
        self.assertIsNone(f['wet_race'])

    def test_multiple_scalar_changes_are_rejected_not_silently_rewritten(self):
        base=seed('racing')
        proposal=loop.effective_proposal('racing',{'kv':1.9,'ld_min':9},base)
        self.assertEqual(proposal['kv'],1.9)
        self.assertEqual(proposal['ld_min'],9)
        self.assertIn('Multiple independent',loop.proposal_problem('racing',proposal,base))

    def test_fresh_trial_uses_its_played_book_and_enters_series(self):
        a=seed('racing'); b={**a,'name':'trial','kv':1.9}
        focus={'match_id':'new-job','created_at':'2026-11-14T00:00:00+00:00','rank':9}
        state={'A':{'strategy':a,'last':{}},'B':{'strategy':b,'last':focus}}
        with tempfile.TemporaryDirectory() as root,patch.object(loop,'STATE',Path(root)):
            (Path(root)/'logs/racing').mkdir(parents=True)
            played=loop.played_for_result(state,focus)
            self.assertEqual(played['kv'],1.9)
            text=loop.match_series('racing',focus,played)
            self.assertIn('new-job',text)
            self.assertIn('kv 1.9',text)
            self.assertIn('stops unknown',text)

    def test_immutable_log_wins_over_reused_slot(self):
        base=seed('racing');old={**base,'kv':1.6};new={**base,'kv':1.9}
        state={'A':{'strategy':new},'B':{'strategy':new,'last':{'match_id':'job'}}}
        with tempfile.TemporaryDirectory() as root,patch.object(loop,'STATE',Path(root)):
            p=Path(root)/'logs/racing';p.mkdir(parents=True)
            (p/'job.json').write_text(json.dumps({'played_strategy':old,'result':{'match_id':'job'}}))
            self.assertEqual(loop.played_for_result(state,{'match_id':'job'})['kv'],1.6)

    def test_wet_no_stop_is_not_a_measured_dry_stop_experiment(self):
        with tempfile.TemporaryDirectory() as root,patch.object(loop,'STATE',Path(root)):
            p=Path(root)/'logs/racing';p.mkdir(parents=True)
            row={'played_strategy':seed('racing'),'result':{'rank':0,'recording':{'facts':{'wet_race':True,'pit_stops':[],'finish_position':1}}}}
            (p/'job.json').write_text(json.dumps(row))
            self.assertEqual(loop.tried_stops({}),{})

if __name__=='__main__':unittest.main()

class TrackAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util, sys, types
        import numpy as np
        from coach.racing_assets import module_at, ASSETS
        cls.np=np
        cls.frozen=module_at('frozen_track_test',Path('assets/racing/template/agent.py'))
        strategy=types.ModuleType('strategy');strategy.STRATEGY=seed('racing')
        cls.saved={k:sys.modules.get(k) for k in ('strategy','formula_driver')}
        sys.modules['strategy']=strategy;sys.modules['formula_driver']=cls.frozen
        cls.wrapper=module_at('racing_dispatch_test',Path('runtime/racing/agent.py'))
        cls.root=tempfile.TemporaryDirectory()
        weights=Path(cls.root.name)/'weights';weights.mkdir()
        for track in ('parkland','highland','crossover'):
            (weights/track).symlink_to((ASSETS/track).resolve(),target_is_directory=True)
        (weights/'bindings.npz').symlink_to((ASSETS/'bindings.npz').resolve())
        cls.wrapper.init({'weights_dir':cls.root.name})

    @classmethod
    def tearDownClass(cls):
        import sys
        for key,value in cls.saved.items():
            if value is None:sys.modules.pop(key,None)
            else:sys.modules[key]=value
        cls.root.cleanup()

    def test_parkland_numeric_assets_remain_identical(self):
        from coach.racing_assets import ASSETS
        for name in ('plan.npz','mechanics.npz'):
            self.assertEqual((ASSETS/'parkland'/name).read_bytes(),(Path('assets/racing/template')/name).read_bytes())

    def test_all_tracks_route_to_the_correct_frozen_driver(self):
        from coach.racing_assets import ROOT
        np=self.np
        for track in ('parkland','highland','crossover'):
            with np.load(ROOT/f'agx-racing-kit/data/racing-{track}.mapdata.npz',allow_pickle=False) as data:
                cols=json.loads(data['centerline_columns_utf8'].tobytes());rows=data['centerline']
                point=rows[0]
                selected=self.wrapper.select_track({'self':{'x':float(point[cols.index('x_m')]),'y':float(point[cols.index('y_m')])}})
            self.assertEqual(selected,track)
            self.assertIsInstance(self.wrapper._drivers[selected],self.frozen.Driver)
            self.assertAlmostEqual(self.wrapper._drivers[selected].length,self.wrapper._drivers[selected].p['L'])

    def test_unknown_coordinates_do_not_default_to_parkland(self):
        self.assertIsNone(self.wrapper.select_track({'self':{'x':1e8,'y':1e8}}))

    def test_generated_plans_bind_matching_pits_and_lengths(self):
        from coach.racing_assets import ROOT,ASSETS,module_at
        maps=module_at('map_test',ROOT/'agx-racing-kit/tools/read_map.py')
        for track in ('highland','crossover'):
            md=maps.load(ROOT/f'agx-racing-kit/data/racing-{track}.mapdata.npz')
            p=self.frozen.load_plan(ASSETS/track)['plan']
            self.assertAlmostEqual(p['L'],md['length_m'])
            self.assertEqual(p['pit_entry_s'],md['pit']['entry_s'])
            self.assertEqual(p['pit_exit_s'],md['pit']['exit_s'])
            self.assertAlmostEqual(p['ds'],p['L']/p['n_nodes'])
            mechanics=self.frozen.load_mechanics(ASSETS/track,p['L'])
            self.assertEqual(len(mechanics['zones']),0)
