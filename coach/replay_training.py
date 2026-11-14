"""Replay original policy inputs for runtime compatibility, never simulated outcomes."""
import importlib.util
import json
from pathlib import Path
import sys


def run(folder, trace):
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location('candidate', folder/'agent.py')
    agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)
    # This observer also exposes helper exceptions suppressed into live empty actions.
    spec = importlib.util.spec_from_file_location('replay_monitor', Path(__file__).with_name('gate_fps.py'))
    monitor = importlib.util.module_from_spec(spec); spec.loader.exec_module(monitor)
    monitor.monitor_empty_errors(agent)
    calls = active = empty = errors = invalid = 0; examples = []
    with trace.open() as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('type') == 'metadata':
                participant = row['participants'][0]
                agent.init({'contract_id':row['rules_version'], 'seat':participant['seat'],
                            'role':participant.get('role',0), 'seed':row.get('seed',1), 'weights_dir':str(folder)})
            if not isinstance(row.get('observation'), dict): continue
            obs = row['observation']; calls += 1
            live = obs.get('phase') == 'live' and (obs.get('me') or {}).get('alive') is True
            active += live
            try:
                action = agent.act(obs)
                invalid += not isinstance(action, dict)
                if live and action == {}: empty += 1
            except Exception as exc:
                errors += 1
                if len(examples)<3: examples.append({'step':row.get('step'), 'error':f'{type(exc).__name__}: {exc}'})
    hidden = getattr(agent,'_gate_empty_errors',[])
    facts = {'calls':calls,'active_calls':active,'empty_active_actions':empty,'exceptions':errors,
             'invalid_returns':invalid,'swallowed_errors':len(hidden), 'examples':examples+list(hidden[:2])}
    # Empty actions are legal in some contexts; they are reported, not failed by themselves.
    ok = calls > 0 and not (errors or invalid or hidden)
    return ok, facts


if __name__ == '__main__':
    try:
        ok, facts = run(Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps({'ok':ok,'facts':facts}))
    except Exception as exc:
        print(json.dumps({'ok':False,'facts':{'error':f'{type(exc).__name__}: {exc}'}}))
