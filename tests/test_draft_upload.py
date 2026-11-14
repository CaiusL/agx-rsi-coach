import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from coach import loop
from coach.strategies import seed


def state_for(game='fps'):
    book=seed(game)
    return {'phase':'play_B','A':{'strategy':copy.deepcopy(book),'code':'def act(o): return {}\n','last':{}},
            'B':{'strategy':copy.deepcopy(book),'code':'def act(o): return {}\n','last':{},'live_hash':'live-version','seen':[]},
            'code_best':{},'generation':1,'history':[]}

class DraftUploadTests(unittest.TestCase):
    def setUp(self):
        self.cfg=SimpleNamespace()
    def test_improve_never_uploads_even_if_legacy_upload_argument_is_true(self):
        s=state_for();before=copy.deepcopy(s['B'])
        def generate(cfg,game,work,*args):
            work['B']['strategy']['name']='draft-one';work['B']['code']='def act(o): return {"move_forward":1}\n'
            work['plan_ready']=True;work['code_best']={'report':'PASS syntax: ok','ok':True}
        with patch.object(loop,'write_plan',side_effect=generate),patch.object(loop,'check',return_value=(True,'PASS syntax: ok')),patch.object(loop,'ensure_live') as upload,patch.object(loop,'save_state'),patch.object(loop,'log'):
            result=loop.manual_improve(self.cfg,'fps',s,None,upload=True)
        self.assertFalse(result);upload.assert_not_called()
        self.assertEqual(s['B'],before)
        self.assertEqual(s['draft']['strategy']['name'],'draft-one')
        self.assertEqual(s['manual_result']['status'],'no_upload')
        self.assertTrue(s['manual_review'])
    def test_repeated_improvements_refine_latest_local_draft(self):
        s=state_for('racing');before=copy.deepcopy(s['A']);bases=[]
        def generate(cfg,game,work,*args):
            bases.append(work['A']['strategy']['kv'])
            work['B']['strategy']={**work['A']['strategy'],'kv':round(work['A']['strategy']['kv']+.1,1),'name':f'draft-{len(bases)}'}
            work['plan_ready']=True
        with patch.object(loop,'write_plan',side_effect=generate) as model,patch.object(loop,'check',return_value=(True,'validated book')),patch.object(loop,'ensure_live') as upload,patch.object(loop,'save_state'),patch.object(loop,'log'):
            loop.manual_improve(self.cfg,'racing',s,None)
            loop.manual_improve(self.cfg,'racing',s,None)
        self.assertEqual(bases,[1.6,1.7]);self.assertEqual(model.call_count,2)
        self.assertEqual(s['draft']['revision'],2);self.assertEqual(s['A'],before)
        upload.assert_not_called()
    def test_failed_improvement_keeps_previous_ready_draft(self):
        s=state_for();s['draft']={'strategy':seed('fps'),'code':'old draft','ready':True,'revision':4}
        before=copy.deepcopy(s['draft'])
        def fail(cfg,game,work,*args):work['plan_ready']=False
        with patch.object(loop,'write_plan',side_effect=fail),patch.object(loop,'save_state'),patch.object(loop,'log'):
            loop.manual_improve(self.cfg,'fps',s,None)
        self.assertEqual(s['draft'],before)
        self.assertEqual(s['manual_result']['status'],'no_candidate')
    def test_upload_uses_exact_draft_without_generating(self):
        s=state_for();s['draft']={'strategy':{**seed('fps'),'name':'selected'},'code':'exact selected bytes','ready':True}
        selected=loop.draft_id(s['draft'])
        def submit(cfg,game,state,client,slot,**kwargs):
            self.assertEqual(state['B']['code'],'exact selected bytes')
            self.assertFalse(kwargs['regenerate_on_rejection'])
            state['B']['live_hash']=selected;state['B']['active_from']='next-round';return True
        with patch.object(loop,'check',return_value=(True,'PASS syntax: ok')),patch.object(loop,'ensure_live',side_effect=submit) as upload,patch.object(loop,'write_plan') as generate,patch.object(loop,'chat_text') as model,patch.object(loop,'save_state'),patch.object(loop,'log'):
            self.assertTrue(loop.upload_draft(self.cfg,'fps',s,None,selected))
        generate.assert_not_called();model.assert_not_called();upload.assert_called_once()
        self.assertEqual(s['draft']['uploaded_id'],selected)
    def test_changed_draft_id_never_uploads(self):
        s=state_for();s['draft']={'strategy':seed('fps'),'code':'draft','ready':True};before=copy.deepcopy(s['B'])
        with patch.object(loop,'ensure_live') as upload,patch.object(loop,'save_state'),patch.object(loop,'log'):
            self.assertFalse(loop.upload_draft(self.cfg,'fps',s,None,'old-id'))
        upload.assert_not_called();self.assertEqual(s['B'],before)
    def test_rejected_upload_preserves_live_slot_and_draft(self):
        s=state_for();s['draft']={'strategy':seed('fps'),'code':'draft','ready':True};before=copy.deepcopy(s['B'])
        with patch.object(loop,'check',return_value=(True,'PASS syntax: ok')),patch.object(loop,'ensure_live',return_value=False),patch.object(loop,'save_state'),patch.object(loop,'log'):
            self.assertFalse(loop.upload_draft(self.cfg,'fps',s,None,loop.draft_id(s['draft'])))
        self.assertEqual(s['B'],before);self.assertTrue(s['draft']['ready'])
    def test_draft_workspace_cannot_overwrite_live_state(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(loop,'STATE',Path(folder)):
            s=state_for();loop.save_state('fps',s)
            work=copy.deepcopy(s);work['_draft_workspace']=True;work['A']['strategy']['name']='unpublished'
            loop.save_state('fps',work)
            self.assertNotEqual(json.loads((Path(folder)/'fps.json').read_text())['A']['strategy']['name'],'unpublished')
            self.assertTrue((Path(folder)/'draft_work_fps.json').exists())
    def test_manual_control_polls_without_automatic_generation(self):
        s=state_for();s['manual_review']=True
        cfg=SimpleNamespace(account_key=lambda g,slot:'test' if slot=='A' else '',base_url='http://unused')
        with tempfile.TemporaryDirectory() as folder,patch.object(loop,'STATE',Path(folder)),patch.object(loop,'load_state',return_value=s),patch.object(loop,'watch_manual_game',return_value='watching') as watch,patch.object(loop,'tick_single') as automatic,patch.object(loop,'Agx'):
            self.assertEqual(loop.tick(cfg,'fps'),'watching')
        watch.assert_called_once();automatic.assert_not_called()

if __name__=='__main__':unittest.main()
