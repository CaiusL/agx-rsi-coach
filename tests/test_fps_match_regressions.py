"""Behavioral regressions exposed by the 50ef9991 match, plus bad-policy controls."""
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from coach.fps_quality import Audit, comparison
from coach.fps_signals import Contexts, Responses
from coach.gate_fps import combat_regressions, navigation_regressions


def policy():
    strategy = types.ModuleType('strategy')
    strategy.STRATEGY = {'default_site':'A', 'expect_site':'A'}
    module = types.ModuleType('regression_agent')
    source = Path(__file__).resolve().parents[1] / 'runtime/fps/agent.py'
    with patch.dict(sys.modules, {'strategy':strategy}):
        exec(compile(source.read_text(), str(source), 'exec'), module.__dict__)
    module.init({'contract_id':'rsi-fps-5.5', 'seat':7})
    return module


def observation(tick=3000):
    return {'phase':'live','round_index':0,'match_tick':tick,'round_tick':tick,
            'side':'defend','seat':7,'me':{'alive':True,'x':0.,'y':0.,'z':0.,
            'yaw':0.,'pitch':0.,'vx':0.,'vy':0.,'ammo':30,'active_slot':0,
            'weapons':[{'id':'mp9','ammo':30,'reserve':90,'shot_ready':0},None,None]},
            'visible':[{'seat':1,'x':25.,'y':0.,'z':0.,'crouch':True,'parts':['chest']}],
            'bomb':{'state':'held'}}


class MatchRegressionTests(unittest.TestCase):
    def test_runtime_passes_geometry_cadence_and_actual_escape_scenarios(self):
        checks = []
        record = lambda name, ok, detail: checks.append((name,ok,detail))
        combat_regressions(policy(), record)
        navigation_regressions(policy(), record)
        self.assertTrue(all(ok for _,ok,_ in checks), checks)

    def test_standing_head_and_slow_two_shot_policy_fail_new_scenarios(self):
        class OldBehavior:
            def init(self, context): self.step = 0
            def act(self, obs):
                self.step += 1
                return {'fire':self.step % 13 in (1,3), 'move_forward':0., 'pitch_rate':0., 'yaw_rate':0.}
        checks = {}
        combat_regressions(OldBehavior(), lambda name,ok,detail: checks.update({name:ok}))
        self.assertFalse(checks['combat_crouched_chest'])
        self.assertFalse(checks['combat_standing_abdomen'])
        self.assertFalse(checks['combat_smg_cadence'])
        # Recoil may also spoil head shots; failing stance and cadence is required.

    def test_single_recovery_pulse_cannot_pass_blocked_corner(self):
        agent = policy()
        def pulse(me, pos, action):
            if agent.MEM['tick'] % 360 == 0:
                action.update(move_forward=0., move_right=1.)
        agent.recover_navigation = pulse
        checks = {}
        navigation_regressions(agent, lambda name,ok,detail: checks.update({name:ok}))
        self.assertFalse(checks['navigation_blocked_corner'])
        self.assertFalse(checks['navigation_jitter_recovery'])

    def test_aim_audit_penalizes_invisible_head_and_uses_crouch_geometry(self):
        obs = observation()
        bad = Audit({'dt':.05,'yaw':3.,'pitch':2.})
        bad.step(obs, {'fire':True})
        self.assertEqual(bad.summary()['aligned_ready_fire'], 0)
        self.assertEqual(bad.summary()['unsafe_fire'], 1)
        obs['me']['pitch'] = math.atan2(1.18*.6-1.62,25.)
        good = Audit({'dt':.05,'yaw':3.,'pitch':2.})
        good.step(obs, {'fire':True})
        self.assertEqual(good.summary()['aligned_ready_fire'], 1)

    def test_eye_height_and_punch_are_in_the_audit(self):
        obs = observation(); obs['me']['crouch'] = True
        obs['me']['pitch'] = math.atan2(1.18*.6-.95,25.) - .02
        obs['me']['weapons'][0]['aim_punch'] = {'yaw':0.,'pitch':.01}
        audit = Audit({'dt':.05,'yaw':3.,'pitch':2.})
        audit.step(obs, {'fire':True})
        self.assertEqual(audit.summary()['aligned_ready_fire'], 1)

    def test_auto_trigger_is_not_forced_to_release_and_switch_blocks_fire(self):
        agent = policy(); obs = observation()
        obs['me']['pitch'] = math.atan2(1.18*.6-1.62,25.)
        obs['me']['weapons'][0]['trigger_state'] = {'pressed':True}
        self.assertTrue(agent.act(obs)['fire'])
        obs['me']['switch_until'] = obs['match_tick']+10
        self.assertFalse(agent.act(obs)['fire'])

    def test_collision_jitter_is_one_episode_and_pulse_is_not_sustained(self):
        contexts, responses = Contexts(), Responses()
        for i in range(100):
            obs=observation(i*3);obs['visible']=[]
            obs['me'].update(x=.04*math.sin(i),y=.04*math.cos(i))
            context=contexts.step({'observation':obs,'action':{'move_forward':1}})
            responses.step({'move_right':1} if i==60 else {'move_forward':1},context)
        self.assertEqual(len(contexts.opportunities['blocked_responses']),1)
        self.assertEqual(responses.summary()['blocked_responses'],1)
        self.assertEqual(responses.summary()['blocked_sustained_responses'],0)

    def test_source_adjacency_is_not_made_bidirectional(self):
        _, _, graph = policy().build_nav({'nav':{'nodes':[
            {'id':'a','position':[0,0,0]},{'id':'b','position':[1,0,0]}],
            'edges':[{'from':'a','to':'b','kind':'source-adjacency'}]}})
        self.assertIn('a',graph);self.assertNotIn('b',graph)

    def test_fixed_camera_count_exception_needs_geometry_gain_and_closed_loop(self):
        before={'mean_aim_error_rad':.08,'aligned_ready_fire':2,'unsafe_fire':10,'conflicting_fire':0}
        after={**before,'mean_aim_error_rad':.06,'aligned_ready_fire':0,'unsafe_fire':0}
        self.assertIn('aligned_ready_fire',comparison(before,after)[1])
        self.assertNotIn('aligned_ready_fire',comparison(before,after,retargeting_validated=True)[1])
        unchanged={**after,'mean_aim_error_rad':.08}
        self.assertIn('aligned_ready_fire',comparison(before,unchanged,retargeting_validated=True)[1])

    def test_correct_aim_without_shooting_cannot_validate_retargeting(self):
        agent = policy()
        original = agent.fight
        def pacifist(me, enemy):
            return {**original(me, enemy), 'fire':False}
        agent.fight = pacifist
        checks = []
        combat_regressions(agent, lambda name,ok,detail: checks.append(ok))
        self.assertFalse(any(checks))


if __name__ == '__main__': unittest.main()
