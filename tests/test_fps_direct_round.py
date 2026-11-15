import copy
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coach import fps_match_data, loop, round_control, match_retention
from coach.config import load
from coach.fps_tactics import tactical_regressions


class DirectRoundTests(unittest.TestCase):
    def test_reservation_blocks_model_and_a_different_upload_only_until_round_start(self):
        now=datetime.now(timezone.utc)
        state={'round_authority':{'author':'codex','round_at':(now+timedelta(hours=1)).isoformat(),'draft_id':'pinned'}}
        with self.assertRaises(ValueError):round_control.require_model_allowed(state,now)
        with self.assertRaises(ValueError):round_control.require_upload_allowed(state,{'id':'other','author':'codex'},now)
        with self.assertRaises(ValueError):round_control.require_upload_allowed(state,{'id':'pinned','author':'omlx'},now)
        round_control.require_upload_allowed(state,{'id':'pinned','author':'codex'},now)
        round_control.require_model_allowed(state,now+timedelta(hours=2))

    def test_direct_reservation_rejects_improvement_before_any_model_or_state_write(self):
        state={'round_authority':{'author':'codex','round_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}
        with patch.object(loop,'save_state') as save,patch.object(loop,'chat_text') as model:
            with self.assertRaises(ValueError):loop.manual_improve(load(),'fps',state,None)
        save.assert_not_called();model.assert_not_called()

    def test_future_diagnosis_and_planning_enable_reasoning_for_both_games(self):
        answer=json.dumps({'comparison':'2 observed rows','change':'one lever','hypothesis':'uncertain',
                           'evidence':['measured result'],'plan':['validate'],'uncertainty':'not causal'})
        for game in ('fps','racing'):
            with patch.object(loop,'chat_text',return_value=answer) as call:
                decision=json.loads(loop.diagnose(load(),game,[{'role':'user','content':'verified rows'}]))
                self.assertTrue(call.call_args.kwargs['think'])
                self.assertIn('plan',decision)

    def test_unaware_cheap_buy_policy_fails_surprise_and_wealth_checks(self):
        class Unaware:
            def init(self,ctx):pass
            def act(self,obs):return {'buy':'mp9','move_forward':1.,'yaw_rate':0.}
        checks=[];tactical_regressions(Unaware(),lambda n,ok,d:checks.append((n,ok)))
        self.assertTrue(checks);self.assertTrue(all(not ok for _,ok in checks))


class MatchDownloadsTests(unittest.TestCase):
    def test_bulk_exports_are_cached_and_gzip_frames_are_read_without_expanding(self):
        mid='599a5b25-8d2c-4793-9b28-ccf7732da229'
        frame={'match_tick':0,'round_index':0,'phase':'post','players':[]}
        payloads={'events':json.dumps({'type':'match_end','match_tick':0,'round_wins':[0,13]})+'\n',
                  'static':json.dumps({'type':'map','map':{'id':'test','sites':[]}})+'\n',
                  'frames':json.dumps(frame)+'\n'}
        class Client:
            def __init__(self):self.calls=[]
            def download(self,path,params,target,timeout):
                self.calls.append(path);kind=path.rsplit('/',1)[1];data=payloads[kind].encode()
                target.write_bytes(gzip.compress(data) if kind=='frames' else data)
                return 200,{}
        with tempfile.TemporaryDirectory() as tmp,patch.object(fps_match_data,'STORE',Path(tmp)),patch.object(fps_match_data.training,'paths',return_value=(Path(tmp)/'missing',Path(tmp)/'missing-summary')):
            client=Client();focus={'match_id':mid,'seat':7,'status':'succeeded'}
            a=fps_match_data.read(client,focus,background=False);b=fps_match_data.read(client,focus,background=False)
            self.assertEqual(a['status'],'verified');self.assertEqual(b['frame_rows'],1)
            self.assertEqual(len(client.calls),3)
            self.assertTrue((Path(tmp)/mid/'frames.download').read_bytes().startswith(b'\x1f\x8b'))

    def test_incomplete_frames_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);files={k:folder/k for k in ('events','static','frames')}
            files['events'].write_text(json.dumps({'type':'match_end','match_tick':100})+'\n')
            files['static'].write_text('{}\n');files['frames'].write_text(json.dumps({'match_tick':0,'round_index':0,'players':[]})+'\n')
            with self.assertRaisesRegex(ValueError,'Incomplete'):
                fps_match_data.analyze(files,{'match_id':'599a5b25-8d2c-4793-9b28-ccf7732da229','seat':7})

    def test_untrusted_match_ids_cannot_select_paths(self):
        with self.assertRaises(ValueError):fps_match_data.read(None,{'match_id':'../../escape','seat':7,'status':'succeeded'})


class RetentionTests(unittest.TestCase):
    def test_older_raw_is_removed_only_after_markdown_is_saved(self):
        older='47ac7a55-d6a0-436f-aaa3-395724751f1d';newer='599a5b25-8d2c-4793-9b28-ccf7732da229'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);train=root/'training/fps';train.mkdir(parents=True)
            for mid in (older,newer):
                (train/(mid+'-seat7.jsonl')).write_text('raw decisions')
                (train/(mid+'-seat7.summary.json')).write_text(json.dumps({'match_id':mid,'seat':7,'status':'verified','counts':{'fault_calls':0}}))
            with patch.multiple(match_retention,POLICY=root/'retention.json',TRAINING=root/'training',RECORDINGS=root/'recordings',LOGS=root/'logs',HISTORY=root/'history'),patch.object(match_retention,'_created',side_effect=lambda g,m:'2026-11-15T02:00:00Z' if m==older else '2026-11-15T04:00:00Z'):
                (root/'retention.json').write_text('{"mode":"latest_per_game"}')
                result=match_retention.prune('fps')
                self.assertEqual(result['retained_match'],newer)
                self.assertFalse((train/(older+'-seat7.jsonl')).exists())
                self.assertTrue((train/(newer+'-seat7.jsonl')).exists())
                self.assertIn('fault_calls',(root/'history/fps'/f'{older}.md').read_text())
                late=root/'recordings/fps'/older;late.mkdir(parents=True)
                (late/'seat7.summary.json').write_text(json.dumps({'status':'unavailable'}))
                match_retention.prune('fps')
                self.assertIn('fault_calls',(root/'history/fps'/f'{older}.md').read_text())

    def test_changed_newer_raw_does_not_replace_the_verified_match(self):
        old='47ac7a55-d6a0-436f-aaa3-395724751f1d';new='599a5b25-8d2c-4793-9b28-ccf7732da229'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);train=root/'training/fps';train.mkdir(parents=True)
            for mid in (old,new):
                raw=train/(mid+'-seat7.jsonl');raw.write_text('verified raw')
                summary={'match_id':mid,'seat':7,'status':'verified','file_size':raw.stat().st_size,'file_mtime_ns':raw.stat().st_mtime_ns}
                (train/(mid+'-seat7.summary.json')).write_text(json.dumps(summary))
            (train/(new+'-seat7.jsonl')).write_text('truncated')
            with patch.multiple(match_retention,POLICY=root/'retention.json',TRAINING=root/'training',RECORDINGS=root/'recordings',LOGS=root/'logs',HISTORY=root/'history'),patch.object(match_retention,'_created',side_effect=lambda g,m:'2026-11-15T02:00:00Z' if m==old else '2026-11-15T04:00:00Z'):
                (root/'retention.json').write_text('{"mode":"latest_per_game"}')
                result=match_retention.prune('fps')
                self.assertEqual(result['retained_match'],old)
                self.assertTrue((train/(old+'-seat7.jsonl')).exists())
