"""Small trainable skill policy; no inference is installed in a submitted agent."""
import numpy as np


def initialize(features,seed=0,width=32):
    rng=np.random.default_rng(seed);shape=[features,width,width,3];model={}
    for i in range(3):
        model[f'w{i}']=(rng.normal(size=(shape[i],shape[i+1]))/np.sqrt(shape[i])).astype(np.float32)
        model[f'b{i}']=np.zeros(shape[i+1],dtype=np.float32)
    return model


def forward(model,x,cache=False):
    h1=np.tanh(x@model['w0']+model['b0']);h2=np.tanh(h1@model['w1']+model['b1'])
    logits=h2@model['w2']+model['b2'];out=np.column_stack([np.tanh(logits[:,:2]),1/(1+np.exp(-np.clip(logits[:,2],-30,30)))])
    return (out,(h1,h2)) if cache else out


def fit(model,data,epochs=3,seed=0,batch_size=512):
    rng=np.random.default_rng(seed);x=data['x'];target=data['target'];priority=data['priority']
    moments={k:np.zeros_like(v) for k,v in model.items()};variances={k:np.zeros_like(v) for k,v in model.items()};step=0
    final_loss=None
    for _ in range(epochs):
        order=rng.permutation(len(x))
        for offset in range(0,len(x),batch_size):
            ids=order[offset:offset+batch_size];bx=x[ids];by=target[ids]
            weight=np.clip(priority[ids],1,8);weight=weight/(weight.mean()*len(ids))
            out,(h1,h2)=forward(model,bx,True)
            delta=np.empty_like(out);delta[:,:2]=(out[:,:2]-by[:,:2])*(1-out[:,:2]**2)*weight[:,None]
            delta[:,2]=.25*(out[:,2]-by[:,2])*weight
            g2=h2.T@delta;gb2=delta.sum(axis=0);d2=(delta@model['w2'].T)*(1-h2*h2)
            g1=h1.T@d2;gb1=d2.sum(axis=0);d1=(d2@model['w1'].T)*(1-h1*h1)
            gradients={'w2':g2,'b2':gb2,'w1':g1,'b1':gb1,'w0':bx.T@d1,'b0':d1.sum(axis=0)}
            step+=1
            for key,gradient in gradients.items():
                gradient=np.clip(gradient,-5,5);moments[key]=.9*moments[key]+.1*gradient
                variances[key]=.999*variances[key]+.001*gradient*gradient
                model[key]-=.003*(moments[key]/(1-.9**step))/(np.sqrt(variances[key]/(1-.999**step))+1e-8)
            final_loss=float(np.mean((out[:,:2]-by[:,:2])**2)+.25*np.mean(-(by[:,2]*np.log(out[:,2]+1e-7)+(1-by[:,2])*np.log(1-out[:,2]+1e-7))))
    if not all(np.isfinite(v).all() for v in model.values()):raise ValueError('Non-finite learned policy weights')
    return final_loss
