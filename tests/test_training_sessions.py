import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coach import training, sessions, loop
from coach.strategies import seed

MID = 'e81a049f-04bd-435f-9c45-4520abd37944'


def export(seat=2):
    meta = {'type':'metadata','job_id':MID,'game_id':'fps','participants':[{'seat':seat}], 'rules_version':'rsi-fps-5.5'}
    row = {'seat':seat,'step':0,'observation':{'seat':seat,'phase':'live','me':{'alive':True}},'action':{},'fault':False}
    raw = (json.dumps(row)+'\n').encode()
    done = {'type':'export_complete','complete':True,'rows':1,'raw_bytes':len(raw),
            'sha256':hashlib.sha256(raw).hexdigest(),'seat_calls':{str(seat):1}}
    return (json.dumps(meta)+'\n').encode()+raw+(json.dumps({'type':'outcome','winner':1})+'\n'+json.dumps(done)+'\n').encode()


class TrainingTests(unittest.TestCase):
    def test_actual_encoding_without_type_and_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'trace'; p.write_bytes(export())
            data=training.summarize(p,'fps',MID,2)
            self.assertEqual(data['calls'],1)
            self.assertEqual(data['counts']['empty_active_actions'],1)
            p.write_bytes(export().replace(b'"step": 0',b'"step": 1'))
            with self.assertRaisesRegex(ValueError,'digest mismatch'):training.summarize(p,'fps',MID,2)

    def test_other_seat_and_incomplete_export_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'trace';p.write_bytes(export(3))
            with self.assertRaisesRegex(ValueError,'another seat'): training.summarize(p,'fps',MID,2)
            p.write_bytes(b'\n'.join(export().splitlines()[:-1])+b'\n')
            with self.assertRaisesRegex(ValueError,'Incomplete'):training.summarize(p,'fps',MID,2)

    def test_202_is_not_saved_as_training_and_honors_retry(self):
        class Client:
            calls=0
            def get(self,path,params):
                self.calls+=1
                assert path.startswith('/api/v1/python-submissions/jobs/')
                assert params=={'seat':2}
                return 202, {'Retry-After':'120'}, b'{"status":"preparing"}'
        with tempfile.TemporaryDirectory() as folder,patch.object(training,'STORE',Path(folder)):
            client=Client();focus={'match_id':MID,'seat':2,'status':'succeeded'}
            self.assertEqual(training.read(client,'fps',focus)['status'],'preparing')
            self.assertEqual(training.read(client,'fps',focus)['status'],'preparing')
            self.assertFalse(list(Path(folder).rglob('*.jsonl')))
            self.assertEqual(client.calls,1)

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.target=self.now+timedelta(hours=1)
        self.session=sessions.new({'start_at':self.target.isoformat()},self.now)

    def test_timing_and_no_late_upload_or_repeated_release(self):
        self.assertEqual(sessions.stage(self.session,self.target-timedelta(minutes=14)),'improve')
        self.assertEqual(sessions.stage(self.session,self.target-timedelta(minutes=13)),'settling')
        self.assertEqual(sessions.stage(self.session,self.target-timedelta(minutes=12)),'upload')
        self.assertEqual(sessions.stage(self.session,self.target-timedelta(minutes=9)),'missed')
        self.session['upload_started']=self.now.isoformat()
        self.assertEqual(sessions.stage(self.session,self.target-timedelta(minutes=11)),'complete')

    def test_stale_or_timezone_free_schedule_rejected(self):
        for date in ((self.now-timedelta(seconds=1)).isoformat(),'2026-01-01T12:00:00'):
            with self.assertRaises(ValueError):sessions.new({'start_at':date},self.now)

    def test_no_progress_still_uploads_at_deadline(self):
        self.session['no_progress']=3
        self.assertEqual(sessions.stage(self.session,self.now),'stalled')
        self.assertEqual(sessions.stage(self.session,self.target-timedelta(minutes=12)),'upload')

    def test_cutoff_bypasses_generic_retry_catches(self):
        with self.assertRaises(sessions.Deadline):
            try:
                with sessions.until((self.now-timedelta(seconds=1)).isoformat()):pass
            except Exception:self.fail('Generic retry caught the absolute deadline')

    def test_scheduled_release_selects_exactly_one_snapshot(self):
        s=copy.deepcopy(self.session);s['upload_at']=(self.now-timedelta(seconds=1)).isoformat()
        book=seed('fps');draft={'strategy':book,'code':'code','ready':True};draft['id']=loop.draft_id(draft)
        state={'session':s,'draft':draft,'phase':'play_A','A':{},'B':{}}
        with patch.object(loop,'save_state'),patch.object(loop,'upload_draft',return_value=True) as upload:
            loop.session_step(None,'fps',state,None)
            self.assertEqual(s['status'],'uploaded')
            self.assertEqual(s['selected_id'],draft['id'])
            s['next_watch_at']=(self.now+timedelta(minutes=1)).isoformat()
            loop.session_step(None,'fps',state,None)
            self.assertEqual(upload.call_count,1)

    def test_functionally_identical_names_do_not_count_as_progress(self):
        a={'strategy':seed('racing'),'code':'code'};b=copy.deepcopy(a);b['strategy']['name']='renamed'
        self.assertEqual(loop.functional_draft_key(a),loop.functional_draft_key(b))

if __name__=='__main__':unittest.main()
