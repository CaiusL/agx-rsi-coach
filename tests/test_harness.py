import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from coach import loop
from coach.config import load
from tests.support import fps_state
from coach.strategies import seed
from coach.validate import check

class HarnessRegressionTests(unittest.TestCase):
    def test_recorded_finish_beats_rank_array(self):
        a={'rank':0,'recording':{'facts':{'finish_position':10}}}
        b={'rank':1,'recording':{'facts':{'finish_position':1}}}
        self.assertEqual(loop.choose('racing',a,b),'B')
        self.assertFalse(loop.we_won('racing',a))
        self.assertTrue(loop.we_won('racing',b))

    def test_team_win_cannot_promote_noncontributing_seat(self):
        a={'seat_score':0,'team_rounds':10,'recording':{'facts':{'rounds':20,'our_shots':20,'our_hits':10,'our_kills':3,'our_deaths':4}}}
        b={'seat_score':1,'team_rounds':13,'recording':{'facts':{'rounds':20,'our_shots':0,'our_deaths':12}}}
        self.assertEqual(loop.choose('fps',a,b),'A')

    def test_noop_retries_cannot_publish_unchanged_book(self):
        base=seed('racing')
        with patch.object(loop,'diagnose',return_value='diagnosis'),patch.object(loop,'chat_json',return_value=base),patch.object(loop,'chat_text',return_value=json.dumps(base)),patch.object(loop,'log'):
            with self.assertRaises(loop.OmlxError): loop.propose(load(),'racing',base,'facts')

    def test_implementation_receives_explicit_diagnosis(self):
        base=seed('racing'); proposal={**base,'kv':1.7}
        with patch.object(loop,'diagnose',return_value='VISIBLE_DIAGNOSIS_123'),patch.object(loop,'chat_json',return_value=proposal) as call:
            loop.propose(load(),'racing',base,'facts')
            self.assertIn('VISIBLE_DIAGNOSIS_123',str(call.call_args.args[2]))

    def test_inert_fps_change_does_not_count(self):
        base=seed('fps')
        self.assertTrue(loop._same_book(base,loop.effective_proposal('fps',{'execute_live_seconds':20},base)))

    def test_old_gate_pass_does_not_finish_refinement(self):
        state=fps_state()
        state['refine']={'deadline':None,'iters':0,'focus':state['A']['last']}
        state['code_best']['context']='old-fixture-or-match'
        with patch.object(loop,'evidence',return_value='facts'),patch.object(loop,'gate_best',return_value=(False,'FAIL expanded fixture')),patch.object(loop,'improve_code') as improve,patch.object(loop,'stage_plan_b') as upload,patch.object(loop,'save_state'):
            loop.refine_step(load(),'fps',state,None)
            improve.assert_called_once()
            upload.assert_not_called()

    def test_failed_refinement_does_not_upload(self):
        state=fps_state()
        state['refine']={'deadline':None,'iters':loop.REFINE_MAX_ITERS}; state['code_best']={}
        with patch.object(loop,'stage_plan_b') as upload,patch.object(loop,'save_state'):
            loop.refine_step(load(),'fps',state,None)
            upload.assert_not_called()

    def test_gate_rejects_previously_passing_stagger_bug(self):
        state=fps_state()
        code=Path('tests/fixtures/fps_departure_bug.py').read_text()
        ok,report=check('fps',code,state['A']['strategy'])
        self.assertFalse(ok)
        self.assertIn('FAIL departure_seat_0',report)
        self.assertIn('FAIL carrier_seat_4',report)
        self.assertIn('FAIL carrier_seat_9',report)

class RepairWorkflowTests(unittest.TestCase):
    def test_exact_patch_preserves_unedited_code(self):
        code = "x = 1\ny = 3\n"
        actual = loop.apply_code_edits(code, {"edits": [{"old": "x = 1", "new": "x = 2"}]})
        self.assertEqual(actual, "x = 2\ny = 3\n")

    def test_ambiguous_patch_rejected(self):
        with self.assertRaises(loop.OmlxError):
            loop.apply_code_edits("x = 1\nx = 1\n", {"edits": [{"old": "x = 1", "new": "x = 2"}]})

    def test_syntax_regression_rejected(self):
        with self.assertRaises(loop.OmlxError):
            loop.apply_code_edits("x = 1\n", {"edits": [{"old": "x = 1", "new": "if ("}]})

    def test_failed_manual_repair_preserves_candidate_and_never_uploads(self):
        state=fps_state()
        state['code_best']={'code':'retained partial repair','stalls':1}
        previous=copy.deepcopy(state['B'])
        def failed(*args, **kwargs): args[2]['plan_ready']=False
        with patch.object(loop,'write_plan',side_effect=failed),patch.object(loop,'stage_plan_b') as upload,patch.object(loop,'save_state'),patch.object(loop,'log'):
            self.assertFalse(loop.manual_improve(load(),'fps',state,None,upload=True))
        upload.assert_not_called()
        self.assertEqual(state['B'],previous)
        self.assertEqual(state['code_best']['code'],'retained partial repair')
        self.assertEqual(state['manual_result']['status'],'no_candidate')

    def test_upload_requires_current_gate_pass(self):
        state=fps_state()
        state['B']['live_hash']=None
        with patch.object(loop,'check',return_value=(False,'FAIL aiming: missed')),patch.object(loop,'quota_open') as quota,patch.object(loop,'upload') as upload,patch.object(loop,'log'):
            self.assertFalse(loop.ensure_live(load(),'fps',state,None,'B',True))
        quota.assert_not_called()
        upload.assert_not_called()

    def test_resume_uses_function_repair_and_failure_scoped_diagnosis(self):
        import tempfile
        state=fps_state()
        context=loop.candidate_context(state)
        report='PASS navigation: reached\nFAIL aiming: missed'
        state['code_best']={'context':context,'code':'UNCHANGED = 7\ndef repair():\n    return 1\n',
                            'book':state['A']['strategy'],'report':report,'score':0.5,'ok':False,'stalls':0}
        state['repair_diagnosis']={'context':context,'signature':loop.repair_signature(context,report),'text':'cached diagnosis'}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(loop,'ROOT',Path(tmp)),patch.object(loop,'STATE',Path(tmp)),patch.object(loop,'save_state'),patch.object(loop,'log'),patch.object(loop,'diagnose') as diagnose,patch.object(loop,'chat_text',return_value='```python\ndef repair():\n    return 2\n```') as function_call,patch.object(loop,'check',return_value=(False,report)):
                loop.improve_code(load(),'fps',state,'facts','FAIL navigation: stuck\nFAIL aiming: missed',force=True,max_attempts=1)
                diagnose.assert_not_called()
                self.assertEqual(function_call.call_args.kwargs['max_tokens'],5000)
                self.assertIn('return 2',state['code_best']['code'])
                self.assertIn('UNCHANGED = 7',state['code_best']['code'])

if __name__=='__main__': unittest.main()
