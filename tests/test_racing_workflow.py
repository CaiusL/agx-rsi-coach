import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from coach import sim, recording, racing_workflow as workflow
from coach.strategies import seed, clamp, design_document


def experiment(lever):
    return {'experiment':{'lever':lever,'why_this_lever':'Unexplored live car field',
            'dry_hypothesis':'Test corner grip against straight-line speed.',
            'wet_hypothesis':'Test stability under reduced grip.',
            'uncertainty':'The simulator does not model this effect; a match is required.',
            'simulation_effect_s':None}}

class RacingWorkflowTests(unittest.TestCase):
    def test_starved_wings_cannot_be_ignored_by_another_control_trial(self):
        with tempfile.TemporaryDirectory() as folder:
            state={'decisions':[{'changed':['  kv: 1.6 -> 1.9']}] * 4}
            ledger=workflow.coverage(state,Path(folder))
        self.assertTrue(ledger['car_exploration_due'])
        base=seed('racing');candidate={**base,'preview':.25}
        self.assertIn('Car exploration is due',workflow.experiment_problem(experiment('preview'),candidate,base,ledger))
        candidate={**base,'front_wing':21}
        self.assertEqual(workflow.experiment_problem(experiment('front_wing'),candidate,base,ledger),'')
    def test_dry_only_argument_is_rejected(self):
        base=seed('racing');raw=experiment('front_wing');raw['experiment'].pop('wet_hypothesis')
        self.assertIn('wet_hypothesis',workflow.experiment_problem(raw,{**base,'front_wing':21},base,{}))
    def test_invented_simulated_gain_is_rejected(self):
        base=seed('racing');raw=experiment('front_wing');raw['experiment']['simulation_effect_s']=-2.0
        self.assertIn('must be null',workflow.experiment_problem(raw,{**base,'front_wing':21},base,{}))
    def test_car_fields_reach_the_sealed_design(self):
        document=json.loads(Path('assets/racing/template/design.json').read_text())
        base=seed('racing')
        for field,value,path in [('fuel_load_kg',9,('power_unit','fuel_load_kg')),('front_wing',23,('aero','front_wing')),('rear_wing',20.5,('aero','rear_wing'))]:
            book=clamp('racing',{field:value},base)
            car=design_document(document,book)['design']
            self.assertEqual(car[path[0]][path[1]],value)
    def test_local_trials_are_not_counted_as_played_results(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'race.json').write_text(json.dumps({'played_strategy':seed('racing'),'result':{'match_id':'race','recording':{'facts':{'track_id':'parkland','wet_race':False}}}}))
            state={'racing_experiments':[{'changed':['front_wing'],'candidate':{**seed('racing'),'front_wing':23}}]}
            ledger=workflow.coverage(state,Path(folder))
        self.assertEqual(ledger['fields']['front_wing']['played_values'],[20])
        self.assertEqual(ledger['fields']['front_wing']['local_trial_values'],[23])
    def test_scenario_grid_does_not_follow_latest_wetness(self):
        params={'lateral':20.,'brake':11.,'accel':9.,'speed_cap':90.,'energy_mj_per_s':.09,'safety_margin_m':.5}
        def optimize(path,parameters):return ({'estimated_lap_seconds':200.-parameters['lateral']}, {})
        with tempfile.TemporaryDirectory() as folder,patch.object(sim,'OUT',Path(folder)),patch.object(sim,'_params',return_value=params),patch.object(sim,'_kit',return_value=SimpleNamespace(optimize=optimize)):
            dry=sim._racing('parkland',{'recording':{'facts':{'wet_race':False,'max_track_wetness':0}}})
            wet=sim._racing('parkland',{'recording':{'facts':{'wet_race':True,'max_track_wetness':1}}})
            self.assertEqual([l for l in dry.splitlines() if 'shared context lap' in l],[l for l in wet.splitlines() if 'shared context lap' in l])
            result=sim.racing_assessment('parkland',seed('racing'),{**seed('racing'),'fuel_load_kg':9})
            self.assertFalse(result['rankable_by_simulation'])
            self.assertIsNone(result['predicted_delta_s'])
            self.assertEqual([r['wetness'] for r in result['shared_weather_grid']],[0.,.5,.95])
    def test_field_car_evidence_has_same_match_timing(self):
        events=[{'kind':'qualifying_lap','seats':[0],'payload':{'valid':True,'lap_time_s':100.}},
                {'kind':'qualifying_lap','seats':[1],'payload':{'valid':True,'lap_time_s':95.}},
                {'kind':'finished','seats':[0],'payload':{'position':2,'total_time_s':420.,'lap':4}},
                {'kind':'finished','seats':[1],'payload':{'position':1,'total_time_s':400.,'lap':4}}]
        f=recording._racing(events,0)
        rival=next(r for r in f['field_results'] if r['seat']==1)
        self.assertEqual(rival['qualifying_s'],95.)
        self.assertEqual(rival['finish_position'],1)

if __name__=='__main__':unittest.main()
