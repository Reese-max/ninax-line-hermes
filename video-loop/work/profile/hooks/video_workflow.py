"""One video request, one immutable audit binding, one approved final payload."""
import json
import math
from pathlib import Path
import sys
import time

import video_evidence as evidence
import video_takeaway_cascade as cascade
from video_evidence import atomic_json, digest, job_lock, read_json, save_turn
from video_review import NOTICE, REVIEW_POLICY, payload, review_all, status_notice


def execute(request):
    started = time.monotonic()
    deadline = started + min(290, request.get('seconds', 290))
    if request.get('jobs_root'):
        cascade.JOBS = evidence.JOBS = Path(request['jobs_root']).resolve()
    ledger = []
    result = cascade.cascade(request['url'], seconds=max(1, deadline-time.monotonic()-80), force=request.get('force', False))
    job = Path(result['job'])
    doc = result['evidence']
    # The per-turn evidence snapshot travels with the proof; another turn may enrich the shared job.
    def review_source(doc):
        clean = {k:v for k,v in doc.items() if k not in {'turns', 'recovery'}}
        clean['recovery_result'] = {k:doc.get('recovery',{}).get(k) for k in ('status','new_evidence','requested_refresh')}
        return clean
    clean = review_source(doc)
    reviewed = None
    previous = request.get('recall') or {}
    if (previous.get('review_policy')==REVIEW_POLICY and previous.get('evidence_revision') == doc['evidence_revision']
            and previous.get('audit', {}).get('status') == 'pass'):
        reviewed = {'text':previous['text'], 'audit':previous['audit'], 'must_cover':previous.get('must_cover', [])}
    try:
        if reviewed is None:
            reviewed = review_all(clean, request.get('review_question') or request['question'], deadline-3, ledger, job)
            if reviewed.get('source_checks') and deadline-time.monotonic()>65:
                budget=100 if any(r['kind']=='original' for r in reviewed['source_checks']) else 60
                doc = cascade.repair_requested(job,doc,reviewed['source_checks'],min(deadline-55,time.monotonic()+budget),ledger)
                clean = review_source(doc)
                if doc.get('recovery',{}).get('new_evidence'):
                    reviewed = review_all(clean, request.get('review_question') or request['question'], deadline-3, ledger, job)
    except Exception as exc:
        checks = [x['result'] for x in ledger if x.get('stage') == 'audit' and x.get('result')]
        covers = next((x['result'].get('must_cover',[]) for x in ledger if x.get('stage') == 'checklist' and x.get('result')),[])
        reviewed = {'text':NOTICE, 'audit':{'status':'not_checked','reason':type(exc).__name__,'checks':checks}, 'must_cover':covers}
    if reviewed['audit']['status'] != 'pass':
        reviewed['text'] = status_notice(doc['source'],doc['gaps'],reviewed.get('progress'))
    messages = payload(reviewed['text'])
    binding = {k:request[k] for k in ('profile','session_id','session_key','turn_id',
                                      'input_id','input_revision','input_sha256') if k in request}
    binding.update(video=doc['source'], evidence_revision=doc['evidence_revision'],
                   requirement_revision=digest(reviewed['must_cover']))
    approval = {'binding':binding, 'payload_sha256':digest(messages),
                'kind':'summary' if reviewed['audit']['status'] == 'pass' else 'notice',
                'approved_at':time.time()}
    turn = {'binding':binding, 'question':request['question'], 'must_cover':reviewed['must_cover'],
            'review_policy':REVIEW_POLICY,'progress':reviewed.get('progress'),
            'summary_audit':reviewed['audit'], 'approved_delivery':approval, 'evidence_snapshot':clean,
            'recovery':doc.get('recovery'), 'review_calls':ledger, 'elapsed_seconds':round(time.monotonic()-started,2)}
    save_turn(job, request['turn_id'], turn)
    # Statistics contain counts and timings only; chat text and tokens stay in the profile/job.
    try:
        with job_lock(evidence.JOBS,time.monotonic()+1,'quality.lock'):
            atomic_json(evidence.JOBS/'.ninax-quality.json',quality_report(evidence.JOBS))
    except (OSError,TimeoutError):
        pass  # A metrics write must not invalidate an otherwise audited response.
    return {**reviewed, 'review_policy':REVIEW_POLICY,'approval':approval, 'job':str(job), 'evidence_revision':doc['evidence_revision'],
            'source':doc['source'], 'gaps':doc['gaps'], 'recovery':doc.get('recovery'), 'elapsed_seconds':turn['elapsed_seconds']}


def quality_report(jobs):
    # ponytail: O(n) over stored turn receipts; use SQLite when this scan materially delays a turn.
    turns=[]
    unreadable=0
    for path in Path(jobs).glob('*/evidence.json'):
        try:
            turns.extend(json.loads(path.read_text())['turns'].values())
        except (OSError,ValueError,KeyError,AttributeError):
            unreadable+=1
    turns.sort(key=lambda t:t.get('approved_delivery',{}).get('approved_at',0))
    statuses={}
    source_statuses={}
    calls=[]
    times=[]
    recovered=0
    for turn in turns:
        status=turn.get('summary_audit',{}).get('status','unknown')
        statuses[status]=statuses.get(status,0)+1
        source_status=turn.get('evidence_snapshot',{}).get('evidence_status','unknown')
        source_statuses[source_status]=source_statuses.get(source_status,0)+1
        calls.extend(c for c in turn.get('review_calls',[]) if c.get('stage') in {'checklist','audit','revision'})
        elapsed=turn.get('elapsed_seconds')
        if isinstance(elapsed,(int,float)) and math.isfinite(elapsed) and elapsed>=0:
            times.append(elapsed)
        recovered+=bool((turn.get('recovery') or {}).get('new_evidence'))
    times.sort()
    known=[c for c in calls if isinstance((c.get('usage') or {}).get('total_tokens'),int)]
    recent=turns[-3:]
    warnings=[]
    if len(recent)==3 and all(t.get('summary_audit',{}).get('status')!='pass' for t in recent):
        warnings.append('three_consecutive_reviews_not_passed')
    if unreadable:
        warnings.append('unreadable_evidence_records')
    return {'generated_at':time.time(),'turns':len(turns),'audit_status':statuses,'source_status':source_statuses,
            'audit_pass_rate':statuses.get('pass',0)/len(turns) if turns else None,
            'turns_with_new_evidence':recovered,
            'latency_seconds':{key:times[max(0,math.ceil(len(times)*q)-1)] if times else None for key,q in [('p50',.5),('p95',.95)]},
            'llm_calls':len(calls),'known_usage_calls':len(known),'unknown_usage_calls':len(calls)-len(known),
            'reported_total_tokens':sum(c['usage']['total_tokens'] for c in known),
            'cost':None,'cost_status':'not_reported_by_provider','warnings':warnings,'unreadable_records':unreadable}


if __name__ == '__main__':
    try:
        print(json.dumps(quality_report(Path(sys.argv[2])) if len(sys.argv)==3 and sys.argv[1]=='--report'
                         else execute(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}))
        raise SystemExit(1)
