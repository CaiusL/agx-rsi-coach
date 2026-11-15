"""Vectorized synthetic combat-skill environment with observable-only features.

This is a declared approximation, not the AGX engine or a match-win simulator.
"""
import math
import numpy as np

FEATURES=('visible','yaw_error_step','pitch_error_step','yaw_error_radius','pitch_error_radius',
          'range','body_radius','cooldown_ready','target_crouch')
FAMILIES=('stationary','moving','crouched_partial','occlusion_reacquire')


def world(seed,count,calibration,split=0,generation=0):
    rng=np.random.default_rng(np.random.SeedSequence([seed,split,generation]))
    family=np.arange(count)%len(FAMILIES);rng.shuffle(family)
    distance=rng.uniform(5,55,count);crouch=(rng.random(count)<.35)|(family==2)
    part=rng.integers(0,3,count);part=np.where(family==2,rng.integers(0,2,count),part)
    height=np.asarray([1.64,1.18,.88])[part]*np.where(crouch,.6,1)
    radius=np.asarray([.12,.25,.25])[part]*np.where(crouch,.6,1)
    motion=np.where((family==1)|(family==3),rng.uniform(-3,3,count)/distance,0.)
    return {'family':family,'distance':distance,'radius':radius,'crouch':crouch.astype(float),
        'target_yaw':rng.uniform(-.6,.6,count),'target_pitch':np.arctan2(height-1.62,distance),
        'motion':motion,'cover_start':rng.integers(8,18,count),'cover_end':rng.integers(22,30,count),
        'yaw_response':calibration['yaw']*rng.uniform(.9,1.1,count),
        'pitch_response':calibration['pitch']*rng.uniform(.9,1.1,count),
        'period':rng.integers(2,4,count),'dt':float(calibration['dt']),
        'nominal_yaw':float(calibration['yaw']),'nominal_pitch':float(calibration['pitch'])}


def observe(w,yaw,pitch,last_shot,step):
    target=w['target_yaw']+w['motion']*step*w['dt']
    dyaw=(target-yaw+math.pi)%(2*math.pi)-math.pi;dpitch=w['target_pitch']-pitch
    cover=(w['family']==3)&(step>=w['cover_start'])&(step<w['cover_end'])
    visible=~cover&(np.abs(dyaw)<math.pi/3)
    ready=step-last_shot>=w['period'];tol=np.arctan2(w['radius'],w['distance'])
    x=np.column_stack([visible.astype(float),
        np.clip(dyaw/(w['nominal_yaw']*w['dt']),-3,3)/3,
        np.clip(dpitch/(w['nominal_pitch']*w['dt']),-3,3)/3,
        np.clip(dyaw/tol,-4,4)/4,np.clip(dpitch/tol,-4,4)/4,
        w['distance']/55,w['radius']/.25,ready.astype(float),w['crouch']])
    # Hidden target position, motion, stance and radius are never learner inputs.
    x[~visible,1:7]=0.;x[~visible,8:]=0.
    context={'yaw_error':dyaw,'pitch_error':dpitch,'tolerance':tol,'visible':visible,'ready':ready}
    return x.astype(np.float32),context


def teacher(w,context):
    action=np.column_stack([np.clip(context['yaw_error']/(w['nominal_yaw']*w['dt']),-1,1),
                            np.clip(context['pitch_error']/(w['nominal_pitch']*w['dt']),-1,1),
                            np.zeros(len(w['distance']))])
    action[:,2]=(context['ready']&context['visible']&
                  (np.abs(context['yaw_error'])<=context['tolerance'])&
                  (np.abs(context['pitch_error'])<=context['tolerance']))
    action[~context['visible']]=0.
    return action.astype(np.float32)


def project(action,w,context):
    """Declared hybrid controller: learned coarse aim; analytic fine aim and fire guard."""
    result=np.asarray(action,dtype=float).copy();result[:,:2]=np.clip(result[:,:2],-1,1)
    expert=teacher(w,context)
    fine=np.hypot(context['yaw_error'],context['pitch_error'])<.04
    result[fine,:2]=expert[fine,:2]
    result[:,2]=(result[:,2]>=.5)&(expert[:,2]>0)
    result[~context['visible']]=0.
    return result.astype(np.float32)


def rollout(w,policy=None,record=False,episode_offset=0):
    n=len(w['distance']);yaw=np.zeros(n);pitch=np.zeros(n);last=np.full(n,-100)
    hits=np.zeros(n);misses=np.zeros(n);first=np.full(n,2.);hidden_fire=0;steps=max(1,int(2/w['dt']))
    collected={k:[] for k in ('x','target','action','reward','next_x','done','episode','priority')}
    for step in range(steps):
        x,context=observe(w,yaw,pitch,last,step);expert=teacher(w,context)
        raw=expert if policy is None else policy(x)
        if not np.isfinite(raw).all():raise ValueError('Non-finite simulated policy action')
        action=expert if policy is None else project(raw,w,context)
        fire=action[:,2]>0;hidden_fire+=int(np.sum(fire&~context['visible']))
        yaw+=action[:,0]*w['yaw_response']*w['dt'];pitch+=action[:,1]*w['pitch_response']*w['dt']
        target=w['target_yaw']+w['motion']*step*w['dt']
        error=np.hypot((target-yaw+math.pi)%(2*math.pi)-math.pi,w['target_pitch']-pitch)
        aligned=fire&(error<=context['tolerance']);bad=fire&~aligned
        hits+=aligned;misses+=bad;first=np.where(aligned&(first==2.),step*w['dt'],first);last=np.where(fire,step,last)
        reward=aligned.astype(float)-2*bad-.01*np.minimum(error,1)
        if record:
            next_x,_=observe(w,yaw,pitch,last,step+1)
            disagreement=np.mean(np.abs(np.asarray(raw)-expert),axis=1)
            for key,value in [('x',x),('target',expert),('action',action),('reward',reward.astype(np.float32)),
                ('next_x',next_x),('done',np.full(n,step==steps-1,dtype=np.uint8)),
                ('episode',np.arange(n,dtype=np.int64)+episode_offset),
                ('priority',(1+4*disagreement+2*(expert[:,2]>(action[:,2]+.1))+2*(reward<0)).astype(np.float32))]:
                collected[key].append(value)
    per_episode=hits-2*misses-.5*first
    metrics={'score':float(per_episode.mean()),'mean_aligned_requests':float(hits.mean()),
             'mean_misaligned_requests':float(misses.mean()),'mean_first_aligned_seconds':float(first.mean()),
             'episodes_with_aligned_fire':int(np.sum(hits>0)),'episodes':n,'hidden_fire_requests':hidden_fire,
             'families':{name:{'score':float(per_episode[w['family']==i].mean()),
                              'episodes':int(np.sum(w['family']==i))} for i,name in enumerate(FAMILIES) if np.any(w['family']==i)}}
    return metrics,{k:np.concatenate(v) for k,v in collected.items()} if record else None
