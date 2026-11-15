"""Select verified map traces and accept only measured, non-regressing responses."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from coach import training
from coach.strategies import render
from coach import match_retention


class EvidenceUnavailable(RuntimeError): pass


VERSION = 5


def corpus(focus=None):
    items=[]
    for path in (training.STORE/'fps').glob('*.summary.json'):
        try:data=json.loads(path.read_text())
        except (OSError,ValueError):continue
        if data.get('status')!='verified':continue
        raw,_=training.paths('fps',data['match_id'],data['seat'])
        if not raw.exists():continue
        stat=raw.stat()
        if (data.get('file_size'),data.get('file_mtime_ns')) != (stat.st_size,stat.st_mtime_ns):continue
        # Download time is not match time. A fresh download of an old match must
        # not evict the latest result. Every available map/seat trace is retained.
        created=''
        try:
            record=json.loads((training.STORE.parent/'logs/fps'/f'{data["match_id"]}.json').read_text())
            created=(record.get('result') or {}).get('created_at','')
        except (OSError,ValueError):pass
        priority=(data['match_id']==(focus or {}).get('match_id'),created,data['match_id'])
        items.append((priority,raw,data['sha256']))
    return sorted(items,reverse=True)[:1 if match_retention.latest_only(training.STORE) else 5]


def assess(code,book,baseline_code,baseline_book,focus=None,goal='quality'):
    traces=corpus(focus)
    if not traces:
        if not (focus or {}).get('match_id'):
            return {'ok':True,'status':'bootstrap_fixture_only','reasons':['No played match yet; starter draft has fixture validation only, not demonstrated behavioral improvement.']}
        return {'ok':False,'status':'evidence_unavailable','reasons':['Verified real-input corpus unavailable; retain current selection and retry data retrieval.']}
    # Latest failure is development evidence. Reserve the two oldest complete
    # matches as a separate validation split; their examples never enter prompts.
    holdout=[p.name for _,p,_ in traces[-2:]] if len(traces)>=3 else []
    scripts=[Path(__file__).with_name(n) for n in ('fps_compare.py','fps_signals.py','fps_quality.py','fps_evaluation.py','gate_fps.py','fps_geometry.py','fps_tactics.py','fps_objectives.py')]
    payload=json.dumps([VERSION,goal,match_retention.latest_only(training.STORE),holdout,code,book,baseline_code,baseline_book,[(str(p),sha) for _,p,sha in traces]],sort_keys=True)
    key=hashlib.sha256(payload.encode()+b''.join(p.read_bytes() for p in scripts)).hexdigest()
    cache=training.STORE/'behavior'/f'{key}.json'
    if cache.exists():return json.loads(cache.read_text())
    with tempfile.TemporaryDirectory(prefix='fps-paired-') as folder:
        root=Path(folder)
        for name,source,strategy in (('baseline',baseline_code,baseline_book),('candidate',code,book)):
            target=root/name;target.mkdir();(target/'agent.py').write_text(source);(target/'strategy.py').write_text(render(strategy))
        try:
            done=subprocess.run([sys.executable,'-I',str(scripts[0]),str(root/'baseline'),str(root/'candidate'),*[str(p) for _,p,_ in traces],
                *(['--combat-kda'] if goal=='combat_kda' else ['--quality'] if goal=='quality' else []),
                *['--holdout='+name for name in holdout]],capture_output=True,text=True,cwd=root,timeout=180)
            result=json.loads(done.stdout.splitlines()[-1])
        except (subprocess.TimeoutExpired,ValueError,IndexError):result={'ok':False,'status':'evaluation_unavailable','reasons':['Paired replay exceeded its budget or returned no report']}
    result['corpus_hash']=key;result['status']=result.get('status') or ('offline_tested_experiment' if result['ok'] else 'rejected')
    result['evaluation_version']=VERSION;result['holdout_traces']=len(holdout)
    if result.get('ok') and len(traces)<3:
        tactical=(result.get('tactical_fixtures') or {}).get('candidate',[])
        if match_retention.latest_only(training.STORE) and len(tactical)>=5 and all(c['ok'] for c in tactical) and result.get('closed_loop_combat'):
            result.update(status='offline_limited_validation',validation_coverage='latest_match_and_fixtures',
                          warning='Latest-only retention: no historical held-out replay. Markdown summaries are trend evidence, not replay inputs.')
        else:
            result.update(ok=False,status='evidence_unavailable');result.setdefault('reasons',[]).append('At least three independent verified match traces are required for development and held-out validation')
    cache.parent.mkdir(parents=True,exist_ok=True);cache.write_text(json.dumps(result))
    return result


def report(result):
    prefix='PASS' if result.get('ok') else 'FAIL'
    compact={k:v for k,v in result.items() if k not in ('traces','corpus_hash')}
    examples=[]
    for trace in result.get('traces') or []:
        if trace.get('split')=='holdout':continue
        examples.extend(trace.get('blocked_examples') or [])
    combat=[]
    quality=[]
    for trace in result.get('traces') or []:
        if trace.get('split')=='holdout':continue
        combat.extend(trace.get('combat_examples') or [])
        quality.append({'trace':trace['trace'],'calibration':trace.get('calibration'),'quality':trace.get('quality'),'recorded_motion':trace.get('recorded_motion')})
    return prefix+' offline_experiment: '+json.dumps(compact,ensure_ascii=False)+'\nDevelopment control audit: '+json.dumps(quality,ensure_ascii=False)+'\nCaptured failure examples: '+json.dumps(examples[:3],ensure_ascii=False)+'\nCombat opportunity examples: '+json.dumps(combat[:4],ensure_ascii=False)


def contract_repair(result, baseline_passed, candidate_passed):
    """A proven legality repair need not invent a combat gain on unaffected inputs."""
    allowed=('No demonstrated behavior change:', 'No measured gain in ', 'Combat-focused revision needs ')
    reasons=result.get('reasons') or []
    if (baseline_passed or not candidate_passed or result.get('ok') or
        result.get('calls',0)<=0 or result.get('holdout_traces',0)<1 or
        (result.get('runtime_errors') or [0,0])[-1] or result.get('regressions') or
        not reasons or any(not reason.startswith(allowed) for reason in reasons)):
        return result
    return {**result,'ok':True,'status':'offline_contract_repair',
        'gains':list(result.get('gains') or [])+['functional_contract_repair'],
        'reasons':[], 'contract_repair_note':'Baseline fails the functional contract and this candidate passes. Paired inputs show no regression; this is a reliability fix, not demonstrated combat improvement.'}
