import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coach import loop, racing_match_data, racing_workflow
from coach.config import load
from coach.omlx import OmlxError

MID='24a0fa67-86c1-4f32-be26-afd5d4039a48'


def fixture():
    metadata={'game_id':'racing','rules_version':'rsi-racing-4.4','track':{'id':'test','length_m':1000},
              'cars':[{'seat':s,'design_summary':{'fuel_load_kg':12,'front_wing':23}} for s in (0,3)]}
    scene=[{'type':'initial_metadata','metadata':metadata},{'type':'terminal_metadata','metadata':metadata}]
    events=[{'tick':64,'phase':'race','kind':'retired','seats':[3],'payload':{'reason':'damage','lap':0}},
            {'tick':66,'phase':'race','kind':'finished','seats':[0],'payload':{'position':1,'total_time_s':1,'lap':4}},
            {'tick':66,'phase':'race','kind':'race_closed','seats':[],'payload':{}}]
    frames=[]
    for tick,phase,wet,status,fuel in [(0,'qualifying',0.,'racing',12.),(3,'qualifying',1.,'racing',11.),
        (60,'race',0.,'racing',12.),(63,'race',0.,'racing',11.7),(66,'race',1.,'retired',11.7)]:
        frames.append({'game_id':'racing','tick':tick,'time':tick/60,'phase':phase,
            'weather':{'condition':'wet' if wet else 'dry','track_wetness':wet},
            'cars':[{'seat':3,'status':status,'speed_mps':22.,'fuel_kg':fuel,'ers_soc_mj':2.,
                     'tyre_wear':[.01]*4,'compound':'soft','lap':0}]})
    return {'static':scene,'events':events,'frames':frames}


class RacingDataTests(unittest.TestCase):
    def analyze(self,payload):
        with tempfile.TemporaryDirectory() as tmp:
            files={k:Path(tmp)/k for k in payload}
            for kind,records in payload.items():
                raw=''.join(json.dumps(r)+'\n' for r in records).encode()
                files[kind].write_bytes(gzip.compress(raw) if kind=='frames' else raw)
            with patch.object(racing_match_data.training,'paths',return_value=(Path(tmp)/'absent',Path(tmp)/'absent-summary')):
                return racing_match_data.analyze(files,{'match_id':MID,'seat':3})

    def test_retirement_uses_own_weather_and_fuel_before_global_weather_changes(self):
        result=self.analyze(fixture());window=result['retirement_windows'][0]
        self.assertEqual(window['reported_reason'],'damage')
        self.assertTrue(all(p['wetness']==0 for p in window['preceding_public_samples']))
        self.assertEqual(window['preceding_public_samples'][-1]['fuel_kg'],11.7)
        self.assertEqual(result['winner_seat'],0)
        self.assertEqual(result['telemetry_by_seat_and_phase']['3']['race']['weather_seconds'],{'dry':.05})
        self.assertEqual(result['weather_by_phase']['race']['end'],'wet')

    def test_qualifying_and_race_fuel_are_not_mixed(self):
        result=self.analyze(fixture());phases=result['telemetry_by_seat_and_phase']['3']
        self.assertEqual(phases['qualifying']['fuel_min_kg'],11.)
        self.assertEqual(phases['race']['fuel_min_kg'],11.7)

    def test_missing_terminal_scene_and_incomplete_frames_fail(self):
        data=fixture();data['static'].pop()
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.analyze(data)
        data=fixture();data['frames'].pop()
        with self.assertRaisesRegex(ValueError,'Incomplete'):self.analyze(data)

    def test_backwards_frame_clock_is_rejected(self):
        data=fixture();data['frames'][2]['tick']=1
        with self.assertRaisesRegex(ValueError,'Non-monotonic'):self.analyze(data)

    def test_racing_diagnosis_cannot_omit_evidence_plan_and_uncertainty(self):
        answer=json.dumps({'comparison':'row A vs B','change':'one field','hypothesis':'unproven'})
        with patch.object(loop,'chat_text',return_value=answer):
            with self.assertRaisesRegex(OmlxError,'validation plan'):
                loop.diagnose(load(),'racing',[{'role':'user','content':'race evidence'}])

    def test_history_uses_actual_values_instead_of_strategy_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);record={'played_strategy':{'name':'fuel7-front20','fuel_load_kg':12,'front_wing':23},
                'result':{'match_id':MID,'created_at':'2026-11-15T04:00:00Z',
                          'recording':{'facts':{'track_id':'crossover','finished':False,
                          'retirements':[{'phase':'race','reason':'damage'}]}}}}
            (folder/(MID+'.json')).write_text(json.dumps(record));facts=racing_workflow.survival_evidence(folder)
            self.assertEqual(facts['tracks']['crossover']['not_finished'],1)
            self.assertEqual(facts['tracks']['crossover']['recent_runs'][0]['played_fields']['fuel_load_kg'],12)
            ledger=racing_workflow.coverage({},folder)
            self.assertTrue(ledger['survival_priority'])
            self.assertFalse(ledger['car_exploration_due'])
