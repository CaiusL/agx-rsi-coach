import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coach.fps_combat import summarize
from coach.fps_signals import Contexts,Responses
from coach.fps_compare import compare


class CombatTests(unittest.TestCase):
    def test_dashboard_attributes_kda_to_played_version(self):
        from coach import ui
        mid='fe1daa77-95fe-4fd6-898f-ed2fb246935b'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);logs=root/'logs'/'fps';logs.mkdir(parents=True)
            (logs/f'{mid}.json').write_text(json.dumps({'played_strategy':{'name':'played-version'}}))
            record={'strategy':{'name':'unplayed-revision'},'last':{'match_id':mid,'recording':{'facts':{
                'kill_attribution':'explicit','our_kills':0,'our_deaths':11,'our_assists':None,'our_kd_ratio':0.}}}}
            with patch.object(ui,'STATE',root):view=ui._slot('fps',record)
            self.assertEqual(view['kda'],{'kills':0,'deaths':11,'assists':None,'kd_ratio':0.,'version':'played-version'})

    def test_shared_contributors_are_not_assigned_kills_or_assists(self):
        facts=summarize([{'type':'kill','target':3,'attackers':[0,2]},{'type':'kill','target':0,'attackers':[0]},{'type':'round_end'}],0,0)
        self.assertEqual(facts['our_kills'],0);self.assertEqual(facts['our_deaths'],1)
        self.assertEqual(facts['our_shared_kill_involvements'],1)
        self.assertIsNone(facts['our_assists']);self.assertIsNone(facts['our_kda_ratio'])

    def test_explicit_assists_and_killer_take_precedence(self):
        facts=summarize([{'type':'kill','target':3,'killer':2,'attackers':[0,2],'assists':[0]},
            {'type':'kill','target':4,'killer':0,'assists':[]},{'type':'kill','target':0,'killer':1,'assists':[]},{'type':'round_end'}],0,0)
        self.assertEqual(facts['our_kills'],1);self.assertEqual(facts['our_assists'],1)
        self.assertEqual(facts['our_kda_ratio'],2.)

    def test_damage_and_first_deaths_are_measured_without_inventing_ratios(self):
        facts=summarize([{'type':'kill','target':0,'attackers':[2],'round_index':0,'match_tick':1},
            {'type':'fire','player':0,'round_index':0,'match_tick':2},
            {'type':'hit','player':0,'target':2,'hp_damage':20.},{'type':'round_end'}],0,0)
        self.assertEqual(facts['our_opening_deaths'],1);self.assertEqual(facts['our_deaths_without_firing'],1)
        self.assertEqual(facts['our_damage_per_round'],20.)
        self.assertIsNone(summarize([],0,0)['our_kd_ratio'])

    def test_readiness_windows_exclude_reload_cooldown_and_weapon_switch(self):
        def obs(tick,ready=0):
            return {'phase':'live','round_index':0,'match_tick':tick,'side':'attack',
                'me':{'alive':True,'x':0,'y':0,'z':0,'yaw':0,'pitch':0,'ammo':30,'active_slot':0,'weapons':[{'id':'ak47','shot_ready':ready},None,None]},
                'visible':[{'seat':9,'x':40,'y':0,'z':0,'alive':True}]}
        contexts=Contexts();responses=Responses()
        c=contexts.step({'observation':obs(0,20),'action':{}});self.assertIsNone(c['ready_window'])
        c=contexts.step({'observation':obs(21),'action':{}})
        self.assertIsNotNone(c['ready_window'])
        responses.step({'fire':True,'weapon_slot':1},c);self.assertEqual(responses.summary()['ready_engagement_windows'],0)
        responses.step({'fire':True,'reload':True},c);self.assertEqual(responses.summary()['ready_engagement_windows'],0)
        responses.step({'fire':True,'weapon_slot':0},c);self.assertEqual(responses.summary()['ready_engagement_windows'],1)

    def test_combat_goal_requires_combat_gain(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'base';candidate=root/'candidate';base.mkdir();candidate.mkdir()
            for target in (base,candidate):(target/'strategy.py').write_text('STRATEGY = {}\n')
            (base/'agent.py').write_text('def init(c): pass\ndef act(o): return {}\n')
            (candidate/'agent.py').write_text('def init(c): pass\ndef act(o): return {"fire":True}\n')
            metadata={'type':'metadata','rules_version':'rsi-fps-5.5','participants':[{'seat':0}]}
            rows=[metadata]
            for tick in range(0,90,3):rows.append({'observation':{'phase':'live','round_index':0,'match_tick':tick,'side':'attack',
                'me':{'alive':True,'x':0,'y':0,'z':0,'yaw':0,'pitch':0,'ammo':30,'active_slot':0,'weapons':[{'id':'ak47','shot_ready':0},None,None]},
                'visible':[{'seat':9,'x':40,'y':0,'z':0,'alive':True}]},'action':{}})
            trace=root/'trace.jsonl';trace.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            result=compare(base,candidate,[trace],goal='combat_kda')
            # The old evaluator accepted this fire-only change. It is not a
            # demonstrated aim/discipline improvement and must now be rejected.
            self.assertFalse(result['ok']);self.assertGreater(result['candidate']['ready_engagement_windows'],0)
            (candidate/'agent.py').write_text('def init(c): pass\ndef act(o): return {"move_right":1}\n')
            result=compare(base,candidate,[trace],goal='combat_kda')
            self.assertFalse(result['ok']);self.assertTrue(any('Combat-focused' in r for r in result['reasons']))


if __name__=='__main__':unittest.main()
