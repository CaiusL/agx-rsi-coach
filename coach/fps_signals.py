"""Policy-independent opportunities from recorded FPS inputs, not a game simulator."""
import math

VERSION = 2
SCORE_KEYS = ('blocked_responses', 'plant_responses', 'defuse_responses', 'rifle_engagements', 'ready_engagement_windows')


def action_key(action, obs):
    if not isinstance(action, dict): return ('invalid', type(action).__name__)
    result = {}
    for key,value in action.items():
        if key == 'weapon_slot':
            if value == (obs.get('me') or {}).get('active_slot'): continue
        elif value in (None,False,'none',''): continue
        if type(value) in (int,float):
            value = round(value,3)
            if value == 0 and key != 'weapon_slot': continue
        result[key] = value
    return result


class Contexts:
    def __init__(self):
        self.previous = None; self.map = {}; self.stall = 0.; self.episode = 0
        self.opportunities = {k:set() for k in SCORE_KEYS}; self.examples = []

    def step(self, row):
        obs = row['observation']; me = obs.get('me') or {}
        if obs.get('map'): self.map = obs['map']
        context = {'active':obs.get('phase') == 'live' and me.get('alive') is True,
                   'round':obs.get('round_index'), 'recorded':row.get('action') or {},
                   'blocked':None,'plant':None,'defuse':None,'rifle':None,'ready_window':None,'equipped_slot':None,'visible_range':None}
        prev = self.previous; self.previous = row
        stationary = False
        if context['active'] and prev:
            po=prev['observation']; pm=po.get('me') or {}; pa=prev.get('applied_action') or prev.get('action') or {}
            dt=(obs.get('match_tick',0)-po.get('match_tick',0))/60
            moving=abs(pa.get('move_forward') or 0)+abs(pa.get('move_right') or 0)>.2
            comparable=po.get('round_index') == obs.get('round_index') and po.get('phase') == 'live' and pm.get('alive')
            coords=all(type(m.get(k)) in (int,float) for m in (me,pm) for k in ('x','y'))
            if comparable and coords and moving and 0 < dt <= .2:
                stationary=math.hypot(me['x']-pm['x'],me['y']-pm['y'])<.005 and abs(me.get('z',0)-pm.get('z',0))<.02
                if stationary: self.stall += dt
        if not stationary:
            if self.stall >= 2: self.episode += 1
            self.stall=0.
        if not context['active']: return context
        if self.stall >= 2 and not obs.get('visible') and not me.get('blind_ticks') and (row.get('action') or {}).get('use') not in ('plant','defuse'):
            context['blocked']=(context['round'],self.episode)
            self.opportunities['blocked_responses'].add(context['blocked'])
            if len(self.examples)<4 and not any(x['episode']==context['blocked'] for x in self.examples):
                self.examples.append({'episode':context['blocked'],'step':row.get('step'),
                    'position':[me.get(k) for k in ('x','y','z')],'action':row.get('action'),
                    'note':'Recorded movement requested without displacement for >=2s; cause not identified.'})
        bomb=obs.get('bomb') or {}; quiet=not obs.get('visible') and not me.get('blind_ticks')
        if quiet and obs.get('side')=='attack' and me.get('carrying_bomb') and bomb.get('state') != 'planted':
            for site in self.map.get('sites') or []:
                center=site.get('center');radius=site.get('radius')
                if center and type(radius) in (int,float) and math.hypot(me.get('x',0)-center[0],me.get('y',0)-center[1])<radius*.8 and abs(me.get('z',0)-(center[2] if len(center)>2 else 0))<1.5:
                    context['plant']=context['round'];self.opportunities['plant_responses'].add(context['plant'])
        position=bomb.get('position')
        if quiet and obs.get('side')=='defend' and bomb.get('state')=='planted' and position and math.hypot(me.get('x',0)-position[0],me.get('y',0)-position[1])<2.5:
            context['defuse']=context['round'];self.opportunities['defuse_responses'].add(context['defuse'])
        visible=[e for e in obs.get('visible') or [] if e.get('alive',True) and 'x' in e and 'y' in e]
        if visible:
            enemy=min(visible,key=lambda e:math.hypot(e['x']-me.get('x',0),e['y']-me.get('y',0)))
            dx,dy=enemy['x']-me.get('x',0),enemy['y']-me.get('y',0);distance=math.hypot(dx,dy)
            context['visible_range']='under_15m' if distance<15 else '15_to_30m' if distance<30 else 'over_30m'
            weapons=me.get('weapons') or [];slot=me.get('active_slot',0)
            context['equipped_slot']=slot
            weapon=(weapons[slot] or {}).get('id') if type(slot) is int and 0<=slot<len(weapons) else None
            yaw_error=(math.atan2(-dy,dx)-me.get('yaw',0)+math.pi)%(2*math.pi)-math.pi
            pitch_error=math.atan2(enemy.get('z',0)-me.get('z',0),max(.01,distance))-me.get('pitch',0)
            if quiet is False and not me.get('blind_ticks') and weapon in ('ak47','m4a1','galilar') and me.get('ammo',0)>0 and distance<=60 and abs(yaw_error)<.06 and abs(pitch_error)<.06 and math.hypot(me.get('vx',0),me.get('vy',0))<.5:
                context['rifle']=(context['round'],context['visible_range']);self.opportunities['rifle_engagements'].add(context['rifle'])
            held=(weapons[slot] or {}) if type(slot) is int and 0<=slot<len(weapons) else {}
            tick=obs.get('match_tick',0)
            ready=(held.get('shot_ready') or 0)<=tick and (me.get('switch_until') or 0)<=tick and not held.get('reload_state') and not me.get('reload')
            if slot in (0,1) and weapon and weapon!='knife' and ready and not me.get('blind_ticks') and me.get('ammo',0)>0 and distance<=60 and abs(yaw_error)<.06 and abs(pitch_error)<.06 and math.hypot(me.get('vx',0),me.get('vy',0))<.5:
                context['ready_window']=(context['round'],enemy.get('seat'),tick//15)
                context['weapon']=weapon;context['distance_m']=round(distance,2)
                self.opportunities['ready_engagement_windows'].add(context['ready_window'])
        return context


class Responses:
    def __init__(self):
        self.covered={k:set() for k in SCORE_KEYS};self.visible={};self.fire={}

    def step(self, action, context):
        if not isinstance(action,dict) or not context['active']: return
        blocked=context['blocked'];old=context['recorded']
        redirect=abs((action.get('yaw_rate') or 0)-(old.get('yaw_rate') or 0))>.35 and abs(action.get('move_forward') or 0)>.2
        if blocked is not None and (abs(action.get('move_right') or 0)>.25 or (action.get('move_forward') or 0)<-.25 or action.get('jump') or redirect):
            self.covered['blocked_responses'].add(blocked)
        if context['plant'] is not None and action.get('use')=='plant':self.covered['plant_responses'].add(context['plant'])
        if context['defuse'] is not None and action.get('use')=='defuse':self.covered['defuse_responses'].add(context['defuse'])
        fires_ready_slot=action.get('fire') and not action.get('reload') and action.get('weapon_slot',context['equipped_slot'])==context['equipped_slot']
        if context['rifle'] is not None and fires_ready_slot:self.covered['rifle_engagements'].add(context['rifle'])
        if context['ready_window'] is not None and fires_ready_slot:self.covered['ready_engagement_windows'].add(context['ready_window'])
        bucket=context['visible_range']
        if bucket:
            self.visible[bucket]=self.visible.get(bucket,0)+1
            self.fire[bucket]=self.fire.get(bucket,0)+bool(action.get('fire'))

    def summary(self):
        return {**{k:len(v) for k,v in self.covered.items()},'visible_calls_by_range':self.visible,'fire_requests_by_range':self.fire}
