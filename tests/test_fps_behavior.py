import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime,timedelta,timezone
import hashlib
from concurrent.futures import Future
from coach import loop
from coach import sessions,training
from coach.fps_signals import Contexts, Responses, action_key
from coach.fps_compare import compare
from coach.agx import Agx,AgxError
from coach.gate_fps import Seat


def row(tick,**me):
    return {'seat':0,'step':tick,'observation':{'phase':'live','round_index':0,'match_tick':tick*3,
        'side':'attack','me':{'alive':True,'x':0.,'y':0.,'z':0.,'active_slot':0,**me}},'action':{'move_forward':1}}


class BehaviorTests(unittest.TestCase):
    def test_pistol_fixture_has_ammo_and_honors_active_slot(self):
        seat=Seat('attack',0,[None,{'id':'pistol','ammo':20},{'id':'knife'}])
        self.assertEqual(seat.ammo(),20)
        seat.apply({'weapon_slot':1,'fire':True},{'state':'held'},None)
        self.assertEqual(seat.ammo(),19);self.assertEqual(seat.shots,1)
    def test_neutral_fields_and_roundoff_do_not_count_as_behavior_changes(self):
        obs=row(0)['observation']
        self.assertEqual(action_key({},obs),action_key({'move_forward':0.,'fire':False,'use':'none','weapon_slot':0,'yaw_rate':.00001},obs))

    def test_recorded_immobilization_has_one_episode_and_a_response_proxy(self):
        contexts=Contexts();responses=Responses()
        for i in range(80):
            context=contexts.step(row(i));responses.step({'move_forward':1},context)
        self.assertEqual(len(contexts.opportunities['blocked_responses']),1)
        self.assertEqual(responses.summary()['blocked_responses'],0)
        responses.step({'move_right':1},context)
        self.assertEqual(responses.summary()['blocked_responses'],1)

    def test_vertical_progress_is_not_immobilization(self):
        contexts=Contexts()
        for i in range(80):contexts.step(row(i,z=i*.1))
        self.assertEqual(len(contexts.opportunities['blocked_responses']),0)

    def test_no_plant_opportunity_for_a_noncarrier(self):
        contexts=Contexts();r=row(0,carrying_bomb=False)
        r['observation']['map']={'sites':[{'center':[0,0,0],'radius':4}]}
        self.assertIsNone(contexts.step(r)['plant'])
        r=row(1,carrying_bomb=True)
        self.assertIsNotNone(contexts.step(r)['plant'])

    def test_zero_plants_without_carrier_evidence_is_not_an_objective_fault(self):
        focus={'recording':{'facts':{'attack_rounds':12,'our_plants':0,'field_plants':9}},
               'training':{'status':'verified','counts':{'carrier_calls':0}}}
        self.assertEqual(loop.combat_fault(focus),'')

    def test_reused_book_names_get_a_bounded_model_authored_name_only_retry(self):
        from coach.config import load
        from coach.strategies import seed
        book=seed('fps')
        with patch.object(loop,'admitted_names',return_value={book['name']}),patch.object(loop,'log'),patch.object(loop,'chat_json',side_effect=[{'name':book['name']},{'name':book['name']},{'name':'recovery-trial-unique'}]) as model:
            result=loop.code_book(load(),'fps',book,'def act(o):return {}','PASS syntax: ok')
        self.assertEqual(result['name'],'recovery-trial-unique')
        self.assertEqual(model.call_args.kwargs['max_tokens'],100)

    def test_paired_evaluator_rejects_cosmetic_change_and_accepts_recovery_response(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'base';candidate=root/'candidate';base.mkdir();candidate.mkdir()
            for p in (base,candidate):(p/'strategy.py').write_text('STRATEGY = {}\n')
            code='def init(c): pass\ndef act(o): return {"move_forward":1}\n'
            (base/'agent.py').write_text(code);(candidate/'agent.py').write_text('# renamed version\n'+code)
            trace=root/'trace.jsonl'
            rows=[{'type':'metadata','rules_version':'rsi-fps-5.5','participants':[{'seat':0}]}]+[row(i) for i in range(80)]
            trace.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            result=compare(base,candidate,[trace])
            self.assertFalse(result['ok']);self.assertEqual(result['changed_actions'],0)
            (candidate/'agent.py').write_text('def init(c): pass\ndef act(o): return {"move_right":1}\n')
            result=compare(base,candidate,[trace])
            self.assertTrue(result['ok']);self.assertEqual(result['candidate']['blocked_responses'],1)

    def test_socket_timeout_is_reported_as_an_infrastructure_error(self):
        with patch('coach.agx.urllib.request.urlopen',side_effect=TimeoutError('read timeout')),patch('coach.agx.time.sleep'):
            with self.assertRaises(AgxError):Agx('https://example.invalid').get('/matches')

    def test_paired_policies_do_not_share_strategy_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'base';candidate=root/'candidate';base.mkdir();candidate.mkdir()
            code='def init(c): pass\ndef act(o):\n import strategy\n return {"move_forward":strategy.STRATEGY["speed"]}\n'
            for target,speed in ((base,1),(candidate,-1)):
                (target/'agent.py').write_text(code);(target/'strategy.py').write_text(f'STRATEGY = {{"speed":{speed}}}\n')
            rows=[{'type':'metadata','rules_version':'rsi-fps-5.5','participants':[{'seat':0}]}]+[row(i) for i in range(80)]
            trace=root/'trace.jsonl';trace.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            result=compare(base,candidate,[trace])
        self.assertEqual(result['changed_actions'],80)
        self.assertTrue(result['ok'])

    def test_gain_on_one_map_cannot_hide_regression_on_another(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'base';candidate=root/'candidate';base.mkdir();candidate.mkdir()
            for p in (base,candidate):(p/'strategy.py').write_text('STRATEGY = {}\n')
            (base/'agent.py').write_text('def init(c): pass\ndef act(o): return {"move_right":1} if o["me"]["marker"]=="map2" else {"move_forward":1}\n')
            (candidate/'agent.py').write_text('def init(c): pass\ndef act(o): return {"move_right":1} if o["me"]["marker"]=="map1" else {"move_forward":1}\n')
            traces=[]
            for marker,episodes in (('map1',3),('map2',1)):
                rows=[{'type':'metadata','rules_version':'rsi-fps-5.5','participants':[{'seat':0}]}]
                for i in range(episodes*80):rows.append(row(i,x=float(i//80),marker=marker))
                p=root/(marker+'.jsonl');p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n');traces.append(p)
            result=compare(base,candidate,traces)
        self.assertGreater(result['candidate']['blocked_responses'],result['baseline']['blocked_responses'])
        self.assertFalse(result['ok']);self.assertIn('map2.jsonl:blocked_responses',result['regressions'])

    def test_network_error_does_not_consume_no_progress_budget(self):
        session=sessions.new({'start_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
        session['prompt_version']=hashlib.sha256(Path(loop.__file__).with_name('prompts.py').read_bytes()).hexdigest()[:16]
        session['no_progress']=2
        state={'session':session,'phase':'play_A','A':{'last':{}},'B':{}}
        with patch.object(loop,'load_state',return_value={}),patch.object(loop,'save_state'),patch.object(loop,'watch_manual_game',side_effect=AgxError('temporary connection failure')):
            loop.session_step(None,'fps',state,None)
        self.assertEqual(session['no_progress'],2)
        self.assertEqual(session['infrastructure_errors'],1)
        self.assertEqual(session['status'],'retrying_data')

    def test_export_fetch_is_queued_once_without_blocking_model_loop(self):
        future=Future()
        class Worker:
            calls=0
            def submit(self,*args):self.calls+=1;return future
        worker=Worker();summary={'match_id':'e81a049f-04bd-435f-9c45-4520abd37944','seat':2,'status':'succeeded'}
        with tempfile.TemporaryDirectory() as folder,patch.object(training,'STORE',Path(folder)),patch.object(training,'_executor',worker),patch.object(training,'_pending',{}):
            self.assertEqual(training.read(None,'fps',summary,background=True)['status'],'preparing')
            training.read(None,'fps',summary,background=True)
            self.assertEqual(worker.calls,1)
            future.set_result({'status':'verified'})
            self.assertEqual(training.read(None,'fps',summary,background=True)['status'],'verified')

    def test_new_session_waits_for_a_match_from_its_target_round(self):
        from coach.strategies import seed
        now=datetime.now(timezone.utc);target=now-timedelta(minutes=10)
        session=sessions.new({'start_at':(now+timedelta(hours=1)).isoformat()})
        session.update(round_at=target.isoformat(),upload_started=target.isoformat(),status='uploaded',source_match='initial')
        state={'phase':'play_A','session':session,'A':{'strategy':seed('fps'),'last':{'match_id':'old-but-newly-recorded','created_at':(target-timedelta(hours=1)).isoformat()}},'B':{}}
        with patch.object(loop,'save_state'),patch.object(loop,'watch_manual_game',return_value='waiting'),patch.object(loop,'round_payload') as rounds:
            loop.session_step(None,'fps',state,None)
        rounds.assert_not_called();self.assertEqual(state['session']['id'],session['id'])


if __name__=='__main__':unittest.main()
