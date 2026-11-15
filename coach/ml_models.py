"""Small NumPy models for advisory diagnostics, not counterfactual game simulation."""
import math
import numpy as np


def sigmoid(value):
    value=np.clip(value,-40,40)
    return 1/(1+np.exp(-value))


def fit_logistic(x,y,regularization=1.):
    x=np.asarray(x,dtype=float);y=np.asarray(y,dtype=float)
    mean=x.mean(axis=0);scale=x.std(axis=0);scale[scale<1e-6]=1.
    design=np.column_stack([np.ones(len(x)),(x-mean)/scale])
    weights=np.zeros(design.shape[1]);prior=float(np.clip(y.mean(),1e-4,1-1e-4))
    weights[0]=math.log(prior/(1-prior))
    penalty=np.eye(len(weights))*regularization;penalty[0,0]=1e-6
    for _ in range(35):
        predicted=sigmoid(design@weights)
        gradient=design.T@(predicted-y)+penalty@weights
        hessian=design.T@(design*np.maximum(predicted*(1-predicted),1e-6)[:,None])+penalty
        step=np.linalg.solve(hessian,gradient)
        weights-=step
        if np.linalg.norm(step)<1e-6:break
    return {'weights':weights,'mean':mean,'scale':scale}


def logistic_predict(model,x):
    x=np.asarray(x,dtype=float)
    return sigmoid(model['weights'][0]+((x-model['mean'])/model['scale'])@model['weights'][1:])


def binary_metrics(y,probability,baseline):
    y=np.asarray(y,dtype=float);p=np.asarray(probability,dtype=float)
    brier=float(np.mean((p-y)**2));base=float(np.mean((float(baseline)-y)**2))
    # Tie-aware rank AUC; no artificial ranking among equal probabilities.
    positives=int(y.sum());negatives=len(y)-positives;auc=None
    if positives and negatives:
        order=np.argsort(p,kind='stable');ranks=np.empty(len(p));start=0
        while start<len(p):
            stop=start+1
            while stop<len(p) and p[order[stop]]==p[order[start]]:stop+=1
            ranks[order[start:stop]]=(start+1+stop)/2;start=stop
        auc=float((ranks[y==1].sum()-positives*(positives+1)/2)/(positives*negatives))
    return {'rows':len(y),'positive_rows':positives,'brier':brier,'constant_baseline_brier':base,'auc':auc}


def kernel(a,b,length_scale=1.):
    a=np.asarray(a,dtype=float);b=np.asarray(b,dtype=float)
    squared=np.maximum(0,(a*a).sum(axis=1)[:,None]+(b*b).sum(axis=1)[None,:]-2*a@b.T)
    return np.exp(-squared/(2*length_scale**2))


def fit_gp(x,y,noise=.25):
    """Fixed-prior Gaussian process; binary targets yield scores, not calibrated probabilities."""
    x=np.asarray(x,dtype=float);y=np.asarray(y,dtype=float)
    matrix=kernel(x,x)+np.eye(len(x))*(noise**2+1e-8)
    factor=np.linalg.cholesky(matrix)
    alpha=np.linalg.solve(factor.T,np.linalg.solve(factor,y-.5))
    return {'x':x,'factor':factor,'alpha':alpha,'noise':float(noise)}


def gp_predict(model,x):
    cross=kernel(np.asarray(x,dtype=float),model['x'])
    mean=.5+cross@model['alpha'];v=np.linalg.solve(model['factor'],cross.T)
    uncertainty=np.sqrt(np.maximum(1e-8,1-(v*v).sum(axis=0)+float(model.get('noise',.25))**2))
    return mean,uncertainty


def gp_loo(x,y):
    """Every validation prediction excludes the complete target match."""
    x=np.asarray(x,dtype=float);y=np.asarray(y,dtype=float);pred=[];baselines=[]
    for i in range(len(y)):
        keep=np.arange(len(y))!=i
        pred.append(float(np.clip(gp_predict(fit_gp(x[keep],y[keep]),x[i:i+1])[0][0],0,1)))
        baselines.append(float(y[keep].mean()))
    return {'matches':len(y),'mse':float(np.mean((np.asarray(pred)-y)**2)),
            'constant_baseline_mse':float(np.mean((np.asarray(baselines)-y)**2))}
