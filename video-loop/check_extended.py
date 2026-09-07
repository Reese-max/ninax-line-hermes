"""Real prepared long media + native model/payload; never calls the LINE API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

parser=argparse.ArgumentParser()
parser.add_argument('--job',type=Path,required=True)
parser.add_argument('--profile',type=Path,required=True,help='Existing model credentials; no messaging')
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args()
os.environ['HERMES_HOME']=str(args.profile)
os.environ['NINAX_DISABLE_METERED_FETCH']='1'
from dotenv import load_dotenv
load_dotenv(args.profile/'.env')
BASE=Path(__file__).parent
sys.path.insert(0,str(BASE/'work/profile/hooks'))
import video_workflow as workflow
import video_evidence as evidence
import video_review as review

manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (BASE/'work/profile/hooks').glob('video_*.py')}
receipt={'status':'RUNNING','implementation':manifest,'real_line_message_sent':False,'metered_fetch_disabled':True,'turns':[]}
evidence.atomic_json(args.out,receipt)
evidence.JOBS=workflow.cascade.JOBS=args.job.parent
url=json.loads((args.job/'source.info.json').read_text())['webpage_url']
doc=evidence.snapshot(args.job,url)
assert doc['duration']>300 and 'full_media_missing' not in doc['gaps']
receipt.update(duration=doc['duration'],sections=len(review.evidence_sections(doc)),source_status=doc['evidence_status'])
started=time.monotonic()
try:
    # Recovery can add evidence and split a section; allow bounded continuation beyond three chunks.
    for index in range(8):
        result=workflow.execute({'url':url,'question':'完整整理這支影片的主要內容、重要數字與條件，以及最後結論。',
            'profile':str(BASE/'isolated-long-review'),'session_id':'extended-fixture','session_key':'extended-fixture',
            'turn_id':'extended-'+str(os.getpid())+'-'+str(index),'jobs_root':str(args.job.parent)})
        receipt['turns'].append({'status':result['audit']['status'],'reason':result['audit'].get('reason'),
             'seconds':result['elapsed_seconds'],'progress':result.get('progress'),
             'new_evidence':(result.get('recovery') or {}).get('new_evidence'),
             'method':result['audit'].get('method'),'review_policy':result['review_policy']})
        receipt['sections']=(result.get('progress') or {}).get('total',receipt['sections'])
        evidence.atomic_json(args.out,receipt)
        if result['audit']['status']=='pass':
            break
        if result['audit'].get('reason') not in {'chunk_review_pending','source_recovery_requested'}:
            raise AssertionError('long review did not pass: '+json.dumps(result['audit'],ensure_ascii=False)[:700])
    assert result['audit']['status']=='pass'
    assert result['audit']['method']=='independent_sections_and_final_integration'
    assert result['progress']['completed']==result['progress']['total']>=2
    assert evidence.digest(review.payload(result['text']))==result['approval']['payload_sha256']
    saved=evidence.read_json(args.job/'evidence.json')['turns'][result['approval']['binding']['turn_id']]
    assert saved['summary_audit']['status']=='pass'
    receipt.update(status='PASS',elapsed_seconds=round(time.monotonic()-started,2),payload_sha256=result['approval']['payload_sha256'],
                   summary=result['text'],quality=workflow.quality_report(args.job.parent))
except Exception as exc:
    receipt.update(status='FAIL',error=str(exc)[:800])
    raise
finally:
    evidence.atomic_json(args.out,receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k not in {'summary','implementation'}},ensure_ascii=False))
