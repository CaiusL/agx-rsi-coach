import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coach import loop, outcomes


def result(mid, status='succeeded', score=0, hour=0, **extra):
    return {'match_id':mid, 'status':status,'created_at':f'2026-11-14T{hour:02d}:00:00+00:00',
            'seat':0,'team':0,'seat_score':score, **extra}


class LossStreakTests(unittest.TestCase):
    def test_shared_account_slots_and_repeated_reads_count_match_once(self):
        state={};rows=[result('win',score=1),result('loss',hour=2)]
        outcomes.sync(state,'fps',rows*2)
        self.assertEqual(state['loss_streak'],1)
        self.assertTrue(state['loss_streak_info']['exact'])
        self.assertFalse(outcomes.sync(state,'fps',rows))
        self.assertEqual(len(state['match_outcomes']),2)

    def test_late_matches_are_inserted_by_match_time(self):
        state={};outcomes.sync(state,'fps',[result('last',hour=8),result('first',score=1)])
        outcomes.sync(state,'fps',[result('missed-1',hour=2),result('missed-2',hour=4),result('missed-3',hour=6)])
        self.assertEqual(state['loss_streak'],4)
        outcomes.sync(state,'fps',[result('reset',score=1,hour=10)])
        self.assertEqual(state['loss_streak'],0)

    def test_draw_breaks_loss_streak_and_void_jobs_do_not_change_it(self):
        state={};rows=[result('win',score=1),result('loss',hour=2),result('draw',score=.5,hour=4)]
        outcomes.sync(state,'fps',rows+[result('broken-job',status='failed',hour=6)])
        self.assertEqual(state['loss_streak'],0)
        self.assertEqual(state['loss_streak_info']['boundary_outcome'],'draw')
        outcomes.sync(state,'fps',[result('new-loss',hour=8)])
        self.assertEqual(state['loss_streak'],1)

    def test_actual_team_win_is_not_changed_by_a_combat_defect(self):
        row=result('team-win',score=0,round_wins=[13,3],recording={'facts':{'our_shots':0,'our_deaths':12}})
        state={};outcomes.sync(state,'fps',[result('old-loss'),row])
        self.assertEqual(state['loss_streak'],0)

    def test_retirement_overrides_rank_zero_and_survives_partial_api_refresh(self):
        row=result('retired',hour=2,rank=0,recording={'facts':{'finished':False}})
        state={};outcomes.sync(state,'racing',[result('win',rank=0),row])
        self.assertEqual(state['loss_streak'],1)
        outcomes.sync(state,'racing',[result('retired',hour=2,rank=0)])
        self.assertEqual(state['loss_streak'],1)

    def test_unknown_result_is_a_boundary_not_an_invented_loss(self):
        state={};outcomes.sync(state,'fps',[result('win',score=1),result('unknown',score=None,hour=2),result('loss',hour=4)])
        self.assertEqual(state['loss_streak'],1)
        self.assertFalse(state['loss_streak_info']['exact'])

    def test_history_migration_replaces_frozen_counter(self):
        state={'loss_streak':2,'history':[{'a':result('win',score=1)}],
               'A':{'last':result('loss',hour=2)},'B':{'last':result('loss',hour=2)}}
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(outcomes.bootstrap(state,'fps',Path(folder)))
        self.assertEqual(state['loss_streak'],1)

    def test_fresh_match_backfills_results_outside_uploaded_version_window(self):
        rows=[]
        for hour,score in ((0,1),(2,0),(4,0),(6,0)):
            rows.append({'id':f'm{hour}','created_at':f'2026-11-14T{hour:02d}:00:00+00:00','status':'succeeded',
                         'mode':'ranked','_seat':0,'_team':0,'result':{'scores':[score],'ranks':[0 if score else 1]}})
        state={};record={'active_from':'2026-11-14T06:00:00+00:00','seen':[]}
        with patch.object(loop,'my_matches',return_value=(rows,{})),patch.object(loop,'save_state'):
            match,_=loop.fresh_match(SimpleNamespace(api_key='test-key'),'fps',record,state)
        self.assertEqual(match['id'],'m6')
        self.assertEqual(state['loss_streak'],3)

    def test_manual_recording_updates_counter_without_changing_live_strategy(self):
        last=result('win',score=1)
        state={'phase':'play_A','A':{'strategy':{'name':'live'},'last':last},'B':{}}
        outcomes.sync(state,'fps',[last])
        match={'id':'loss','created_at':'2026-11-14T02:00:00+00:00','status':'succeeded',
               '_seat':0,'_team':0,'result':{'scores':[0],'ranks':[1]}}
        before=copy.deepcopy(state['A']['strategy'])
        with patch.object(loop,'fresh_match',return_value=(match,{})),patch.object(loop,'attach_standings'),patch.object(loop,'save_state'),patch.object(loop,'log_game'):
            loop.watch_manual_game(None,'fps',state,None)
            loop.watch_manual_game(None,'fps',state,None)
        self.assertEqual(state['loss_streak'],1)
        self.assertEqual(state['A']['strategy'],before)


if __name__=='__main__':unittest.main()
