import copy
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from coach import bundle,loop,release,series,sessions
from coach.agx import AgxError
from coach.strategies import seed
from tests.test_draft_upload import state_for


class ReadinessTests(unittest.TestCase):
    def test_paused_publication_is_not_reported_as_failed_admission(self):
        class Client:
            api_key='synthetic-test-key'
            def post_zip(self,*args):return 200,{},b'{"submission_id":"accepted","admission":"passed"}'
            def post_json(self,*args,**kwargs):raise AgxError('competition paused',409)
        state=state_for();state['draft']={'strategy':{**seed('fps'),'name':'selected'},'code':'candidate','ready':True}
        before=copy.deepcopy(state['B']);identity=loop.draft_id(state['draft'])
        with patch.object(loop,'check',return_value=(True,'PASS syntax')),patch.object(loop.training,'check_replay',return_value=(True,'')),patch.object(loop.fps_evaluation,'assess',return_value={'ok':True}),patch.object(loop,'quota_open',return_value=True),patch.object(loop,'round_payload',return_value={}),patch.object(loop,'ensure_agent',return_value='agent'),patch.object(loop,'register'),patch.object(loop,'note_upload'),patch.object(loop,'save_state'),patch.object(loop,'log'):
            self.assertFalse(loop.upload_draft(None,'fps',state,Client(),identity))
        self.assertEqual(state['B'],before)
        self.assertIn('Bundle passed admission',state['manual_result']['error'])
        self.assertEqual(state['release_attempt']['http_status'],409)

    def test_racing_generation_baseline_never_replaces_selected_release(self):
        state=state_for('racing');selected={'strategy':{**seed('racing'),'name':'reviewed-v1','preview':.15},
            'code':'selected source','ready':True,'revision':8}
        selected['id']=loop.draft_id(selected);state['draft']=copy.deepcopy(selected)
        baseline={'strategy':{**seed('racing'),'name':'trial-baseline','preview':.2},'code':'baseline source'}
        observed=[]
        def failed(cfg,game,work,*args):
            observed.append((work['A']['strategy']['preview'],copy.deepcopy(state['draft'])))
            work['plan_ready']=False
        with patch.object(loop,'write_plan',side_effect=failed),patch.object(loop,'save_state'),patch.object(loop,'log'):
            loop.manual_improve(None,'racing',state,None,baseline_override=baseline)
        self.assertEqual(observed,[(.2,selected)])
        self.assertEqual(state['draft'],selected)

    def test_readiness_lock_prevents_generation_but_preserves_release(self):
        now=datetime.now(timezone.utc);target=now+timedelta(hours=1)
        session=sessions.new({'start_at':target.isoformat()},now);session['readiness_locked']=True
        self.assertEqual(sessions.stage(session,now),'ready')
        self.assertEqual(sessions.stage(session,target-timedelta(minutes=12)),'upload')

    def test_progress_retains_bundle_id_when_publication_times_out(self):
        class Client:
            api_key='synthetic-test-key'
            def post_zip(self,*args):return 200,{},b'{"submission_id":"accepted-bundle","admission":"passed"}'
            def post_json(self,*args,**kwargs):raise AgxError('publication timeout')
        state=state_for();state['draft']={'strategy':{**seed('fps'),'name':'selected'},'code':'candidate','ready':True}
        state['release_attempt']={'submission_id':'stale-id','activation_response':{'old':True}}
        before=copy.deepcopy(state['B']);identity=loop.draft_id(state['draft'])
        with patch.object(loop,'check',return_value=(True,'PASS syntax')),patch.object(loop.training,'check_replay',return_value=(True,'')),patch.object(loop.fps_evaluation,'assess',return_value={'ok':True}),patch.object(loop,'quota_open',return_value=True),patch.object(loop,'round_payload',return_value={}),patch.object(loop,'ensure_agent',return_value='agent'),patch.object(loop,'register'),patch.object(loop,'note_upload'),patch.object(loop,'save_state'):
            with self.assertRaises(AgxError):loop.upload_draft(None,'fps',state,Client(),identity)
        self.assertEqual(state['B'],before)
        self.assertEqual(state['manual_result']['status'],'confirmation_required')
        self.assertEqual(state['release_attempt']['stage'],'publishing')
        self.assertEqual(state['release_attempt']['submission_id'],'accepted-bundle')
        self.assertNotIn('activation_response',state['release_attempt'])

    def test_second_coach_cannot_take_same_state_directory(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(loop,'STATE',Path(directory)):
            first=loop.acquire_coach_lock()
            try:
                with self.assertRaises(RuntimeError):loop.acquire_coach_lock()
            finally:first.close()
            next_owner=loop.acquire_coach_lock();next_owner.close()

    def test_bundle_bytes_do_not_depend_on_zip_clock(self):
        book=seed('fps');code='def act(o): return {}\n'
        a=bundle.build('fps',book,code);b=bundle.build('fps',book,code)
        self.assertEqual(a,b)
        with zipfile.ZipFile(io.BytesIO(a)) as archive:
            self.assertEqual(archive.namelist(),['agent.py','strategy.py'])
            self.assertTrue(all(info.date_time==(1980,1,1,0,0,0) for info in archive.infolist()))

    def test_series_reset_preserves_platform_ids_and_trial_history(self):
        state=state_for();state['B']['submission_id']='agx-original-id'
        state['match_outcomes']=[{'match_id':'trial','outcome':'loss','created_at':'2026-11-14T10:00:00+00:00'}]
        state['draft']={'strategy':seed('fps'),'code':'policy','revision':19,'id':'selected'}
        before=copy.deepcopy(state['B'])
        self.assertTrue(series.configure(state,'2026-11-15T00:00:00+08:00'))
        self.assertEqual(state['draft']['revision'],1);self.assertEqual(state['draft']['series_version'],1)
        self.assertEqual(state['B'],before);self.assertEqual(len(state['match_outcomes']),1)
        self.assertFalse(series.configure(state,'2026-11-14T16:00:00+00:00'))
        state['match_outcomes'] += [{'match_id':'official','outcome':'loss','created_at':'2026-11-14T16:00:00+00:00'}]
        view=series.view(state,datetime(2026,11,14,17,tzinfo=timezone.utc))
        self.assertEqual(view['matches'],1);self.assertEqual(view['loss_streak'],1)

    def test_version_increments_on_admission_not_local_rebuild(self):
        state=state_for();series.configure(state,'2026-11-15T00:00:00+08:00')
        draft={'id':'snapshot-a'}
        series.stamp(state,draft);series.stamp(state,draft)
        self.assertEqual(draft['series_version'],1)
        series.admitted(state,draft);series.admitted(state,draft)
        self.assertEqual(state['counted_series']['next_version'],2)
        self.assertEqual(len(state['counted_series']['releases']),1)
        self.assertEqual(series.version(state,'snapshot-a'),1)
        self.assertEqual(series.version(state,'snapshot-b'),2)

    def test_release_files_match_actual_submission_bytes(self):
        state=state_for();series.configure(state,'2026-11-15T00:00:00+08:00')
        draft={'strategy':seed('fps'),'code':'def act(o): return {}\n','ready':True,'revision':1,'report':'PASS syntax'}
        draft['id']=loop.draft_id(draft)
        with tempfile.TemporaryDirectory() as directory,patch.object(release,'STORE',Path(directory)):
            info=release.prepare('fps',state,draft)
            self.assertEqual(Path(info['archive']).name,'fps-v1.zip')
            expected=bundle.build('fps',draft['strategy'],draft['code'])
            self.assertEqual(Path(info['archive']).read_bytes(),expected)
            self.assertEqual(info['bundle_sha256'],hashlib.sha256(expected).hexdigest())
            manifest=json.loads(Path(info['manifest']).read_text())
            for name,sha in manifest['files'].items():self.assertEqual(hashlib.sha256((Path(info['manifest']).parent/name).read_bytes()).hexdigest(),sha)
            Path(info['archive']).write_bytes(b'tampered archive')
            with self.assertRaises(ValueError):release.prepare('fps',state,draft)
            draft['code']='mutated after validation'
            with self.assertRaises(ValueError):release.prepare('fps',state,draft)

    def test_deadline_restores_live_slot_and_marks_confirmation_unknown(self):
        state=state_for();state['draft']={'strategy':{**seed('fps'),'name':'selected'},'code':'candidate','ready':True}
        before=copy.deepcopy(state['B']);identity=loop.draft_id(state['draft'])
        with patch.object(loop,'check',return_value=(True,'PASS syntax')),patch.object(loop.training,'check_replay',return_value=(True,'')),patch.object(loop.fps_evaluation,'assess',return_value={'ok':True}),patch.object(loop,'ensure_live',side_effect=sessions.Deadline()),patch.object(loop,'save_state'):
            with self.assertRaises(sessions.Deadline):loop.upload_draft(None,'fps',state,None,identity)
        self.assertEqual(state['B'],before)
        self.assertEqual(state['manual_result']['status'],'confirmation_required')

    def test_due_release_precedes_unrelated_manual_job(self):
        now=datetime.now(timezone.utc)
        due=sessions.new({'start_at':(now+timedelta(minutes=11)).isoformat()},now)
        old=sessions.new({'start_at':(now-timedelta(hours=1)).isoformat()},now-timedelta(hours=2))
        old['status']='missed_window'
        states={'fps':{'session':due},'racing':{'session':old}}
        with patch.object(loop,'load_state',side_effect=lambda game:states[game]):
            self.assertEqual(loop.game_order(['racing'],now),['fps','racing'])

    def test_recorded_replay_rejects_invalid_action_values(self):
        from coach.replay_training import run
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'agent.py').write_text('def init(c): pass\ndef act(o): return {"fire":1}\n')
            trace=root/'trace.jsonl';rows=[{'type':'metadata','rules_version':'rsi-fps-5.5','participants':[{'seat':0}]},
                {'step':0,'observation':{'phase':'live','me':{'alive':True}}}]
            trace.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
            ok,facts=run(root,trace)
            self.assertFalse(ok);self.assertEqual(facts['invalid_returns'],1)


if __name__=='__main__':unittest.main()
