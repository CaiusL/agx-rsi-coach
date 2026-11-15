"""Closed-loop bomb response probes using partial, legal seat observations.

Open flat movement and reveal distance are fixture assumptions, not AGX visibility.
"""
import copy
import math
try:
    from .fps_tactics import base_observation
except ImportError:
    from fps_tactics import base_observation


class ObjectiveAudit:
    """Describe own recorded requests without treating unknown position as truth."""
    def __init__(self):
        self.rounds={}

    def step(self,obs,action):
        me=obs.get('me') or {};bomb=obs.get('bomb') or {}
        if obs.get('phase')!='live' or not me.get('alive') or bomb.get('state')!='planted':return
        key=str(obs.get('round_index'));pos=[me.get(k,0) for k in ('x','y','z')]
        row=self.rounds.setdefault(key,{'side':obs.get('side'),'calls':0,'position_absent_calls':0,
            'defuse_without_current_position':0,'known_out_of_range_defuse':0,'moving_calls':0,
            'first_position':pos,'max_displacement_m':0.,'first_bomb':dict(bomb)})
        row['calls']+=1;position=bomb.get('position');row['position_absent_calls']+=not bool(position)
        row['moving_calls']+=abs(action.get('move_forward') or 0)+abs(action.get('move_right') or 0)>.2
        row['max_displacement_m']=max(row['max_displacement_m'],math.dist(pos,row['first_position']))
        if obs.get('side')=='defend' and action.get('use')=='defuse':
            row['defuse_without_current_position']+=not bool(position)
            if position and math.dist(pos,position)>2.8:row['known_out_of_range_defuse']+=1

    def summary(self):
        return {'rounds':self.rounds,
            'note':'Missing current bomb position alone does not prove an invalid defuse: prior legal memory may locate it. '
                   'Repeated requests with no displacement and no known target require investigation. '
                   'Attack-side post-plant holding can be valid. Counts are requests, not successful interactions.'}


def objective_regressions(agent, record):
    for name,known,remember in (('hidden_search',False,False),('known_rotate',True,False),('remembered_rotate',True,True)):
        agent.init({'contract_id':'rsi-fps-5.5','seat':0})
        obs=base_observation();obs['map']['sites'].append({'id':'B','center':[18.,0.,0.],'radius':4.})
        me=obs['me'];bomb_position=[18.,0.,0.];blind_requests=0;defuse_seconds=0.;max_distance=0.;revealed=False
        # Establish a held-site goal before the state changes to planted.
        for i in range(5):
            obs['match_tick']=obs['round_tick']=1200+i*3
            agent.act(copy.deepcopy(obs))
        obs['bomb']={'state':'planted','fuse_end_tick':3900}
        for i in range(600):
            obs['match_tick']=obs['round_tick']=1500+i*3
            distance=math.hypot(me['x']-18.,me['y'])
            show=(known and (not remember or i==0)) or distance<3.5
            obs['bomb']={'state':'planted','fuse_end_tick':3900}
            if show:obs['bomb'].update(position=bomb_position,site='B');revealed=True
            obs['sounds']=[{'type':'bomb_beep','direction':'unknown','distance_band':'far' if distance>12 else 'near'}]
            action=agent.act(copy.deepcopy(obs))
            if not isinstance(action,dict):action={}
            defusing=action.get('use')=='defuse'
            if defusing and distance>2.8:blind_requests+=1
            me['yaw']+=max(-1,min(1,action.get('yaw_rate',0)))*.15
            yaw=me['yaw'];forward=max(-1,min(1,action.get('move_forward',0)));right=max(-1,min(1,action.get('move_right',0)))
            me['vx']=5*(forward*math.cos(yaw)+right*math.sin(yaw))
            me['vy']=5*(-forward*math.sin(yaw)+right*math.cos(yaw))
            me['x']+=me['vx']*.05;me['y']+=me['vy']*.05
            max_distance=max(max_distance,math.hypot(me['x'],me['y']))
            valid=defusing and distance<=2.8 and math.hypot(me['vx'],me['vy'])<.5
            defuse_seconds=defuse_seconds+.05 if valid else 0.
            if defuse_seconds>=10:break
        passed=defuse_seconds>=10 and blind_requests<=3
        record('objective_'+name,passed,
            f'Found bomb={revealed}, completed stationary 10s defuse={defuse_seconds>=10}, '
            f'out-of-range defuse requests={blind_requests}, displacement={max_distance:.2f}m. '
            'Bomb planted at another site; position can be absent. Clear stale hold/route/escape state, '
            'search with legal information and retain a legitimately observed position. '
            'Open flat fixture, no hidden position supplied until close; no engine path or win prediction.')
