"""Closed-loop response fixtures for observed economy and surprise-contact failures.

These use legal seat inputs. They establish responses, never engine kills or wins.
"""
import copy
import math

PRICES = {'ak47':2700, 'm4a1':2900, 'galilar':1800, 'mp9':1250,
          'armor':650, 'helmet':1000, 'defusekit':400}


def base_observation(side='defend'):
    return {'phase':'live', 'round_index':0, 'round_tick':1200, 'match_tick':1200,
            'seat':0, 'side':side, 'me':{'alive':True, 'hp':100., 'x':0., 'y':0., 'z':0.,
                'yaw':0., 'pitch':0., 'vx':0., 'vy':0., 'grounded':True, 'armor':100,
                'cash':0, 'ammo':30, 'active_slot':0,
                'weapons':[{'id':'m4a1', 'ammo':30, 'reserve':90, 'shot_ready':0}, None, None]},
            'visible':[], 'sounds':[], 'bomb':{'state':'held'},
            'map':{'sites':[{'id':'A','center':[0.,0.,0.],'radius':6.}],
                   'spawns':{'T':[{'position':[-25.,0.,0.]}], 'CT':[{'position':[25.,0.,0.]}]},
                   'nav':{'nodes':[], 'edges':[]}}}


def tactical_regressions(agent, record):
    for side, primary in (('attack','ak47'), ('defend','m4a1')):
        agent.init({'contract_id':'rsi-fps-5.5','seat':0})
        obs = base_observation(side); obs['phase']='buy'; me=obs['me']
        me.update(cash=10000, armor=0, helmet=False, weapons=[None,{'id':'pistol','ammo':20},None], active_slot=1)
        bought=[]
        for i in range(20):
            obs['round_tick']=obs['match_tick']=i*3
            action=agent.act(copy.deepcopy(obs)); item=action.get('buy')
            if item not in PRICES or me['cash'] < PRICES[item]: continue
            me['cash']-=PRICES[item]; bought.append(item)
            if item in ('ak47','m4a1','galilar','mp9'):
                me['weapons'][0]={'id':item,'ammo':30,'reserve':90}; me['active_slot']=0
            elif item=='armor': me['armor']=100
            elif item=='helmet': me['helmet']=True
            elif item=='defusekit': me['has_kit']=True
        held=(me['weapons'][0] or {}).get('id')
        record('economy_full_buy_'+side, held==primary and me['armor']>=50 and me.get('helmet',False),
               f'$10000 {side}: bought={bought}, primary={held}, armor={me["armor"]}, helmet={me.get("helmet",False)}. '
               'A declared full-rifle strategy must execute its affordable buy; no predicted duel outcome.')
    for name, target_yaw, damage, sound in (
        ('rear_damage', math.pi, True, None),
        ('near_left_sound', math.pi/2, False, {'type':'footstep','direction':'left','distance_band':'near'}),
        ('defensive_entry', math.pi, False, None),
    ):
        agent.init({'contract_id':'rsi-fps-5.5','seat':0})
        obs=base_observation(); acquired=None; fired=None
        enemy={'seat':1,'alive':True,'x':12*math.cos(target_yaw),'y':-12*math.sin(target_yaw),
               'z':0., 'crouch':False, 'parts':['chest']}
        for i in range(40):
            tick=1200+3*i; obs['round_tick']=obs['match_tick']=tick
            if damage and i==1: obs['me']['hp']=75.
            obs['sounds']=[sound] if sound and i==1 else []
            error=(target_yaw-obs['me']['yaw']+math.pi)%(2*math.pi)-math.pi
            obs['visible']=[enemy] if abs(error)<.5 else []
            if obs['visible'] and acquired is None: acquired=round(i*.05,3)
            action=agent.act(copy.deepcopy(obs))
            obs['me']['yaw']+=max(-1,min(1,action.get('yaw_rate',0)))*.15
            obs['me']['pitch']+=max(-1,min(1,action.get('pitch_rate',0)))*.1
            if action.get('fire') and obs['visible'] and abs(error)<.05 and fired is None:
                fired=round(i*.05,3)
        record('awareness_'+name, acquired is not None and acquired<=1.25 and fired is not None and fired<=1.5,
               f'Acquired={acquired}s, first aimed fire={fired}s after {name}; target hidden until camera faces it. '
               'Open-space angular response only; no inferred hidden coordinates, damage, cover or win-rate model.')
