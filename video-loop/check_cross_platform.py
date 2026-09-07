"""Verify one public cross-platform candidate using cached media and local-only downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time


def prepare_fixture(source,job):
    """Copy source artifacts, never credentials, provider receipts or approved turns."""
    source=Path(source).resolve();job=Path(job).resolve()
    assert not job.exists(),'Use a new fixture path'
    assert not job.is_relative_to(source) and not source.is_relative_to(job),'Fixture must be isolated'
    proof=json.loads((source/'media-proof.json').read_text())
    media=Path(proof['file_path']).resolve()
    assert media.is_file() and media.is_relative_to(source),'Cached media must belong to the source job'
    job.mkdir(parents=True)
    for name in ('source.info.json','transcript.json','visual.json'):
        if (source/name).is_file():
            shutil.copy2(source/name,job/name)
    shutil.copy2(media,job/'cached-media.mp4')
    proof['file_path']=str(job/'cached-media.mp4')
    (job/'media-proof.json').write_text(json.dumps(proof))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hooks',type=Path,required=True)
    parser.add_argument('--source-job',type=Path,required=True)
    parser.add_argument('--job',type=Path,required=True)
    parser.add_argument('--candidate',required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    os.environ['NINAX_DISABLE_METERED_FETCH']='1'
    sys.path.insert(0,str(args.hooks.resolve()))
    import video_evidence as evidence
    import video_recovery as recovery
    source=args.source_job.resolve();job=args.job.resolve()
    assert not args.out.exists(),'Use a new receipt path'
    original=evidence.identity(evidence.read_json(source/'source.info.json').get('webpage_url',''))
    candidate=evidence.identity(args.candidate)
    assert original and candidate and original['platform']!=candidate['platform'],'Two supported platforms are required'
    prepare_fixture(source,job)
    evidence.JOBS=recovery.JOBS=job.parent
    doc=evidence.snapshot(job,original['url'])
    assert doc['media_sha256'] and doc['duration']
    record={'status':'RUNNING','pid':os.getpid(),'started_at':time.time(),'source':original,
            'candidate':candidate,'source_media_sha256':doc['media_sha256'],
            'implementation_sha256':hashlib.sha256(Path(recovery.__file__).read_bytes()).hexdigest(),
            'real_line_message_sent':False,'metered_fetch_disabled':True,'download_mode':'local-only','ledger':[]}
    evidence.atomic_json(args.out,record)
    started=time.monotonic()
    try:
        ok=recovery.recover_original(doc,[{'url':candidate['url'],'relation':'candidate_original'}],
                                    job,time.monotonic()+100,record['ledger'])
        external=evidence.read_json(job/'external-evidence.json')
        record.update(status='PASS' if ok else 'UNVERIFIED',verification=external.get('verification'),
                      relation=external.get('relation'),candidate_duration=external.get('duration'))
        if ok:
            assert external['source_media_sha256']==doc['media_sha256'] and external['target']==doc['source']
            if external['relation']=='matching_visual_version':
                assert external['verification']['creator_verified'] is False
    except Exception as exc:
        record.update(status='FAIL',error=type(exc).__name__+': '+str(exc)[:300])
        raise
    finally:
        record['seconds']=round(time.monotonic()-started,2)
        evidence.atomic_json(args.out,record)
        print(json.dumps(record,ensure_ascii=False))
    return 0 if record['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
