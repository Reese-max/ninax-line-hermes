"""Source-bound video evidence. Text length is never a completeness signal."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

JOBS = Path('/workspace/video-timeline-pipeline/jobs')
POLICY = 'ninax-video-2.2'
HOSTS = {'instagram.com': 'instagram', 'facebook.com': 'facebook', 'fb.watch': 'facebook',
         'youtube.com': 'youtube', 'youtu.be': 'youtube', 'tiktok.com': 'tiktok',
         'x.com': 'x', 'twitter.com': 'x', 'threads.net': 'threads',
         'threads.com': 'threads', 'bilibili.com': 'bilibili', 'vimeo.com': 'vimeo',
         'douyin.com': 'douyin', 't.co':'x', 'b23.tv':'bilibili'}
PATHS = {'instagram':r'/(?:p|reel|reels|tv)/([^/]+)',
         'facebook':r'/(?:reel|reels|videos|share/[vr])/([^/]+)',
         'youtube':r'/(?:shorts|embed|live)/([^/]+)', 'tiktok':r'/@[^/]+/video/(\d+)',
         'x':r'/[^/]+/status/(\d+)', 'threads':r'/@[^/]+/post/([^/]+)',
         'bilibili':r'/video/([^/]+)', 'vimeo':r'/(\d+)', 'douyin':r'/video/(\d+)'}
SHORT_HOSTS = {'fb.watch','t.co','b23.tv','vm.tiktok.com','vt.tiktok.com','v.douyin.com'}


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        tmp = Path(output.name)
        try:
            os.chmod(tmp, 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(path)


@contextlib.contextmanager
def job_lock(job, deadline, name='evidence.lock'):
    with (Path(job) / name).open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('evidence_lock_timeout')
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run(args, deadline, *, timeout=None, input=None, cwd=None, env=None, is_current=None):
    remaining = deadline - time.monotonic()
    if timeout is not None:
        remaining = min(remaining, timeout)
    if remaining <= 0:
        raise TimeoutError('stage_budget_exhausted')
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, cwd=cwd, env=env)
    expires = time.monotonic()+remaining
    try:
        while True:
            if is_current is not None and not is_current():
                raise InterruptedError('turn_cancelled')
            remaining = expires-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('stage_budget_exhausted')
            try:
                stdout, stderr = proc.communicate(input=input, timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                input = None
    except BaseException:
        # Nested stages own process groups too; include their descendants in cancellation.
        import psutil
        with contextlib.suppress(psutil.NoSuchProcess):
            for child in reversed(psutil.Process(proc.pid).children(recursive=True)):
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def identity(url):
    try:
        u = urlsplit(url)
        host = (u.hostname or '').lower()
        platform = next((v for h, v in HOSTS.items() if host == h or host.endswith('.' + h)), '')
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443) or not platform:
            return None
        key = (parse_qs(u.query).get('v') or [''])[0] if platform in {'youtube','facebook'} else ''
        if not key:
            pattern = r'/([^/]+)' if host in SHORT_HOSTS or host=='youtu.be' else PATHS[platform]
            found = re.search(pattern,u.path)
            key = found.group(1) if found else ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{2,128}', key or ''):
            return None
        if key.lower() in {'watch', 'reel', 'reels', 'videos', 'video', 'explore', 'share', 'feed'}:
            return None
        canonical = urlunsplit(('https', host, u.path.rstrip('/'), 'v=' + key if platform in {'youtube','facebook'} and u.path.rstrip('/') == '/watch' else '', ''))
        return {'platform': platform, 'id': key, 'url': canonical}
    except (TypeError, ValueError):
        return None


def matches(job, url):
    wanted = identity(url)
    info = read_json(Path(job) / 'source.info.json')
    actual = identity(info.get('webpage_url') or '')
    return bool(wanted and actual and wanted['platform'] == actual['platform']
                and wanted['id'] == actual['id'])


def valid_job(job):
    job = Path(job).resolve()
    if not job.is_relative_to(JOBS.resolve()) or not job.is_dir():
        raise ValueError('invalid_job_directory')
    return job


def artifact_stamps(job):
    result = {}
    for name in ('source.info.json','apify.item.json','brightdata.item.json','transcript.json',
                 'visual.json','media-proof.json','cached-media.mp4','external-evidence.json'):
        path = Path(job)/name
        if path.is_file():
            stat = path.stat()
            result[name] = [stat.st_ino,stat.st_size,stat.st_mtime_ns]
    media = Path(read_json(Path(job)/'media-proof.json').get('file_path') or '/nonexistent')
    if media.is_file() and media.resolve().is_relative_to(Path(job).resolve()):
        stat = media.stat()
        result['decoded_media'] = [stat.st_ino,stat.st_size,stat.st_mtime_ns]
    return result


def evidence_revision(doc):
    keys = ('schema_version','policy','source','author','duration','media_sha256','evidence_status',
            'gaps','items','processed_ranges','limitations','artifact_stamps','related_sources','source_aliases')
    return digest({k:doc[k] for k in keys})


def snapshot(job, url):
    job = valid_job(job)
    info = read_json(job / 'source.info.json')
    raw = read_json(job / 'apify.item.json') or read_json(job / 'brightdata.item.json')
    speech = read_json(job / 'transcript.json')
    media = read_json(job / 'media-proof.json')
    visuals = read_json(job / 'visual.json', [])
    visuals = visuals if isinstance(visuals, list) else []
    duration = media.get('duration') or info.get('duration') or raw.get('videoDuration') or raw.get('duration')
    try:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            duration = None
    except (ValueError, TypeError):
        duration = None
    items, gaps = [], []
    source_url = (identity(info.get('webpage_url') or '') or identity(url) or {}).get('url', '')
    def add(kind, text, start=None, end=None):
        if isinstance(text, list):
            text = '\n'.join(str(x) for x in text)
        if isinstance(text, str) and text.strip():
            items.append({'id': f'E{len(items)+1}', 'kind': kind, 'text': text.strip(),
                          'start': start, 'end': end, 'source_url': source_url})
    caption = raw.get('caption') or raw.get('description') or info.get('description')
    add('caption', caption)
    segments = speech.get('segments') or []
    timestamp_errors = []
    for seg in segments:
        if seg.get('quality_issue'):
            continue
        try:
            start, end = float(seg['start']), float(seg['end'])
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end
                    and duration and end <= duration + 1):
                raise ValueError()
            add('speech', seg.get('text'), start, end)
        except (TypeError, KeyError, ValueError):
            timestamp_errors.append('invalid_speech_timestamp')
    if not segments:
        add('untimed_transcript', speech.get('text') or raw.get('transcript') or raw.get('transcription'))
    frame_times = []
    for frame in visuals:
        try:
            stamp = float(frame['timestamp'])
            if not (duration and math.isfinite(stamp) and 0 <= stamp <= duration + 0.1):
                raise ValueError()
            frame_times.append(stamp)
            add('visual_observation', frame.get('visual_summary'), stamp, stamp)
            add('visible_text', frame.get('visible_text'), stamp, stamp)
            add('visual_observation', frame.get('important_details'), stamp, stamp)
        except (TypeError, KeyError, ValueError):
            timestamp_errors.append('invalid_visual_timestamp')
    media_hash = media.get('sha256')
    audio_complete = bool(media_hash and speech.get('media_sha256') == media_hash
                          and speech.get('analysis_policy') == POLICY
                          and speech.get('processed_ranges') == [[0, media.get('audio_duration',duration)]]
                          and speech.get('status') in {'transcribed', 'no_speech_detected', 'no_audio_track'})
    if not matches(job, url):
        gaps.append('source_identity_unverified')
    if not duration:
        gaps.append('duration_unknown')
    if not media_hash or not media.get('file_stamp') or artifact_stamps(job).get('decoded_media') != media['file_stamp']:
        gaps.append('full_media_missing')
    declared = info.get('duration') or raw.get('videoDuration') or raw.get('duration')
    try:
        declared = float(declared)
        if duration and declared>0 and abs(declared-duration)>max(2,declared*.05):
            gaps.append('source_duration_mismatch')
    except (TypeError,ValueError):
        pass
    if not audio_complete:
        gaps.append('audio_not_fully_processed')
    if speech.get('quality_gaps') or speech.get('quality_checked') is not True:
        gaps.append('speech_quality_unresolved')
    visual_proof = media.get('visual') or {}
    if media.get('has_video', True) and not (frame_times and visual_proof.get('policy') == POLICY
            and visual_proof.get('sha256') == media_hash and visual_proof.get('complete')
            and duration and visual_proof.get('decoded_until',0) >= duration-1):
        gaps.append('visual_sampling_incomplete')
    gaps.extend(sorted(set(timestamp_errors)))
    related_sources = []
    external = read_json(job/'external-evidence.json')
    target = external.get('target') or {}
    wanted = identity(url) or {}
    if (external.get('verification',{}).get('method')=='creator-and-unique-timed-speech'
            and all(target.get(k)==wanted.get(k) for k in ('platform','id')) and identity(external.get('url',''))):
        related_sources.append({k:external[k] for k in ('url','author','relation','duration','verification')})
        for segment in external.get('segments',[]):
            try:
                start,end = float(segment['start']),float(segment['end'])
                if not (math.isfinite(start) and math.isfinite(end) and 0<=start<=end<=float(external['duration'])+1):
                    raise ValueError()
                if segment.get('text'):
                    items.append({'id':f'E{len(items)+1}','kind':'source_subtitle','text':segment['text'],
                                  'start':start,'end':end,'source_url':external['url'],
                                  'source_relation':external['relation'],'source_duration':external['duration']})
            except (KeyError,TypeError,ValueError):
                gaps.append('invalid_external_timestamp')
    content = [x for x in items if x['kind'] != 'caption']
    status = 'ready' if not gaps else ('partial' if content else 'insufficient')
    evidence = {'schema_version': 1, 'policy': POLICY, 'source': identity(url),
                'source_aliases':list(dict.fromkeys([source_url,(identity(url) or {}).get('url','')])),
                'author': info.get('uploader') or raw.get('ownerUsername') or raw.get('username'),
                'duration': duration, 'media_sha256': media_hash, 'evidence_status': status,
                'gaps': gaps, 'items': items, 'processed_ranges': {'audio': speech.get('processed_ranges', []),
                'visual_samples': sorted(set(frame_times)), 'audio_recovery':speech.get('recovery'),
                'speech_quality_gaps':speech.get('quality_gaps',[])},
                'related_sources':related_sources,
                'limitations': ['畫面為取樣觀察；未逐幀確認。', '自動語音辨識與畫面判讀可能有誤差。']}
    evidence['artifact_stamps'] = artifact_stamps(job)
    evidence['evidence_revision'] = evidence_revision(evidence)
    return evidence


def save_snapshot(job, evidence):
    with job_lock(job, time.monotonic() + 10):
        old = read_json(Path(job) / 'evidence.json')
        evidence = {**evidence, 'turns': old.get('turns', {})}
        atomic_json(Path(job) / 'evidence.json', evidence)
    return evidence


def save_turn(job, key, value):
    with job_lock(job, time.monotonic() + 10):
        doc = read_json(Path(job) / 'evidence.json')
        doc.setdefault('turns', {})[key] = value
        atomic_json(Path(job) / 'evidence.json', doc)
