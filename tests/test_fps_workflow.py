import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from coach import fps_evaluation, fps_reference, fps_workflow, loop
from coach import recording
from coach.fps_quality import Audit, fit, comparison
from coach.gate_fps import Seat, trigger_cycle, action_error
from coach.config import load


class WorkflowTests(unittest.TestCase):
    def test_uploaded_session_recovers_when_agx_failed_to_create_its_match(self):
        now=datetime.now(timezone.utc);target=now-timedelta(hours=1)
        session={'id':'uploaded-midnight','enabled':True,'status':'uploaded','round_at':target.isoformat(),
            'upload_at':(target-timedelta(minutes=12)).isoformat(),'source_match':'old-result','upload_started':target.isoformat()}
        state={'phase':'play_B','A':{},'B':{'strategy':{'name':'admitted'},'code':'policy',
            'last':{'match_id':'old-result','created_at':(target-timedelta(hours=2)).isoformat()}},
            'session':session,'loss_streak':2}
        future={'start_at':(now+timedelta(hours=1)).isoformat()}
        def observe(*args):
            state['round_observation']={'observed_at':now.isoformat(),'round_state':'failed','match_assigned':False,'next_round':future}
            return 'AGX scheduling failed'
        with patch.object(loop,'watch_manual_game',side_effect=observe),patch.object(loop,'save_state'),patch.object(loop,'round_payload') as request,patch.object(loop.fps_evaluation,'corpus',return_value=[]):
            loop.session_step(load(),'fps',state,None)
        request.assert_not_called()
        self.assertEqual(state['session']['round_at'],future['start_at'])
        self.assertEqual(state['session']['previous_void_session'],'uploaded-midnight')
        self.assertEqual(state['session']['source_match'],'old-result')
        self.assertEqual(state['loss_streak'],2)

    def test_running_unassigned_round_does_not_trigger_void_recovery(self):
        now=datetime.now(timezone.utc);target=now-timedelta(hours=1)
        state={'phase':'play_B','A':{},'B':{'last':{}},'session':{
            'id':'waiting','enabled':True,'status':'uploaded','round_at':target.isoformat(),
            'upload_started':target.isoformat(),'source_match':'old'},
            'round_observation':{'observed_at':now.isoformat(),'round_state':'running','match_assigned':False,
                'next_round':{'start_at':(now+timedelta(hours=1)).isoformat()}}}
        with patch.object(loop,'watch_manual_game',return_value='waiting'),patch.object(loop,'save_state'),patch.object(loop,'round_payload') as request,patch.object(loop.fps_evaluation,'corpus',return_value=[]):
            loop.session_step(load(),'fps',state,None)
        request.assert_not_called();self.assertEqual(state['session']['id'],'waiting')

    def test_contract_repair_cannot_override_actual_regressions(self):
        result={'ok':False,'calls':100,'holdout_traces':2,'runtime_errors':[0,0],'gains':[],
            'reasons':['No demonstrated behavior change: identical actions'],'regressions':[]}
        fixed=fps_evaluation.contract_repair(result,False,True)
        self.assertTrue(fixed['ok']);self.assertEqual(fixed['status'],'offline_contract_repair')
        self.assertFalse(fps_evaluation.contract_repair({**result,'regressions':['holdout:aim_control']},False,True)['ok'])
        self.assertFalse(fps_evaluation.contract_repair(result,True,True)['ok'])
        self.assertFalse(fps_evaluation.contract_repair({**result,'runtime_errors':[0,1]},False,True)['ok'])

    def test_failed_scheduler_round_can_resume_only_on_a_published_future_window(self):
        now=datetime.now(timezone.utc)
        state={'phase':'play_B','B':{'strategy':{'name':'live'},'code':'policy',
            'last':{'match_id':'old-played-result'}},'A':{},'session':{
                'id':'void-session','enabled':True,'status':'missed_window','void_round_observed':True,
                'round_at':(now-timedelta(hours=2)).isoformat(),'upload_at':(now-timedelta(hours=2,minutes=12)).isoformat(),
                'source_match':'old-played-result'}}
        published={'start_at':(now+timedelta(hours=1)).isoformat()}
        with patch.object(loop,'watch_manual_game',return_value='No new played result'),patch.object(loop,'round_payload',return_value={'next_round':published}),patch.object(loop,'save_state'):
            loop.session_step(load(),'fps',state,None)
        self.assertEqual(state['session']['round_at'],published['start_at'])
        self.assertEqual(state['session']['source_match'],'old-played-result')
        self.assertEqual(state['session']['previous_void_session'],'void-session')
        self.assertNotIn('loss_streak',state)

    def test_reasoning_only_reply_gets_small_structured_decision_call(self):
        decision={'comparison':'Observed five shots versus 285 ineffective held-trigger requests.',
            'change':'Inspect fight and the current trigger condition.', 'hypothesis':'Test trigger readiness; verify on subsequent real match.'}
        with patch.object(loop,'chat_text',return_value='<think>Evidence review</think>') as reasoning,patch.object(loop,'chat_json',return_value=decision) as extraction:
            result=json.loads(loop.diagnose(load(),'fps',[{'role':'user','content':'Current verified evidence'}]))
        self.assertEqual(result,decision);self.assertTrue(reasoning.call_args.kwargs['think'])
        self.assertFalse(extraction.call_args.kwargs['think']);self.assertLess(extraction.call_args.kwargs['max_tokens'],2000)

    def test_invalid_actions_are_not_silently_clipped_or_coerced(self):
        for action in ({'fire':1},{'yaw_rate':float('nan')},{'pitch_rate':'0'},{'move_forward':2},{'weapon_slot':True},{'use':'plant-anywhere'}):
            self.assertIsNotNone(action_error(action),action)
        self.assertIsNone(action_error({'fire':False,'move_forward':.5,'weapon_slot':1}))

    def test_plant_attempts_are_not_counted_as_completed_plants(self):
        events=[{'type':'plant_start','player':0,'round_index':0},
            {'type':'plant_cancel','player':0,'round_index':0},
            {'type':'plant_start','player':0,'round_index':0},
            {'type':'plant_complete','round_index':0,'item_id':'bomb','match_tick':300},
            {'type':'plant_complete_detail','player':0,'round_index':0,'item_id':'bomb','match_tick':300,'site':'A'},
            {'type':'buy','seat':0,'kind':'galilar'},
            {'type':'round_end','round_index':0,'round_wins':[1,0]}]
        facts=recording._fps(events,0,0)
        self.assertEqual(facts['our_plant_attempts'],2);self.assertEqual(facts['our_plants'],1)
        self.assertEqual(facts['field_plants'],1);self.assertEqual(facts['our_purchases'],{'galilar':1})

    def test_observed_results_are_idempotent_and_not_causal_validation(self):
        state={};summary={'status':'succeeded','match_id':'match','training':{'version_id':'played-id'},
            'recording':{'facts':{'our_kills':3,'our_deaths':2}}}
        for _ in range(2):fps_workflow.observe(state,summary,{'name':'played'})
        self.assertEqual(len(state['fps_observed_results']),1)
        self.assertFalse(state['fps_observed_results'][0]['causal_improvement_proven'])

    def test_held_usp_trigger_does_not_create_fictitious_shots(self):
        seat=Seat('attack',0,[None,{'id':'usp','ammo':20},{'id':'knife'}])
        for _ in range(20):seat.apply({'fire':True},{'state':'dropped'},None)
        self.assertEqual(seat.shots,1)
        seat.apply({'fire':False},{'state':'dropped'},None)
        seat.apply({'fire':True},{'state':'dropped'},None)
        self.assertEqual(seat.shots,2)

    def test_trigger_fixture_rejects_constant_fire_but_accepts_trigger_cycles(self):
        class Policy:
            def init(self,ctx):self.step=0
            def act(self,obs):self.step+=1;return {'fire':True}
        checks=[];trigger_cycle(Policy(),lambda name,ok,detail:checks.append(ok))
        self.assertEqual(checks,[False])
        class Cycling(Policy):
            def act(self,obs):self.step+=1;return {'fire':self.step%2==1}
        checks=[];trigger_cycle(Cycling(),lambda name,ok,detail:checks.append(ok))
        self.assertEqual(checks,[True])

    def test_fixture_uses_physics_ticks_and_observed_pitch_rate(self):
        seat=Seat('attack',0,[None,{'id':'pistol','ammo':20},{'id':'knife'}])
        self.assertEqual(seat.observation(20,'live',{},None,[])['match_tick'],60)
        seat.apply({'pitch_rate':1},{'state':'dropped'},None)
        self.assertAlmostEqual(seat.pitch,.1)

    def test_turn_calibration_uses_applied_controls(self):
        rows=[]
        for i in range(12):
            rows.append({'observation':{'phase':'live','round_index':0,'match_tick':i*3,
                'me':{'alive':True,'yaw':i*.075,'pitch':i*.05}},
                'action':{'yaw_rate':-1,'pitch_rate':-1},'applied_action':{'yaw_rate':.5,'pitch_rate':.5}})
        result=fit(rows)
        self.assertAlmostEqual(result['yaw'],3);self.assertAlmostEqual(result['pitch'],2)

    def test_quality_does_not_reward_bad_or_conflicting_fire(self):
        c={'dt':.05,'yaw':3.,'pitch':2.,'release_required_weapons':['usp']}
        obs={'phase':'live','match_tick':30,'me':{'alive':True,'yaw':0.,'pitch':0.,'ammo':20,'active_slot':1,
            'weapons':[None,{'id':'usp','trigger_state':{'pressed':True}}]},'visible':[{'x':10.,'y':0.,'z':0.}]}
        audit=Audit(c);audit.step(obs,{'fire':True});s=audit.summary()
        self.assertEqual(s['aligned_ready_fire'],0);self.assertEqual(s['trigger_blocked_fire'],1)
        audit=Audit(c);audit.step(obs,{'fire':True,'reload':True})
        self.assertEqual(audit.summary()['conflicting_fire'],1)

    def test_more_firing_alone_does_not_improve_quality(self):
        b={'mean_aim_error_rad':.1,'aligned_ready_fire':4,'conflicting_fire':0,'unsafe_fire':5,'unsafe_fire_fraction':.5}
        c={**b,'aligned_ready_fire':40}
        self.assertEqual(comparison(b,c),([],[]))
        c={**c,'unsafe_fire':9,'unsafe_fire_fraction':.8}
        self.assertIn('unsafe_fire',comparison(b,c)[1])

    def test_fewer_bad_requests_with_same_useful_responses_is_not_a_ratio_regression(self):
        baseline={'mean_aim_error_rad':.04,'aligned_ready_fire':4,'conflicting_fire':0,'unsafe_fire':25,'unsafe_fire_fraction':25/38}
        candidate={**baseline,'unsafe_fire':12,'unsafe_fire_fraction':12/16}
        gains,regressions=comparison(baseline,candidate)
        self.assertEqual(gains,['fire_discipline']);self.assertEqual(regressions,[])

    def test_holdout_examples_do_not_enter_model_report(self):
        report=fps_evaluation.report({'ok':False,'traces':[
            {'trace':'dev','split':'development','blocked_examples':[{'position':'visible-example'}]},
            {'trace':'validation','split':'holdout','blocked_examples':[{'position':'SECRET_HELDOUT_CASE'}],
             'combat_examples':[{'step':'SECRET_HELDOUT_CASE'}]}]})
        self.assertIn('visible-example',report);self.assertNotIn('SECRET_HELDOUT_CASE',report)

    def test_public_research_does_not_treat_team_winners_as_known_strategies(self):
        leaders=[{'display_name':'leader'}]
        rows=[{'id':'void','status':'failed','created_at':'3','participants':[{'display_name':'leader'}]},
              {'id':'latest','status':'succeeded','created_at':'2','participants':[{'display_name':'leader'}]},
              {'id':'old','status':'succeeded','created_at':'1','participants':[{'display_name':'leader'}]}]
        self.assertEqual([r['id'] for r in fps_reference.select_matches(rows,leaders)],['latest'])

    def test_independent_experiment_uses_live_anchor_and_retains_partial_repair(self):
        state={'phase':'play_B','A':{'strategy':{'name':'older'}},
               'B':{'strategy':{'name':'live'},'code':'live policy','live_hash':'live-id','last':{}},
               'draft':{'id':'unplayed','ready':True,'strategy':{'name':'speculative'},'code':'unplayed policy'},
               'code_best':{'code':'unplayed policy','ok':True}}
        captured=[]
        def inspect(cfg,game,work,*args):
            captured.append((work['A']['code'],work['code_best']));work['plan_ready']=False
        with patch.object(loop,'write_plan',side_effect=inspect),patch.object(loop,'save_state'),patch.object(loop,'log'):
            loop.manual_improve(load(),'fps',state,None)
        self.assertEqual(captured,[('live policy',None)])
        self.assertEqual(state['draft']['id'],'unplayed')


if __name__=='__main__':unittest.main()
