"""CPU NumPy navigation classifier trained from kit transitions and teacher labels."""
import numpy as np


def initialize(features, seed=0, width=64):
    rng = np.random.default_rng(seed); sizes = [features, width, width, 5]
    return {key: value for i in range(3) for key, value in (
        (f'w{i}', (rng.normal(size=(sizes[i], sizes[i+1]))/np.sqrt(sizes[i])).astype(np.float32)),
        (f'b{i}', np.zeros(sizes[i+1], dtype=np.float32)))}


def forward(model, x, cache=False):
    h1 = np.tanh(x@model['w0']+model['b0']); h2 = np.tanh(h1@model['w1']+model['b1'])
    logits = h2@model['w2']+model['b2']
    return (logits, (h1, h2)) if cache else logits


def fit(model, data, epochs=4, seed=0, batch_size=256):
    rng = np.random.default_rng(seed); x = data['x']; y = data['target']
    first = {k: np.zeros_like(v) for k, v in model.items()}; second = {k: np.zeros_like(v) for k, v in model.items()}
    iteration = 0; loss = None
    for _ in range(epochs):
        order = rng.permutation(len(x))
        for start in range(0, len(x), batch_size):
            ids = order[start:start+batch_size]; logits, (h1, h2) = forward(model, x[ids], True)
            logits -= logits.max(axis=1, keepdims=True); probability = np.exp(logits)
            probability /= probability.sum(axis=1, keepdims=True)
            weight = np.clip(data['priority'][ids], 1, 8); weight /= weight.sum()
            delta = (probability-y[ids])*weight[:, None]
            d2 = (delta@model['w2'].T)*(1-h2*h2); d1 = (d2@model['w1'].T)*(1-h1*h1)
            gradients = {'w2': h2.T@delta, 'b2': delta.sum(axis=0), 'w1': h1.T@d2,
                         'b1': d2.sum(axis=0), 'w0': x[ids].T@d1, 'b0': d1.sum(axis=0)}
            iteration += 1
            for key, gradient in gradients.items():
                gradient = np.clip(gradient, -5, 5)
                first[key] = .9*first[key]+.1*gradient; second[key] = .999*second[key]+.001*gradient*gradient
                model[key] -= .002*(first[key]/(1-.9**iteration))/(np.sqrt(second[key]/(1-.999**iteration))+1e-8)
            loss = float(-np.mean(np.sum(y[ids]*np.log(probability+1e-8), axis=1)))
    if not all(np.isfinite(v).all() for v in model.values()):raise ValueError('Non-finite navigation weights')
    return loss
