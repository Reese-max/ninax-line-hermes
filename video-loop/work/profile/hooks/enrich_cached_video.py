#!/usr/bin/env python3
"""Fill media evidence with installed Whisper and video-pipeline facilities."""
import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

from video_evidence import POLICY, atomic_json, digest, job_lock, read_json, run, valid_job
import video_evidence

PIPELINE = Path('/workspace/video-timeline-pipeline')
STT_PYTHON = '/home/box/irisx-failover-restore/20260906/gate-c/venv/bin/python'
MAX_BYTES = 128 * 1024 * 1024


def transcribe_media(media):
    from faster_whisper import WhisperModel
    model = WhisperModel('small', device='cpu', compute_type='int8', local_files_only=True)
    segments, info = model.transcribe(str(media), vad_filter=True, temperature=0,
                                     condition_on_previous_text=False)
    kept = [{'start': s.start, 'end': s.end, 'text': s.text.strip(),
             'no_speech_prob': s.no_speech_prob,'avg_logprob':s.avg_logprob,
             'compression_ratio':s.compression_ratio} for s in segments
            if s.text.strip() and s.no_speech_prob <= 0.6]
    return {'text': ' '.join(s['text'] for s in kept), 'segments': kept,
            'language': info.language, 'language_probability': info.language_probability,
            'decoded_duration':info.duration}


def media_location(url):
    u = urlsplit(url)
    host = u.hostname or ''
    if (u.scheme != 'https' or u.username or u.password or u.port not in (None, 443)
            or not host.endswith(('.cdninstagram.com', '.fbcdn.net'))):
        raise ValueError('unsupported_media_host')
    return host, urlunsplit(('', '', u.path, u.query, ''))


def download_media(url, target, deadline):
    host, resource = media_location(url)
    if any(not ipaddress.ip_address(x[4][0]).is_global for x in socket.getaddrinfo(host, 443)):
        raise ValueError('unsafe_media_address')
    connection = http.client.HTTPSConnection(host, timeout=max(1, min(20, deadline-time.monotonic())))
    partial = None
    try:
        connection.request('GET', resource, headers={'User-Agent': 'Mozilla/5.0'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f'media_http_{response.status}')
        if int(response.getheader('Content-Length') or 0) > MAX_BYTES:
            raise ValueError('media_too_large')
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.part', delete=False) as output:
            partial = Path(output.name)
            total = 0
            while data := response.read(1024 * 1024):
                total += len(data)
                if total > MAX_BYTES or time.monotonic() >= deadline:
                    raise ValueError('media_download_limit')
                output.write(data)
        with partial.open('rb') as check:
            if b'ftyp' not in check.read(64):
                raise ValueError('invalid_mp4')
        partial.replace(target)
    finally:
        connection.close()
        if partial:
            partial.unlink(missing_ok=True)


def probe(media, deadline):
    result = run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(media)],
                 deadline, timeout=12)
    if result.returncode:
        raise ValueError('media_probe_failed')
    doc = json.loads(result.stdout)
    duration = float(doc.get('format', {}).get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('invalid_media_duration')
    with media.open('rb') as source:
        sha = hashlib.file_digest(source, 'sha256').hexdigest()
    kinds = {s.get('codec_type') for s in doc.get('streams', [])}
    audio = next((s for s in doc.get('streams',[]) if s.get('codec_type')=='audio'),{})
    stat = media.stat()
    return {'sha256': sha, 'file_path':str(media.resolve()), 'file_stamp':[stat.st_ino,stat.st_size,stat.st_mtime_ns],
            'duration': duration, 'has_audio': 'audio' in kinds, 'has_video': 'video' in kinds,
            'audio_duration':min(duration,float(audio.get('duration') or duration)) if audio else 0}


def fetch_media(url, target, deadline):
    receipt = target.parent/'media-fetch.json'
    attempts = read_json(receipt)
    key = digest([url,POLICY])
    if key in attempts:
        raise ValueError('same_media_url_already_attempted')
    attempts[key] = 'started'
    atomic_json(receipt,attempts)
    try:
        download_media(url,target,deadline)
    except Exception:
        attempts[key] = 'failed'
        atomic_json(receipt,attempts)
        raise
    attempts[key] = 'completed'
    atomic_json(receipt,attempts)


def provider_env():
    from dotenv import dotenv_values
    env = dict(os.environ)
    for path in ['/home/box/.opencode/.env','/home/box/.hermes/.env','/home/box/.grok/.env',str(PIPELINE/'.env')]:
        env.update({k:v for k,v in dotenv_values(path).items() if v is not None})
    return env


def weak_speech(segment):
    text = re.sub(r'\s+','',segment.get('text',''))
    return bool((len(text)>16 and re.search(r'(.{1,8})\1{7,}',text))
                or float(segment.get('compression_ratio') or 0)>2.4
                or float(segment.get('avg_logprob') or 0)<-0.8)


def repair_audio_worker(job):
    request = json.load(sys.stdin)
    clip = job/'audio.wav'
    extraction = run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(request['start']),
        '-i',request['media'],'-t',str(request['end']-request['start']),'-vn','-ar','16000','-ac','1',str(clip)],
        time.monotonic()+12)
    if extraction.returncode:
        raise ValueError('audio_recovery_extraction_failed')
    sys.path.insert(0,str(PIPELINE))
    import pipeline
    transcript,_ = pipeline.transcribe_all([{'index':0,'path':str(clip),'source_start':request['start'],
        'source_end':request['end'],'sha256':hashlib.sha256(clip.read_bytes()).hexdigest()}],
        os.environ['GROQ_API_KEY'],request['model'],'auto',1,job/'cache')
    return transcript


def repair_speech(job,media,proof,speech,deadline):
    bad = [s for s in speech.get('segments',[]) if weak_speech(s)]
    speech['quality_checked'] = True
    speech['quality_gaps'] = [[s['start'],s['end']] for s in bad]
    if not bad:
        return
    for segment in bad:
        segment['quality_issue'] = True
    start = max(0,min(s['start'] for s in bad)-1)
    end = min(proof['audio_duration'],max(s['end'] for s in bad)+1)
    # ponytail: one bounded recovery span; split sparse spans only if real long-video cases require it.
    if end-start>120 or deadline-time.monotonic()<10:
        return
    env = provider_env()
    if not env.get('GROQ_API_KEY'):
        return
    request = {'media':str(media),'start':start,'end':end,'model':env.get('GROQ_ASR_MODEL','whisper-large-v3')}
    recovery = job/('audio-recovery-'+digest([proof['sha256'],start,end,request['model']])[:16])
    recovery.mkdir(exist_ok=True)
    if not (recovery/'local.json').exists():
        atomic_json(recovery/'local.json',speech)
    receipt = recovery/'result.json'
    result = read_json(receipt)
    if not result:
        atomic_json(receipt,{'status':'started'})
        try:
            output = run([str(PIPELINE/'.venv/bin/python'),str(Path(__file__)),'--repair-audio',str(recovery)],
                deadline,timeout=35,input=json.dumps(request),env=env)
            result = {'status':'completed','transcript':json.loads(output.stdout)} if output.returncode==0 else {'status':'failed'}
        except Exception as exc:
            result = {'status':'failed','reason':type(exc).__name__}
        atomic_json(receipt,result)
    segments = result.get('transcript',{}).get('segments') or []
    if not segments or any(weak_speech(s) or not start-.02<=s['start']<=s['end']<=end+1 for s in segments):
        return
    speech['segments'] = sorted([s for s in speech['segments'] if s['end']<=start or s['start']>=end]+segments,
                                 key=lambda s:s['start'])
    speech.update(text=' '.join(s['text'] for s in speech['segments']),quality_gaps=[],
                  recovery={'provider':'groq','model':request['model'],'ranges':[[start,end]],'receipt':str(receipt)})


def vision_worker(job, cache=None):
    # Credentials are loaded by the Hermes parent, whose environment parser is already installed.
    os.environ.setdefault('MINIMAX_SUBSCRIPTION_KEY', os.environ.get('MINIMAX_API_KEY', ''))
    os.environ.setdefault('MINIMAX_MODEL', 'MiniMax-M3')
    sys.path.insert(0, str(PIPELINE))
    import pipeline
    doc = read_json(job / 'frame_candidates.json')
    candidates = doc.get('frames', []) if isinstance(doc, dict) else doc
    config = read_json(job/'manifest.json').get('config',{})
    ending = read_json(job/'ninax-end-frame.json')
    if ending and all(c['path'] != ending['path'] for c in candidates):
        candidates.append({**ending, 'hash':pipeline.average_hash(Path(ending['path'])), 'reasons':['last_frame']})
        candidates.sort(key=lambda c:c['timestamp'])
        candidates = pipeline.limit_items(candidates,int(config.get('max_vision_frames') or 8))
    previous = read_json(job/'ninax-existing-visual.json',[])
    remaining = max(0,int(config.get('max_vision_frames') or 8)-len(previous))
    candidates = [c for c in candidates if not any(abs(c['timestamp']-o['timestamp'])<.01 for o in previous)]
    candidates = pipeline.limit_items(candidates,remaining) if remaining else []
    observations = previous + pipeline.analyze_images(candidates, pipeline.minimax_subscription_key(),
                                           pipeline.minimax_model(), 8, cache or job / 'vision-cache')
    observations.sort(key=lambda o:o['timestamp'])
    atomic_json(job / 'reviewed-visual.json', observations)
    return {'frames': len(observations)}


def enrich_visuals(job, media, proof, deadline, max_frames=0):
    duration = proof['duration']
    count = max(8, min(40, max(max_frames, math.ceil(duration / 30))))
    old = read_json(job / 'media-proof.json').get('visual') or {}
    if (old.get('sha256') == proof['sha256'] and old.get('policy') == POLICY and old.get('complete')
            and old.get('decoded_until',0) >= proof['duration']-1 and old.get('limit',8) >= count):
        proof['visual'] = old
        return
    interval = max(0.25, min(5, duration / (2 * count)))
    prefix = 'media-analysis-' + proof['sha256'][:12] + '-' + POLICY
    root = job / (prefix + '-' + str(count))
    result = run(['/workspace/bin/video-pipeline', '--video', str(media), '--root', str(root),
                  '--local-only', '--max-vision-frames', str(count), '--frame-interval', str(interval),
                  '--workers', '1'], deadline, timeout=25)
    if result.returncode:
        raise ValueError('frame_pipeline_failed')
    manifests = sorted((root / 'jobs').glob('*/manifest.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    selected = next((p.parent for p in manifests if
        read_json(p).get('source', {}).get('resolved_video') == str(media)
        and (p.parent / 'frame_candidates.json').is_file()), None)
    if not selected:
        raise ValueError('unverified_visual_job')
    decoded_frames = len(list((selected/'frames').glob('frame_*.jpg')))
    if decoded_frames*interval < duration-max(1,interval*1.5):
        raise ValueError('video_decode_incomplete')
    # Fixed-rate extraction may stop several seconds before the ending. Sample it explicitly.
    ending_time = max(0,duration-0.25)
    ending_path = selected/'frames/ninax-ending.jpg'
    ending = run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(ending_time),
                  '-i',str(media),'-frames:v','1','-q:v','3',str(ending_path)],deadline,timeout=12)
    if ending.returncode or not ending_path.is_file() or not ending_path.stat().st_size:
        raise ValueError('ending_frame_unavailable')
    atomic_json(selected/'ninax-end-frame.json',{'path':str(ending_path),'timestamp':ending_time})
    previous = read_json(job/'visual.json',[]) if (old.get('sha256')==proof['sha256']
        and old.get('policy')==POLICY and old.get('complete') and old.get('decoded_until',0)>=duration-1) else []
    atomic_json(selected/'ninax-existing-visual.json',previous)
    cache = job/'vision-cache'
    cache.mkdir(exist_ok=True)
    for cached in job.glob(prefix+'*/jobs/*/vision-cache/*.json'):
        if not (cache/cached.name).exists():
            shutil.copy2(cached,cache/cached.name)
    result = run([str(PIPELINE / '.venv/bin/python'), str(Path(__file__)), '--vision', str(selected),
                  '--vision-cache',str(cache)],
                 deadline, timeout=65, env=provider_env())
    if result.returncode:
        raise ValueError('visual_analysis_failed')
    observations = read_json(selected / 'reviewed-visual.json', [])
    if not observations or not all(o.get('visual_summary') for o in observations):
        raise ValueError('empty_visual_evidence')
    atomic_json(job / 'visual.json', observations)
    proof['visual'] = {'sha256': proof['sha256'], 'policy': POLICY, 'complete': True,
                       'frames': len(observations), 'interval': interval,
                       'selection': 'installed pipeline pHash plus first/last frames', 'limit': count,
                       'decoded_frames':decoded_frames,'decoded_until':min(duration,decoded_frames*interval)}


def enrich(job, deadline, max_frames=0):
    job = valid_job(job)
    media = job / 'cached-media.mp4'
    item = read_json(job / 'apify.item.json') or read_json(job / 'brightdata.item.json')
    media_url = item.get('videoUrl') or item.get('video_url')
    if not media.is_file():
        manifest = read_json(job / 'manifest.json')
        candidate = Path(manifest.get('source', {}).get('resolved_video') or '/nonexistent')
        if not candidate.is_absolute():
            candidate = video_evidence.JOBS.parent / candidate
        if candidate.is_file() and candidate.resolve().is_relative_to(job):
            media = candidate.resolve()
        elif media_url:
            fetch_media(media_url, media, deadline)
        else:
            return {'ok': False, 'note': 'full_media_missing'}
    expected = read_json(job/'source.info.json').get('duration') or item.get('videoDuration') or item.get('duration')
    try:
        expected = float(expected)
    except (TypeError,ValueError):
        expected = 0
    try:
        proof = probe(media, deadline)
    except ValueError:
        proof = {}
    if not proof or (expected>0 and abs(expected-proof['duration'])>max(2,expected*.05)):
        if not media_url:
            raise ValueError('media_refresh_required')
        candidate = job/('recovered-media-'+digest(media_url)[:12]+'.mp4')
        if not candidate.is_file():
            fetch_media(media_url,candidate,deadline)
        recovered = probe(candidate,deadline)
        if expected>0 and abs(expected-recovered['duration'])>max(2,expected*.05):
            raise ValueError('source_duration_mismatch')
        if media.name=='cached-media.mp4':
            backup = job/('retained-media-'+hashlib.sha256(media.read_bytes()).hexdigest()[:12]+'.mp4')
            if not backup.exists():
                os.link(media,backup)
        candidate.replace(job/'cached-media.mp4')
        media,proof = job/'cached-media.mp4',recovered
        proof['file_path'] = str(media.resolve())
    old_proof = read_json(job / 'media-proof.json')
    if old_proof.get('sha256') == proof['sha256']:
        proof['visual'] = old_proof.get('visual', {})
    atomic_json(job / 'media-proof.json', proof)
    target = job / 'transcript.json'
    speech = read_json(target)
    audio_complete = (speech.get('media_sha256') == proof['sha256']
                      and speech.get('analysis_policy') == POLICY
                      and speech.get('quality_checked') is True
                      and speech.get('processed_ranges') == [[0, proof['audio_duration']]])
    if not audio_complete:
        if proof['has_audio']:
            result = run([STT_PYTHON, str(Path(__file__)), '--transcribe', str(media)], deadline, timeout=65)
            if result.returncode:
                raise ValueError('audio_analysis_failed')
            speech = json.loads(result.stdout)
            if abs(float(speech.get('decoded_duration') or 0)-proof['audio_duration'])>max(1,proof['audio_duration']*.01):
                raise ValueError('audio_decode_incomplete')
            speech['status'] = 'transcribed' if speech.get('text') else 'no_speech_detected'
        else:
            speech = {'text': '', 'segments': [], 'status': 'no_audio_track'}
        speech.update(provider='local-whisper-small-vad', media_sha256=proof['sha256'],
                      processed_ranges=[[0, proof['audio_duration']]],analysis_policy=POLICY, created_at=time.time())
        repair_speech(job,media,proof,speech,deadline)
        atomic_json(target, speech)
    if proof['has_video']:
        enrich_visuals(job, media, proof, deadline, max_frames)
        atomic_json(job / 'media-proof.json', proof)
    return {'ok': True, 'provider': speech.get('provider'), 'visual_frames': proof.get('visual', {}).get('frames', 0)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('job')
    parser.add_argument('--transcribe', action='store_true')
    parser.add_argument('--vision', action='store_true')
    parser.add_argument('--repair-audio',action='store_true')
    parser.add_argument('--max-frames',type=int,default=0)
    parser.add_argument('--vision-cache',type=Path)
    parser.add_argument('--seconds', type=float, default=100)
    parser.add_argument('--jobs-root')
    args = parser.parse_args()
    if args.jobs_root:
        video_evidence.JOBS = Path(args.jobs_root).resolve()
    try:
        if args.repair_audio:
            result = repair_audio_worker(Path(args.job))
        elif args.vision:
            result = vision_worker(Path(args.job),args.vision_cache)
        elif args.transcribe:
            result = transcribe_media(Path(args.job))
        else:
            deadline = time.monotonic()+args.seconds
            with job_lock(valid_job(args.job), deadline, 'enrichment.lock'):
                result = enrich(Path(args.job), deadline,args.max_frames)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'note': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        raise SystemExit(1)
