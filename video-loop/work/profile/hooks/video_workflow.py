"""One video request, one immutable audit binding, one approved final payload."""
import json
from pathlib import Path
import sys
import time

import video_evidence as evidence
import video_takeaway_cascade as cascade
from video_evidence import atomic_json, digest, read_json, save_turn
from video_review import NOTICE, payload, review, status_notice


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
    clean = {k:v for k,v in doc.items() if k not in {'turns', 'recovery'}}
    clean['recovery_result'] = {k:doc.get('recovery',{}).get(k) for k in ('status','new_evidence','requested_refresh')}
    reviewed = None
    previous = request.get('recall') or {}
    if previous.get('evidence_revision') == doc['evidence_revision'] and previous.get('audit', {}).get('status') == 'pass':
        reviewed = {'text':previous['text'], 'audit':previous['audit'], 'must_cover':previous.get('must_cover', [])}
    try:
        if reviewed is None:
            reviewed = review(clean, request['question'], deadline-2, ledger)
    except Exception as exc:
        checks = [x['result'] for x in ledger if x.get('stage') == 'audit' and x.get('result')]
        covers = next((x['result'].get('must_cover',[]) for x in ledger if x.get('stage') == 'checklist' and x.get('result')),[])
        reviewed = {'text':NOTICE, 'audit':{'status':'not_checked','reason':type(exc).__name__,'checks':checks}, 'must_cover':covers}
    if reviewed['audit']['status'] != 'pass':
        reviewed['text'] = status_notice(doc['source'],doc['gaps'])
    messages = payload(reviewed['text'])
    binding = {k:request[k] for k in ('profile','session_id','session_key','turn_id')}
    binding.update(video=doc['source'], evidence_revision=doc['evidence_revision'],
                   requirement_revision=digest(reviewed['must_cover']))
    approval = {'binding':binding, 'payload_sha256':digest(messages),
                'kind':'summary' if reviewed['audit']['status'] == 'pass' else 'notice',
                'approved_at':time.time()}
    turn = {'binding':binding, 'question':request['question'], 'must_cover':reviewed['must_cover'],
            'summary_audit':reviewed['audit'], 'approved_delivery':approval, 'evidence_snapshot':clean,
            'recovery':doc.get('recovery'), 'review_calls':ledger, 'elapsed_seconds':round(time.monotonic()-started,2)}
    save_turn(job, request['turn_id'], turn)
    return {**reviewed, 'approval':approval, 'job':str(job), 'evidence_revision':doc['evidence_revision'],
            'source':doc['source'], 'gaps':doc['gaps'], 'recovery':doc.get('recovery'), 'elapsed_seconds':turn['elapsed_seconds']}


if __name__ == '__main__':
    try:
        print(json.dumps(execute(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}))
        raise SystemExit(1)
