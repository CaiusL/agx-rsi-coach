import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from coach import loop, manual_jobs, ui

class ManualJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patchers=[patch.object(manual_jobs,'STATE',self.root),patch.object(loop,'STATE',self.root),patch.object(ui,'STATE',self.root)]
        for p in self.patchers:p.start()
    def tearDown(self):
        for p in reversed(self.patchers):p.stop()
        self.tmp.cleanup()
    def handler(self,path,payload):
        h=object.__new__(ui.Handler);h.path=path
        raw=json.dumps(payload).encode();h.rfile=io.BytesIO(raw)
        h.headers={'Content-Length':str(len(raw))};responses=[]
        h._json=lambda body,status=200:responses.append((status,body))
        with patch.object(ui,'_token_ok',return_value=True):ui.Handler.do_POST(h)
        return responses[-1]
    def test_fps_route_only_creates_fps_request(self):
        status,result=self.handler('/api/games/fps/improve',{'game':'fps','upload':False,'request_id':'fps-only'})
        self.assertEqual(status,200)
        self.assertEqual(result['game'],'fps')
        self.assertTrue((self.root/'manual_improve_fps.json').exists())
        self.assertFalse((self.root/'manual_improve_racing.json').exists())
    def test_conflicting_url_and_payload_never_queue_either_game(self):
        status,_=self.handler('/api/games/fps/improve',{'game':'racing','upload':True})
        self.assertEqual(status,400)
        self.assertEqual(list(self.root.glob('manual_improve_*.json')),[])
    def test_repeated_click_does_not_replace_pending_job(self):
        first=manual_jobs.enqueue('fps',False,'first')
        second=manual_jobs.enqueue('fps',True,'second')
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['request_id'],'first')
        self.assertFalse(second['upload'])
    def test_running_job_prevents_second_queue(self):
        manual_jobs.enqueue('fps',False,'first');req=manual_jobs.claim('fps')
        duplicate=manual_jobs.enqueue('fps',True,'second')
        self.assertTrue(duplicate['duplicate'])
        self.assertFalse((self.root/'manual_improve_fps.json').exists())
        manual_jobs.finish(req)
        self.assertIsNone(manual_jobs.running('fps'))
    def test_fps_consumer_cannot_execute_racing(self):
        manual_jobs.enqueue('fps',False,'one')
        cfg=SimpleNamespace(account_key=lambda g,s:'test-key' if s=='A' else '',base_url='http://unused')
        with patch.object(loop,'load_state',return_value={}),patch.object(loop,'manual_improve') as improve,patch.object(loop,'Agx'),patch.object(loop,'log'):
            loop.tick(cfg,'fps')
        self.assertEqual(improve.call_args.args[1],'fps')
        self.assertEqual(improve.call_count,1)
        self.assertEqual(improve.call_args.kwargs['request_id'],'one')
        events=[json.loads(l) for l in (self.root/'manual_requests.jsonl').read_text().splitlines()]
        self.assertEqual([r['event'] for r in events],['queued','started','finished'])
        self.assertEqual({r['game'] for r in events},{'fps'})
    def test_misrouted_queue_is_rejected(self):
        (self.root/'manual_improve_racing.json').write_text(json.dumps({'game':'fps','request_id':'wrong','upload':False}))
        self.assertIsNone(manual_jobs.claim('racing'))
        self.assertIsNone(manual_jobs.running('racing'))

    def test_legacy_improve_request_cannot_request_upload(self):
        status,result=self.handler('/api/improve',{'game':'fps','upload':True,'request_id':'old-client'})
        self.assertEqual(status,200)
        self.assertEqual(result['action'],'improve')
        self.assertFalse(result['upload'])
    def test_upload_endpoint_queues_exact_selected_snapshot(self):
        from coach.strategies import seed
        draft={'strategy':seed('fps'),'code':'selected bytes','ready':True}
        selected=loop.draft_id(draft)
        (self.root/'fps.json').write_text(json.dumps({'draft':draft}))
        status,result=self.handler('/api/games/fps/upload',{'game':'fps','draft_id':selected,'request_id':'upload-only'})
        self.assertEqual(status,200)
        self.assertEqual(result['action'],'upload')
        self.assertEqual(result['draft_id'],selected)
        self.assertTrue(result['upload'])
    def test_upload_endpoint_rejects_stale_snapshot(self):
        from coach.strategies import seed
        (self.root/'fps.json').write_text(json.dumps({'draft':{'strategy':seed('fps'),'code':'new bytes','ready':True}}))
        status,_=self.handler('/api/games/fps/upload',{'game':'fps','draft_id':'old-snapshot'})
        self.assertEqual(status,409)
        self.assertFalse((self.root/'manual_improve_fps.json').exists())
    def test_upload_cannot_silently_replace_a_queued_improvement(self):
        manual_jobs.enqueue('fps',False,'improve',action='improve')
        with self.assertRaises(ValueError):
            manual_jobs.enqueue('fps',True,'upload',action='upload',draft_id='snapshot')

    def test_upload_worker_does_not_run_improvement(self):
        manual_jobs.enqueue('fps',True,'explicit-upload',action='upload',draft_id='selected-snapshot')
        cfg=SimpleNamespace(account_key=lambda g,slot:'test-key' if slot=='A' else '',base_url='http://unused')
        with patch.object(loop,'load_state',return_value={}),patch.object(loop,'manual_improve') as improve,patch.object(loop,'upload_draft',return_value=True) as upload,patch.object(loop,'Agx'),patch.object(loop,'log'):
            loop.tick(cfg,'fps')
        improve.assert_not_called()
        self.assertEqual(upload.call_args.args[4],'selected-snapshot')

if __name__=='__main__':unittest.main()
