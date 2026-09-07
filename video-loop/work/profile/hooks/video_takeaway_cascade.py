#!/usr/bin/env python3
"""Collect first-pass evidence, then recover only what the evidence says is missing."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time
from urllib.parse import urlsplit

import video_evidence as evidence
from video_evidence import PIPELINE_COMMAND, atomic_json, digest, identity, job_lock, matches, read_json, run, snapshot
from video_recovery import recover_original, search

ROOT = Path(__file__).parent
JOBS = evidence.JOBS


def find_job(url):
    if not JOBS.is_dir():
        return None
    choices = [p for p in JOBS.iterdir() if p.is_dir() and matches(p, url)]
    return max(choices, key=lambda p: (bool(read_json(p/'media-proof.json').get('sha256')),
               (p/'source.info.json').stat().st_mtime), default=None)


def stage(name, args, deadline, ledger, timeout):
    entry = {'stage': name, 'status': 'started'}
    ledger.append(entry)
    started = time.monotonic()
    try:
        result = run(args, deadline, timeout=timeout)
        entry['status'] = 'completed' if result.returncode == 0 else 'failed'
        if result.returncode:
            entry['exit_code'] = result.returncode
        try:
            doc = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            doc = {}
        if doc.get('note') or doc.get('reason'):
            entry['reason'] = doc.get('note') or doc['reason']
        return doc
    except Exception as exc:
        entry.update(status='timeout' if 'Timeout' in type(exc).__name__ else 'failed',
                     reason=type(exc).__name__)
        return {}
    finally:
        entry['seconds'] = round(time.monotonic()-started, 2)


def enrich(job, deadline, ledger, max_frames=0):
    return stage('media_analysis', [sys.executable, str(ROOT/'enrich_cached_video.py'), str(job),
                 '--jobs-root', str(JOBS), '--seconds', str(max(1, deadline-time.monotonic())),
                 '--max-frames',str(max_frames)], deadline, ledger, 100)


def content_revision(doc):
    # File rewrites invalidate an approval, but are not newly acquired content.
    content={k:doc[k] for k in ('source','media_sha256','duration','items','gaps','processed_ranges','related_sources')}
    content['processed_ranges']={k:v for k,v in doc['processed_ranges'].items() if k!='semantic_recovery'}
    return digest(content)


def expand_visuals(job, doc, deadline, ledger):
    path = job/'visual-refresh.json'
    with job_lock(job,deadline,'visual-refresh.lock'):
        attempts = read_json(path)
        previous = read_json(job/'media-proof.json').get('visual',{}).get('limit',8)
        for count in (16,32,40):
            key = digest([doc['media_sha256'],count,evidence.POLICY])
            if count <= previous or key in attempts:
                continue
            attempts[key] = {'status':'started','max_frames':count}
            atomic_json(path,attempts)
            result = enrich(job,min(deadline,time.monotonic()+100),ledger,count)
            attempts[key].update(status='completed' if result.get('ok') else 'failed')
            atomic_json(path,attempts)
            return snapshot(job,doc['source']['url'])
    ledger.append({'stage':'visual_refresh','status':'skipped','reason':'sampling_options_exhausted'})
    return doc


def placeholder(url):
    source = identity(url)
    if not source:
        raise ValueError('unsupported_video_url')
    job = JOBS / f"ninax-{source['platform']}-{source['id']}"
    job.mkdir(parents=True, exist_ok=True)
    if not (job/'source.info.json').exists():
        atomic_json(job/'source.info.json', {'webpage_url': source['url'], 'id': source['id']})
    return job


def cascade(url, *, seconds=210, force=False):
    deadline = time.monotonic() + min(210, seconds)
    if not identity(url):
        raise ValueError('unsupported_video_url')
    ledger = []
    requested_url = url
    parsed = urlsplit(url)
    if parsed.hostname in evidence.SHORT_HOSTS or '/share/' in parsed.path:
        resolved = stage('resolve_video_url',[sys.executable,str(ROOT/'video_recovery.py'),'--resolve',url],deadline,ledger,10)
        if identity(resolved.get('url') or ''):
            url = resolved['url']
    video = identity(url)
    request_state = JOBS/'.ninax-recovery'/(digest([video['platform'],video['id']])+'.json')
    job = find_job(url)
    if not job:
        first_deadline = min(deadline, time.monotonic()+70)
        stage('initial_local_fetch', [PIPELINE_COMMAND, '--url', url, '--local-only',
              '--root', str(JOBS.parent), '--workers', '1'], first_deadline, ledger, 70)
        job = find_job(url) or placeholder(url)
    doc = snapshot(job, url)
    initial_revision = content_revision(doc)
    if doc['evidence_status'] == 'ready':
        if force:
            doc = expand_visuals(job,doc,deadline,ledger)
        new_evidence = initial_revision != content_revision(doc)
        doc.update(recovery={'status': 'completed' if new_evidence else 'cache_sufficient',
                             'attempts': ledger, 'new_evidence': new_evidence,
                             'requested_refresh':force,'requested_url':requested_url})
        return package(job, doc)
    # Existing bytes take priority over searching for another copy.
    if 'full_media_missing' not in doc['gaps'] or (job/'cached-media.mp4').exists():
        enrich(job, min(deadline, time.monotonic()+100), ledger)
        doc = snapshot(job, url)
    if doc['evidence_status'] != 'ready' and time.monotonic() < deadline-15:
        needs_media = bool({'full_media_missing','source_duration_mismatch'} & set(doc['gaps'])) or any(
            x['stage']=='media_analysis' and x.get('reason') in {'media_refresh_required','media_probe_failed'} for x in ledger)
        candidates = []
        if needs_media:
            recovery_deadline = min(deadline-80, time.monotonic()+40)
            if recovery_deadline > time.monotonic():
                candidates = search(doc, recovery_deadline, ledger)
            recover_original(doc,candidates,job,min(deadline-70,time.monotonic()+90),ledger)
            stage('refresh_original_metadata', [sys.executable, str(ROOT/'video_recovery.py'), '--refresh',
                  url, '--job', str(job), '--seconds', '20'], min(deadline-70, time.monotonic()+22), ledger, 22)
            # No candidate becomes source evidence merely because its title sounds related.
            doc['candidate_sources'] = candidates
            media_result = enrich(job, min(deadline, time.monotonic()+100), ledger)
            if not media_result.get('ok') and ({'full_media_missing','source_duration_mismatch'} & set(snapshot(job, url)['gaps'])):
                fetched = stage('single_metered_fetch', [sys.executable, str(ROOT/'video_recovery.py'), '--fetch',
                    url, '--state', str(request_state), '--jobs-root', str(JOBS), '--seconds', '35'],
                    min(deadline-65, time.monotonic()+40), ledger, 40)
                new_job = Path(fetched.get('job') or '/nonexistent')
                if fetched.get('ok') and matches(new_job, url):
                    # Keep one evidence/turn ledger for this video while reusing provider serializers.
                    if new_job != job:
                        for name in ('source.info.json','apify.item.json','brightdata.item.json'):
                            if (new_job/name).is_file():
                                atomic_json(job/name, read_json(new_job/name))
                if time.monotonic() < deadline-5:
                    enrich(job, deadline, ledger)
        elif time.monotonic() < deadline-5 and not any(x['stage'] == 'media_analysis' and
                                                     x['status'] != 'completed' for x in ledger):
            enrich(job, deadline, ledger)
        refreshed = snapshot(job, url)
        refreshed['candidate_sources'] = candidates
        doc = refreshed
    doc['recovery'] = {'status': 'completed' if doc['evidence_status'] == 'ready' else 'bounded_partial',
                       'attempts': ledger, 'new_evidence': initial_revision != content_revision(doc),
                       'requested_refresh': force, 'remote_task': read_json(request_state)}
    return package(job, doc)


def package(job, doc):
    evidence.save_snapshot(job, doc)
    return {'ok': doc['evidence_status'] == 'ready', 'job': str(job),
            'url': (doc.get('source') or {}).get('url'), 'evidence': doc,
            'draft': json.dumps({k:v for k,v in doc.items() if k != 'turns'}, ensure_ascii=False),
            'note': doc['evidence_status']}


def repair_requested(job, doc, requests, deadline, ledger):
    entry={'stage':'review_requested_recovery','status':'started','targets':requests}
    ledger.append(entry)
    started=time.monotonic()
    before=content_revision(doc)
    try:
        if any(r['kind']=='original' for r in requests):
            attempts=doc.get('recovery',{}).get('attempts',[])+ledger
            queries=max(0,3-sum(a['stage']=='search' for a in attempts))
            slots=max(0,3-sum(a['stage']=='verify_original_source' for a in attempts))
            candidates=search(doc,min(deadline-20,time.monotonic()+25),ledger,queries) if queries else doc.get('candidate_sources',[])
            recover_original(doc,candidates[:slots],job,deadline,ledger)
        targets=[r for r in requests if r['kind']!='original']
        output=run([sys.executable,str(ROOT/'enrich_cached_video.py'),str(job),'--targets',
                    '--jobs-root',str(JOBS),'--seconds',str(max(1,deadline-time.monotonic()))],
                   deadline,timeout=60,input=json.dumps(targets)) if targets else None
        result=json.loads(output.stdout) if output and output.returncode==0 else {}
        entry.update(status='completed' if result.get('ok') else 'unchanged',result=result)
    except Exception as exc:
        entry.update(status='failed',reason=type(exc).__name__)
    finally:
        entry['seconds']=round(time.monotonic()-started,2)
    refreshed=snapshot(job,doc['source']['url'])
    entry['new_evidence']=before!=content_revision(refreshed)
    if entry['new_evidence']:
        entry['status']='completed'
    refreshed['recovery']={**doc.get('recovery',{}),'semantic_recovery':entry,
                           'new_evidence':before!=content_revision(refreshed)}
    package(job,refreshed)
    return refreshed


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--seconds', type=float, default=210)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--jobs-root')
    args = parser.parse_args()
    if args.jobs_root:
        JOBS = evidence.JOBS = Path(args.jobs_root).resolve()
    try:
        print(json.dumps(cascade(args.url, seconds=args.seconds, force=args.force), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False, 'note':type(exc).__name__}))
        raise SystemExit(1)
