"""Bounded, gap-directed recovery; persist an accepted remote task before polling it."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from video_evidence import HERMES, JOBS, PIPELINE, PIPELINE_COMMAND, atomic_json, digest, identity, job_lock, read_json, run


def resolve_video_url(url):
    import urllib.request
    import socket
    import ipaddress
    from urllib.parse import urlsplit
    from video_evidence import SHORT_HOSTS
    parsed = urlsplit(url)
    if parsed.hostname not in SHORT_HOSTS and '/share/' not in parsed.path:
        return url
    def allowed(target):
        if not identity(target):
            raise ValueError('unsafe_video_redirect')
        host = urlsplit(target).hostname
        if any(not ipaddress.ip_address(x[4][0]).is_global for x in socket.getaddrinfo(host,443)):
            raise ValueError('unsafe_video_redirect')
    class Redirect(urllib.request.HTTPRedirectHandler):
        max_redirections = 4
        def redirect_request(self,request,fp,code,msg,headers,newurl):
            allowed(newurl)
            return super().redirect_request(request,fp,code,msg,headers,newurl)
    allowed(url)
    request = urllib.request.Request(url,method='HEAD',headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.build_opener(Redirect()).open(request,timeout=5) as response:
        resolved = response.geturl()
        allowed(resolved)
        return identity(resolved)['url']


def query_seeds(evidence):
    source = evidence.get('source') or {}
    author = str(evidence.get('author') or '').strip()
    texts = [x['text'] for x in evidence.get('items', []) if x['kind'] in
             {'caption', 'speech', 'untimed_transcript', 'visible_text'}]
    unique = next((re.sub(r'https?://\S+|[#@]\S+', '', t).strip() for t in texts if len(t.strip()) >= 8), '')
    # Only public source text; never include the user's private chat in a search query.
    seeds = [f'"{source.get("id", "")}" {author}', f'{author} "{unique[:60]}"',
             f'{author} {unique[:100]} 完整影片']
    return list(dict.fromkeys(s.strip() for s in seeds if len(s.strip()) >= 6))[:3]


def search_worker(query):
    import urllib.request
    # The discovered social vertical has no Instagram/YouTube type; search public source clues generally.
    try:
        body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'search','arguments':{
            'query':query,'domain':'general','max_results':3}}}
        headers={'Content-Type':'application/json','X-Anysearch-Client':'skill/3.0.1'}
        if os.environ.get('ANYSEARCH_API_KEY'):
            headers['Authorization']='Bearer '+os.environ['ANYSEARCH_API_KEY']
        request=urllib.request.Request('https://api.anysearch.com/mcp',data=json.dumps(body).encode(),headers=headers)
        with urllib.request.urlopen(request,timeout=8) as response:
            data=json.loads(response.read())
        result=data.get('result',{})
        if data.get('error') or result.get('isError') or not result.get('content'):
            raise ValueError('anysearch_service_error')
        text='\n'.join(c.get('text','') for c in result['content'] if c.get('type')=='text')
        candidates=[{'title':title,'url':url} for title,url in re.findall(
            r'### \d+\.\s*([^\n]+)\n-\s*\*\*URL\*\*:\s*(https://\S+)',text)]
        return {'backend':'anysearch','ok':True,'candidates':candidates[:3]}
    except Exception as exc:
        fallback_reason='anysearch_'+type(exc).__name__
    sys.path.insert(0, str(HERMES))
    from tools.web_tools import web_search_tool, _get_search_backend
    result = json.loads(web_search_tool(query, limit=3))
    return {'backend': _get_search_backend(), 'ok': result.get('success', False),
            'candidates': result.get('data', {}).get('web', [])[:3],'fallback_reason':fallback_reason}


def search(evidence, deadline, ledger, limit=3):
    candidates = []
    seen = set()
    for query in query_seeds(evidence)[:max(0,min(3,limit))]:
        if time.monotonic() >= deadline or len(candidates) >= 3:
            break
        entry = {'stage': 'search', 'query': query, 'status': 'started'}
        ledger.append(entry)
        try:
            result = run([sys.executable, str(Path(__file__)), '--search', query], deadline, timeout=12)
            doc = json.loads(result.stdout) if result.returncode == 0 else {}
            entry.update(status='completed' if doc.get('ok') else 'failed', backend=doc.get('backend'))
            if doc.get('fallback_reason'):
                entry['reason']=doc['fallback_reason']
            for hit in doc.get('candidates', []):
                candidate = identity(hit.get('url', ''))
                if not candidate or candidate['url'] in seen:
                    continue
                seen.add(candidate['url'])
                original = evidence.get('source') or {}
                same = all(candidate.get(k) == original.get(k) for k in ('platform', 'id'))
                candidates.append({'url': candidate['url'], 'title': str(hit.get('title') or '')[:300],
                                   'description': str(hit.get('description') or '')[:1500],
                                   'relation': 'same_video' if same else 'unverified_candidate'})
                if len(candidates) >= 3:
                    break
        except Exception as exc:
            entry.update(status='failed', reason=type(exc).__name__)
    return candidates


def metadata(url, deadline):
    result = run([str(PIPELINE/'.venv/bin/python'),'-m','yt_dlp','--no-playlist', '--skip-download', '--dump-single-json', '--no-warnings',
                  '--socket-timeout', '8', '--retries', '0', '--', url], deadline, timeout=22)
    if result.returncode:
        return {}
    info = json.loads(result.stdout)
    wanted, actual = identity(url), identity(info.get('webpage_url') or '')
    if not wanted or not actual or any(wanted[k] != actual[k] for k in ('platform', 'id')):
        return {}
    return info


def refresh_metadata(url, job, deadline):
    # yt-dlp obtains a fresh page/CDN URL without downloading an unrelated full video.
    info=metadata(url,deadline)
    if not info:
        return False
    urls = [f.get('url') for f in info.get('formats', []) if f.get('ext') == 'mp4'
            and f.get('acodec') != 'none' and f.get('vcodec') != 'none']
    if not urls:
        return False
    from enrich_cached_video import media_location
    chosen = next((u for u in reversed(urls) if u and _allowed_media(u, media_location)), None)
    if not chosen:
        return False
    raw = read_json(job / 'apify.item.json') or read_json(job / 'brightdata.item.json')
    raw.pop('videoUrl',None)  # Do not let a stale Apify URL override the refreshed URL.
    raw.update(video_url=chosen)
    target = job / ('apify.item.json' if (job / 'apify.item.json').exists() else 'brightdata.item.json')
    atomic_json(target, raw)
    atomic_json(job/'source.info.json',{**read_json(job/'source.info.json'),**info})
    return True


def _allowed_media(url, validator):
    try:
        validator(url)
        return True
    except ValueError:
        return False


def align_transcripts(evidence, info, transcript):
    """A matching title is insufficient: require creator and ordered, time-aligned speech anchors."""
    normalize = lambda s: re.sub(r'[^\w\u4e00-\u9fff]','',str(s).casefold())
    author = normalize(evidence.get('author') or '')
    if not author or author not in {normalize(info.get('uploader')),normalize(info.get('uploader_id'))}:
        return None
    source_speech = [x for x in evidence.get('items',[]) if x['kind']=='speech' and len(normalize(x['text']))>=20]
    target_segments = transcript.get('segments') or []
    joined = ''
    boundaries = []
    for segment in target_segments:
        normalized = normalize(segment.get('text',''))
        boundaries.append((len(joined),len(normalized),segment))
        joined += normalized
    matches = []
    seen = set()
    for source in source_speech:
        anchor = normalize(source['text'])
        if anchor in seen:
            continue
        seen.add(anchor)
        if joined.count(anchor)==1:
            position = joined.index(anchor)
            offset,size,target = next(x for x in boundaries if x[0]<=position<x[0]+x[1])
            target_start = float(target['start'])+(position-offset)/size*(float(target['end'])-float(target['start']))
            matches.append({'source_start':source['start'],'target_start':target_start,
                            'offset':target_start-source['start'],'characters':len(anchor)})
    if len(matches)<2 or sum(x['characters'] for x in matches)<60:
        return None
    matches.sort(key=lambda x:x['source_start'])
    if (matches[-1]['source_start']-matches[0]['source_start']<5 or
            any(a['target_start']>=b['target_start'] for a,b in zip(matches,matches[1:])) or
            max(x['offset'] for x in matches)-min(x['offset'] for x in matches)>3):
        return None
    return {'method':'creator-and-unique-timed-speech','anchors':matches,
            'offset_seconds':sum(x['offset'] for x in matches)/len(matches)}


def align_visuals(source, target, duration):
    """Content continuity is evidence of a matching version, never proof of creator ownership."""
    def informative(frame):
        value=frame.get('hash','')
        stamp=frame.get('timestamp')
        return (isinstance(stamp,(int,float)) and math.isfinite(stamp) and stamp>=0
                and len(value)==64 and set(value)<={'0','1'} and 12<=value.count('1')<=52)
    anchors=[]
    for frame in source:
        if not informative(frame) or frame['timestamp']>duration or any(sum(a!=b for a,b in zip(frame['hash'],old['hash']))<=8 for old in anchors):
            continue
        scores=sorted((sum(a!=b for a,b in zip(frame['hash'],other['hash'])),other['timestamp'])
                      for other in target if informative(other))
        if (not scores or scores[0][0]>4 or any(distance-scores[0][0]<3 and abs(stamp-scores[0][1])>1.5
                                              for distance,stamp in scores[1:])):
            continue
        anchors.append({'source_start':frame['timestamp'],'target_start':scores[0][1],
                        'offset':scores[0][1]-frame['timestamp'],'hash':frame['hash'],'distance':scores[0][0]})
    anchors.sort(key=lambda x:x['source_start'])
    if (len(anchors)<4 or anchors[-1]['source_start']-anchors[0]['source_start']<max(3,duration*.5)
            or any(a['target_start']>=b['target_start'] for a,b in zip(anchors,anchors[1:]))
            or max(a['offset'] for a in anchors)-min(a['offset'] for a in anchors)>1.5):
        return None
    return {'method':'unique-timed-visual-sequence','creator_verified':False,
            'anchors':[{k:v for k,v in a.items() if k!='hash'} for a in anchors],
            'offset_seconds':sum(a['offset'] for a in anchors)/len(anchors)}


def visual_match_worker(request, deadline):
    from enrich_cached_video import probe
    sys.path.insert(0,str(PIPELINE))
    import pipeline
    sequences=[]
    durations=[]
    for name in ('source','candidate'):
        media=Path(request[name]).resolve()
        root=Path(request['root']).resolve()
        if not media.is_file() or not media.is_relative_to(root):
            raise ValueError('unbound_visual_candidate')
        proof=probe(media,deadline)
        if name=='source' and proof['sha256']!=request.get('expected_source_sha256'):
            raise ValueError('visual_source_changed')
        durations.append(proof['duration'])
        interval=max(.25,proof['duration']/(32 if name=='source' else 240))
        frames=root/('match-frames-'+digest([proof['sha256'],interval])[:16])
        frames.mkdir(exist_ok=True)
        cache=frames/('hashes-'+str(interval)+'.json')
        sequence=read_json(cache,[])
        if not sequence:
            output=run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(media),
                        '-vf',f'fps=1/{interval},scale=160:-2','-frames:v','240',str(frames/'frame-%04d.jpg')],deadline,timeout=20)
            if output.returncode:
                raise ValueError('candidate_decode_failed')
            sequence=[{'timestamp':(int(p.stem.split('-')[-1])-1)*interval,'hash':pipeline.average_hash(p)}
                      for p in sorted(frames.glob('frame-*.jpg'))]
            atomic_json(cache,sequence)
        sequences.append(sequence)
    return align_visuals(*sequences,durations[0])


def recover_original(evidence, candidates, job, deadline, ledger):
    from video_evidence import matches
    visual_attempted=False
    for candidate in candidates[:3]:
        if candidate['relation']=='same_video' or time.monotonic()>=deadline-5:
            continue
        root = job/('source-candidate-'+digest(candidate['url'])[:12])
        entry = {'stage':'verify_original_source','url':candidate['url'],'status':'started'}
        ledger.append(entry)
        try:
            info=metadata(candidate['url'],deadline)
            if not info:
                entry.update(status='unverified',reason='source_metadata_unavailable')
                continue
            transcript={}
            # Missing or rate-limited captions cannot block a silent video's visual comparison.
            if any(x['kind']=='speech' and len(x.get('text',''))>=20 for x in evidence.get('items',[])):
                try:
                    run([PIPELINE_COMMAND,'--url',candidate['url'],'--no-video',
                         '--local-only','--root',str(root),'--workers','1'],deadline,timeout=20)
                except TimeoutError:
                    entry['caption_status']='timeout'
                selected = next((p.parent for p in (root/'jobs').glob('*/source.info.json')
                                 if matches(p.parent,candidate['url'])),None)
                if selected:
                    transcript=read_json(selected/'transcript.json')
                entry.setdefault('caption_status','available' if transcript.get('segments') else 'unavailable')
            else:
                entry['caption_status']='skipped_without_source_speech'
            alignment = align_transcripts(evidence,info,transcript)
            duration = float(info.get('duration') or 0)
            if not math.isfinite(duration) or duration<=0:
                continue
            if not alignment and not visual_attempted and evidence.get('media_sha256') and duration<=1200 and deadline-time.monotonic()>12:
                visual_attempted=True
                # One local-only candidate per turn; enforce the byte ceiling while its process runs.
                result=run([PIPELINE_COMMAND,'--url',candidate['url'],'--local-only','--root',str(root),
                            '--workers','1'],deadline,timeout=35,
                           is_current=lambda:sum(p.stat().st_size for p in root.rglob('*') if p.is_file())<=128*1024*1024)
                selected = next((p.parent for p in (root/'jobs').glob('*/source.info.json')
                                 if matches(p.parent,candidate['url'])),None)
                if not selected:
                    entry.update(status='unverified',reason='candidate_media_unavailable')
                    continue
                candidate_media=Path(read_json(selected/'manifest.json').get('source',{}).get('resolved_video') or '/nonexistent')
                if not candidate_media.is_absolute():
                    candidate_media=root/candidate_media
                source_media=Path(read_json(job/'media-proof.json').get('file_path') or '/nonexistent')
                if (selected/'.download-complete').is_file() and candidate_media.is_file() and candidate_media.resolve().is_relative_to(selected.resolve()):
                    output=run([str(PIPELINE/'.venv/bin/python'),str(Path(__file__)),'--visual-match'],deadline,timeout=35,
                               input=json.dumps({'source':str(source_media),'candidate':str(candidate_media),'root':str(job),
                                                 'expected_source_sha256':evidence['media_sha256']}))
                    if output.returncode==0:
                        alignment=json.loads(output.stdout)
            if not alignment or duration < float(evidence.get('duration') or 0):
                entry.update(status='rejected',reason='identity_or_alignment_mismatch')
                continue
            verified = {'target':evidence['source'],'url':candidate['url'],'author':info.get('uploader'),
                        'source_media_sha256':evidence.get('media_sha256'),
                        'relation':('matching_visual_version' if alignment['method']=='unique-timed-visual-sequence' else
                                    'longer_original' if duration>float(evidence.get('duration') or 0)+3 else 'same_content_copy'),
                        'duration':duration,'verification':alignment,'segments':transcript.get('segments',[])}
            atomic_json(job/'external-evidence.json',verified)
            entry.update(status='verified',relation=verified['relation'],anchors=len(alignment['anchors']))
            candidate['relation']=verified['relation']
            return True
        except Exception as exc:
            entry.update(status='unverified',reason=type(exc).__name__)
    return False


def metered_fetch(url, state_path, deadline, jobs_root):
    """One single-URL async request. Uncertain acceptance is never retriggered.

    A provider POST additionally requires a one-shot grant already recorded in
    the state receipt by authorize_metered_fetch; the kill-switch stays first.
    """
    if os.environ.get('NINAX_DISABLE_METERED_FETCH')=='1':
        return {'ok':False,'reason':'metered_fetch_disabled'}
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True,exist_ok=True)
    with job_lock(state_path.parent,deadline,state_path.stem+'.lock'):
        return _metered_fetch(url,state_path,deadline,jobs_root)


def authorize_metered_fetch(url, state_path, deadline, seconds=600):
    """Record a positive one-shot authorization for this exact source in the receipt.

    The grant binds the source identity, expires, and is consumed atomically
    before any provider POST. An in-flight or ambiguous submission can never
    be re-authorized, so an uncertain acceptance still cannot be retriggered.
    """
    if os.environ.get('NINAX_DISABLE_METERED_FETCH')=='1':
        return {'ok':False,'reason':'metered_fetch_disabled'}
    wanted = identity(url)
    if not wanted or wanted['platform'] != 'instagram':
        raise ValueError('metered_source_not_supported')
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True,exist_ok=True)
    with job_lock(state_path.parent,deadline,state_path.stem+'.lock'):
        state = read_json(state_path)
        old_identity = identity(state.get('url') or '')
        if state and (not old_identity or any(old_identity[k] != wanted[k] for k in ('platform','id'))):
            raise ValueError('provider_request_identity_mismatch')
        if (state.get('status') in {'starting', 'pending', 'submitted', 'unknown'}
                or (state.get('remote_task_id') and state.get('status') not in {'failed', 'completed'})):
            raise ValueError('metered_submission_in_progress')
        if state.get('status') in {'failed', 'completed'}:
            # Re-authorizing a finished task retires its receipt into history.
            state = {'history': (state.get('history',[])+
                     [{k:state.get(k) for k in ('backend','remote_task_id','status','started_at')}])[-20:]}
        for stale in ('ok', 'status', 'reason', 'denied_at', 'metered_requests'):
            state.pop(stale, None)  # A grant receipt awaits its fetch; stale outcomes mislead audits.
        seconds = float(seconds)
        if not math.isfinite(seconds):
            raise ValueError('metered_authorization_invalid')
        now = time.time()
        grant = {'platform': wanted['platform'], 'id': wanted['id'], 'url': wanted['url'],
                 'scope': 'single_metered_submission', 'authorized_at': now,
                 'expires_at': now+max(1,seconds), 'consumed': False}
        state.update(url=wanted['url'], authorization=grant)
        atomic_json(state_path, state)
        return {'ok': True, 'state': str(state_path), 'authorization': grant}


def _authorization_denial(grant, wanted, now):
    """A grant is positive only when scoped, unconsumed, in-window, and bound to this source."""
    if not isinstance(grant, dict):
        return 'metered_authorization_absent'
    if grant.get('consumed'):
        return 'metered_authorization_consumed'
    if grant.get('scope') != 'single_metered_submission':
        return 'metered_authorization_invalid'
    granted = identity(grant.get('url') or '')
    if (not granted or granted['platform'] != grant.get('platform') or granted['id'] != grant.get('id')
            or any(granted[k] != wanted[k] for k in ('platform','id'))):
        return 'metered_authorization_mismatch'
    try:
        authorized_at = float(grant['authorized_at'])
        expires_at = float(grant['expires_at'])
    except (KeyError, TypeError, ValueError):
        return 'metered_authorization_invalid'
    if not (math.isfinite(authorized_at) and math.isfinite(expires_at)):
        return 'metered_authorization_invalid'
    if not authorized_at <= now:
        return 'metered_authorization_invalid'
    if not now < expires_at:
        return 'metered_authorization_expired'
    return None


def _metered_fetch(url, state_path, deadline, jobs_root):
    import brightdata_ig_fallback as bright
    import apify_ig_fallback as apify
    bright.JOBS = apify.JOBS = Path(jobs_root)
    wanted = identity(url)
    if not wanted or wanted['platform'] != 'instagram':
        return {'ok': False, 'reason': 'metered_source_not_supported'}
    state = read_json(state_path)
    old_identity = identity(state.get('url') or '')
    if state and (not old_identity or any(old_identity[k] != wanted[k] for k in ('platform','id'))):
        raise ValueError('provider_request_identity_mismatch')
    history = state.get('history',[])
    if state.get('status') in {'failed','completed'} and time.time()-state.get('started_at',time.time()) > 300:
        history = (history+[{k:state.get(k) for k in ('backend','remote_task_id','status','started_at')}])[-20:]
        grant = state.get('authorization')
        state = {'history': history}
        if isinstance(grant, dict):
            state['authorization'] = grant
    if state.get('status') in {'starting', 'unknown', 'failed', 'completed'}:
        return {**state, 'resumed': True}
    backend = state.get('backend') or ('brightdata' if bright.token() else 'apify')
    tok = bright.token() if backend == 'brightdata' else apify.token()
    if not tok:
        return {'ok': False, 'reason': 'provider_not_configured'}
    if not state.get('remote_task_id'):
        # A new submission needs a positive one-shot grant bound to this exact
        # source; the missing kill-switch is never an approval.
        denial = _authorization_denial(state.get('authorization'), wanted, time.time())
        if denial:
            state.update(ok=False, status='not_authorized', reason=denial, url=wanted['url'],
                         metered_requests=0, denied_at=time.time())
            atomic_json(state_path, state)
            return state
        state['authorization'].update(consumed=True, consumed_at=time.time())
        for stale in ('ok', 'reason', 'denied_at'):
            state.pop(stale, None)
        state.update(url=wanted['url'], backend=backend, status='starting', started_at=time.time(),
                     metered_requests=1, history=history)
        atomic_json(state_path, state)  # Consume the grant and mark starting before any POST;
                                        # a lost POST response must not create a second billable run.
        try:
            if backend == 'brightdata':
                query = urlencode({'dataset_id': bright.DATASET_ID, 'include_errors': 'true', 'format': 'json'})
                result = bright.http_json('POST', f'{bright.API_ROOT}/trigger?{query}', tok,
                                          {'input': [{'url': wanted['url']}]}, timeout=12)
                remote_id = result.get('snapshot_id')
            else:
                result = apify.api(tok, 'POST', '/acts/apify~instagram-reel-scraper/runs?waitForFinish=0',
                                   {'username': [wanted['url']], 'resultsLimit': 1, 'includeTranscript': True,
                                    'includeDownloadedVideo': False}, timeout=12)['data']
                remote_id = result.get('id')
        except Exception as exc:
            match=re.match(r'http_(\d+):',str(exc))
            code=getattr(exc,'code',None) or (int(match.group(1)) if match else None)
            rejected=code in {400,401,403,404,422,429}
            state.update(ok=False,status='failed' if rejected else 'unknown',
                         reason='provider_http_'+str(code) if code else type(exc).__name__,
                         metered_requests=0 if rejected else 1)
            atomic_json(state_path,state)
            return state
        state.update(remote_task_id=remote_id, status='submitted' if remote_id else 'unknown')
        atomic_json(state_path, state)
    remote_id = state.get('remote_task_id')
    if not remote_id:
        return state
    items = None
    while time.monotonic() < deadline - 2:
        if backend == 'brightdata':
            progress = bright.http_json('GET', f'{bright.API_ROOT}/progress/{remote_id}', tok, timeout=8)
            status = progress.get('status')
            if status == 'ready':
                items = bright.http_json('GET', f'{bright.API_ROOT}/snapshot/{remote_id}?format=json', tok, timeout=10)
        else:
            progress = apify.api(tok, 'GET', f'/actor-runs/{remote_id}', timeout=8)['data']
            status = progress.get('status')
            if status == 'SUCCEEDED':
                items = apify.dataset_items(tok, progress)
        if status in {'failed', 'FAILED', 'ABORTED', 'TIMED-OUT'}:
            state.update(ok=False,status='failed',reason='provider_task_failed')
            atomic_json(state_path,state)
            return state
        if items is not None:
            break
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    if items is None:
        return state  # The task ID is durable; a later turn polls it without a new POST.
    items = items if isinstance(items, list) else items.get('data', [])
    hit = next((x for x in items if _same_record(x, wanted)), None)
    if not hit:
        state.update(ok=False, status='failed', reason='source_identity_mismatch')
    else:
        job = (bright.write_job(url, wanted['id'], hit, 'datasets-async', 'instagram') if backend == 'brightdata'
               else apify.write_job(url, wanted['id'], hit))
        state.update(ok=True, status='completed', job=str(job))
    atomic_json(state_path, state)
    return state


def _same_record(item, wanted):
    if item.get('error') or item.get('success') is False:
        return False
    actual = identity(item.get('url') or item.get('post_url') or '')
    code = item.get('shortcode') or item.get('shortCode') or item.get('postCode')
    return bool((actual and all(actual[k] == wanted[k] for k in ('platform', 'id')))
                or (code and str(code) == wanted['id']))


def _request_state_path(url, state_arg, jobs_root):
    if state_arg:
        return Path(state_arg)
    wanted = identity(url or '')
    if not wanted:
        raise ValueError('unsupported_video_url')
    return Path(jobs_root)/'.ninax-recovery'/(digest([wanted['platform'],wanted['id']])+'.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--search')
    parser.add_argument('--resolve')
    parser.add_argument('--fetch')
    parser.add_argument('--authorize')
    parser.add_argument('--authorization-seconds', type=float, default=600)
    parser.add_argument('--refresh')
    parser.add_argument('--visual-match',action='store_true')
    parser.add_argument('--job')
    parser.add_argument('--state')
    parser.add_argument('--jobs-root', default=str(JOBS))
    parser.add_argument('--seconds', type=float, default=40)
    args = parser.parse_args()
    try:
        if args.visual_match:
            result=visual_match_worker(json.load(sys.stdin),time.monotonic()+args.seconds)
        elif args.resolve:
            result = {'url':resolve_video_url(args.resolve)}
        elif args.search:
            result = search_worker(args.search)
        elif args.refresh:
            result = {'ok': refresh_metadata(args.refresh, Path(args.job), time.monotonic()+args.seconds)}
        elif args.authorize:
            result = authorize_metered_fetch(args.authorize,
                _request_state_path(args.authorize, args.state, args.jobs_root),
                time.monotonic()+min(15,args.seconds), args.authorization_seconds)
        else:
            result = metered_fetch(args.fetch,
                _request_state_path(args.fetch, args.state, args.jobs_root),
                time.monotonic()+args.seconds, args.jobs_root)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'reason': type(exc).__name__}))
        raise SystemExit(1)
