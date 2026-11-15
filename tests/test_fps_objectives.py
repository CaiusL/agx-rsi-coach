import copy
from datetime import datetime,timedelta,timezone
import math
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coach import fps_objectives, loop, sessions
from coach.fps_compare import compare
from coach.fps_compare import load as load_agent
from coach.fps_tactics import base_observation
from coach.strategies import seed,render


class Probe:
    """Fixture positive control: public-site search, then remembered visible target."""
    def init(self,context):self.position=None
    def act(self,obs):
        bomb=obs.get('bomb') or {};me=obs['me']
        if bomb.get('state')!='planted':return {}
        if bomb.get('position'):self.position=bomb['position']
        goal=self.position or obs['map']['sites'][1]['center']
        dx,dy=goal[0]-me['x'],goal[1]-me['y']
        if self.position and math.hypot(dx,dy)<2.:
            return {'use':'defuse','move_forward':0.,'move_right':0.}
        error=(math.atan2(-dy,dx)-me['yaw']+math.pi)%(2*math.pi)-math.pi
        return {'yaw_rate':max(-1,min(1,error/.15)),'move_forward':.8 if abs(error)<.15 else 0.}


class ObjectiveTests(unittest.TestCase):
    def test_shipped_hotfix_completes_partial_information_retake_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'agent.py').write_text(loop.runtime_code('fps'))
            (root/'strategy.py').write_text(render(seed('fps')))
            policy=load_agent(root,'shipped_retake');checks=[]
            fps_objectives.objective_regressions(policy,lambda *r:checks.append(r))
            self.assertTrue(all(r[1] for r in checks),checks)

    def test_shipped_hotfix_does_not_defuse_at_previous_rounds_bomb(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'agent.py').write_text(loop.runtime_code('fps'))
            (root/'strategy.py').write_text(render(seed('fps')))
            policy=load_agent(root,'shipped_round_memory');policy.init({'contract_id':'rsi-fps-5.5','seat':0})
            obs=base_observation();obs['map']['sites'].append({'id':'B','center':[18.,0.,0.],'radius':4.})
            obs['bomb']={'state':'planted','position':[0.,0.,0.],'fuse_end_tick':4000}
            self.assertEqual(policy.act(copy.deepcopy(obs)).get('use'),'defuse')
            obs['round_index']=1;obs['bomb']={'state':'planted','fuse_end_tick':8000}
            obs['sounds']=[{'type':'bomb_beep','direction':'unknown','distance_band':'far'}]
            obs.pop('map');action=policy.act(copy.deepcopy(obs))
            self.assertNotEqual(action.get('use'),'defuse')
            self.assertGreater(abs(action.get('move_forward',0))+abs(action.get('move_right',0)),.1)

    def test_function_parser_ignores_illustrative_snippets_and_preserves_unrelated_source(self):
        code='UNCHANGED=7\ndef act(o):\n    return {}\n'
        reply='Explanation:\n```python\n    if condition:\n        pass\n```\nActual patch:\n```python\ndef act(o):\n    return {"move_forward":1}\n```'
        result=loop.apply_function_replacements(code,reply)
        self.assertIn('UNCHANGED=7',result);self.assertIn('"move_forward":1',result)
        with self.assertRaisesRegex(loop.OmlxError,'Duplicate'):
            loop.apply_function_replacements(code,'```python\ndef act(o):return {}\n```\n```python\ndef act(o):return {}\n```')

    def test_exact_edits_reject_unwired_helpers(self):
        code='def act(o):return {}\n'
        with self.assertRaisesRegex(loop.OmlxError,'never referenced'):
            loop.apply_code_edits(code,{'edits':[{'old':code,'new':code+'\ndef locate(o):return None\n'}]})
        wired='def act(o):return locate(o)\ndef locate(o):return {}\n'
        self.assertEqual(loop.apply_code_edits(code,{'edits':[{'old':code,'new':wired}]}),wired)

    def test_proven_objective_fixture_gain_can_pass_quality_when_trace_lacks_that_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folders=[root/'base',root/'candidate']
            for i,folder in enumerate(folders):
                folder.mkdir();(folder/'strategy.py').write_text('STRATEGY={}\n')
                (folder/'agent.py').write_text(f'fixed={bool(i)}\ndef init(c):pass\ndef act(o):return {{}}\n')
            path=root/'trace.jsonl';path.write_text(json.dumps({'observation':{'phase':'live','round_index':0,
                'match_tick':0,'me':{'alive':True,'x':0.,'y':0.},'visible':[]},'action':{}})+'\n')
            def objectives(policy,record):record('objective_hidden_search',policy.fixed,'probe')
            with patch('coach.fps_compare.objective_regressions',side_effect=objectives),patch('coach.fps_compare.tactical_regressions'),patch('coach.fps_compare.combat_regressions',side_effect=lambda p,r:r('combat',True,'probe')),patch('coach.fps_compare.Audit') as audit,patch('coach.fps_compare.quality_comparison',return_value=([],[])):
                audit.return_value.summary.return_value={'aim_samples':1,'aligned_ready_fire':1}
                quality=compare(*folders,[path],goal='quality')
                combat=compare(*folders,[path],goal='combat_kda')
            self.assertTrue(quality['ok'],quality['reasons']);self.assertEqual(quality['changed_actions'],0)
            self.assertIn('objective_execution',quality['gains'])
            self.assertFalse(combat['ok'])  # An explicit combat-only request keeps its stricter contract.

    def test_partial_observation_probes_accept_legal_search_and_remembered_bomb(self):
        checks=[];fps_objectives.objective_regressions(Probe(),lambda *r:checks.append(r))
        self.assertEqual(len(checks),3);self.assertTrue(all(c[1] for c in checks),checks)

    def test_blind_defuse_at_old_site_fails(self):
        class Blind(Probe):
            def act(self,obs):return {'use':'defuse'}
        checks=[];fps_objectives.objective_regressions(Blind(),lambda *r:checks.append(r))
        self.assertFalse(any(c[1] for c in checks))

    def test_recorded_audit_distinguishes_requests_and_does_not_invent_position(self):
        audit=fps_objectives.ObjectiveAudit();obs={'phase':'live','round_index':1,'side':'defend',
            'me':{'alive':True,'x':0.,'y':0.,'z':0.},'bomb':{'state':'planted','fuse_end_tick':3000}}
        for _ in range(30):audit.step(obs,{'use':'defuse'})
        result=audit.summary()['rounds']['1']
        self.assertEqual(result['defuse_without_current_position'],30)
        self.assertEqual(result['known_out_of_range_defuse'],0)
        self.assertEqual(result['max_displacement_m'],0.)

    def test_stalled_fps_schedules_fresh_diagnosis_and_retries_before_cutoff(self):
        now=datetime.now(timezone.utc);session=sessions.new({'start_at':(now+timedelta(hours=1)).isoformat()})
        session.update(no_progress=3,status='stalled',prompt_version='same')
        state={'session':session,'phase':'play_A','A':{},'B':{}}
        with patch.object(loop,'save_state'),patch.object(loop,'watch_manual_game'),patch.object(loop,'manual_improve'),patch.object(loop,'load_state',return_value={}),patch.object(loop.hashlib,'sha256') as digest,patch.object(loop.fps_evaluation,'corpus',return_value=[]):
            digest.return_value.hexdigest.return_value='same'
            loop.session_step(None,'fps',state,None)
            self.assertIn('strategy_retry_at',session)
            session['strategy_retry_at']=(now-timedelta(seconds=1)).isoformat()
            loop.session_step(None,'fps',state,None)
            self.assertEqual(session['strategy_retries'],1)
            self.assertEqual(state['fps_retry_generation'],1)
            self.assertEqual(session['attempts'],1)

    def test_fps_priority_is_independent_of_spent_time_and_due_upload_still_wins(self):
        now=datetime.now(timezone.utc)
        states={g:{'session':sessions.new({'start_at':(now+timedelta(hours=1)).isoformat()})} for g in ('fps','racing')}
        states['racing']['session']['model_work_seconds']=1800
        states['fps']['session']['model_work_seconds']=120
        with patch.object(loop,'load_state',side_effect=lambda g:states[g]):
            self.assertEqual(loop.game_order([],now),['fps','racing'])
            states['fps']['session']['model_work_seconds']=10000
            states['racing']['session']['model_work_seconds']=0
            self.assertEqual(loop.game_order([],now),['fps','racing'])
            states['racing']['session']['upload_at']=(now-timedelta(seconds=1)).isoformat()
            self.assertEqual(loop.game_order([],now),['racing','fps'])

    def test_fps_has_no_cycle_budget_only_the_scheduled_release_cutoff(self):
        now=datetime.now(timezone.utc);session=sessions.new({'start_at':(now+timedelta(hours=1)).isoformat()})
        state={'session':session,'phase':'play_A','A':{},'B':{}}
        with patch.object(loop,'save_state'),patch.object(loop,'watch_manual_game'),patch.object(loop,'manual_improve'),patch.object(loop,'load_state',return_value={}),patch.object(loop.fps_evaluation,'corpus',return_value=[]),patch.object(sessions,'until') as until:
            loop.session_step(None,'fps',state,None)
            self.assertEqual(until.call_args.args[0],session['model_stop_at'])
            self.assertFalse(session['cycle_budget_enforced'])
            self.assertIsNone(session['cycle_budget_seconds'])


if __name__=='__main__':unittest.main()
