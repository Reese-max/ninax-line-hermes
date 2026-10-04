"""Run with the installed Hermes Python; no network or real LINE messages."""
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock,patch

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE/'work/profile/hooks'))
sys.path.insert(0, sys.argv[1] if len(sys.argv)>1 else '/home/box/.hermes/hermes-agent')
import video_evidence as ev
import video_takeaway_cascade as cascade
import video_review as review
import video_recovery as recovery
import enrich_cached_video as enrich


def check():
    with tempfile.TemporaryDirectory(prefix='ninax-check-') as directory:
        root = Path(directory)
        ev.JOBS = cascade.JOBS = root
        job = root/'wrong-folder-name'
        job.mkdir()
        url = 'https://www.instagram.com/reel/CaseSensitiveID/'
        ev.atomic_json(job/'source.info.json', {'webpage_url':url, 'id':'CaseSensitiveID','duration':78})
        ev.atomic_json(job/'apify.item.json', {'caption':'完整教學：設定一個參數之後還有兩個步驟。'})
        first = ev.snapshot(job,url)
        assert first['evidence_status'] == 'insufficient' and first['items']
        assert cascade.find_job(url) == job
        assert not ev.matches(job,'https://www.facebook.com/reel/CaseSensitiveID/')
        assert not ev.matches(job,'https://www.instagram.com/reel/casesensitiveid/')
        assert ev.identity('https://instagram.com.evil.example/reel/CaseSensitiveID') is None
        assert ev.identity('https://user:password@instagram.com/reel/CaseSensitiveID') is None
        (job/'cached-media.mp4').write_bytes(b'isolated-test-media')
        stat=(job/'cached-media.mp4').stat()
        ev.atomic_json(job/'media-proof.json', {'duration':78,'sha256':'abc','has_video':True,
                       'file_path':str(job/'cached-media.mp4'),'file_stamp':[stat.st_ino,stat.st_size,stat.st_mtime_ns],
                       'visual':{'policy':ev.POLICY,'sha256':'abc','complete':True,'decoded_until':78}})
        ev.atomic_json(job/'transcript.json', {'text':'第一步不是先開啟，需等候 30 秒。',
             'segments':[{'start':0,'end':10,'text':'第一步不是先開啟，需等候 30 秒。'}],
             'status':'transcribed','media_sha256':'abc','processed_ranges':[[0,78]],'analysis_policy':ev.AUDIO_POLICY,
             'quality_checked':True})
        ev.atomic_json(job/'visual.json', [{'timestamp':3,'visual_summary':'畫面顯示第二步',
                                          'visible_text':['上限 20%']}])
        complete = ev.snapshot(job,url)
        assert complete['evidence_status'] == 'ready'
        assert any(x['text']=='上限 20%' for x in complete['items'])
        ev.save_snapshot(job,complete)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda i:ev.save_turn(job,str(i),{'requested_step':i}),[1,2]))
        assert set(ev.read_json(job/'evidence.json')['turns']) == {'1','2'}
        old_enrich = cascade.enrich
        cascade.enrich = lambda *args: (_ for _ in ()).throw(AssertionError('ready cache must not call media/provider'))
        assert cascade.cascade(url)['ok']
        cascade.enrich = old_enrich
        expansions=[]
        def expand(job,deadline,ledger,count):
            expansions.append(count)
            proof=ev.read_json(job/'media-proof.json')
            if count==16:
                proof['visual']['limit']=count
                frames=ev.read_json(job/'visual.json')
                ev.atomic_json(job/'visual.json',frames+[{'timestamp':5,'visual_summary':'新增畫面資訊'}])
            ev.atomic_json(job/'media-proof.json',proof)
            return {'ok':count==16}
        with patch.object(cascade,'enrich',side_effect=expand):
            forced=cascade.cascade(url,force=True)
            assert forced['evidence']['recovery']['requested_refresh'] and forced['evidence']['recovery']['new_evidence']
            for i in range(3):
                unchanged=cascade.cascade(url,force=True)
                assert not unchanged['evidence']['recovery']['new_evidence'],'rewritten files are not new content'
        assert expansions==[16,32,40],'each sampling option must run at most once'
        assert unchanged['evidence']['recovery']['attempts'][-1]['reason']=='sampling_options_exhausted'
        (job/'cached-media.mp4').write_bytes(b'changed-media-with-stale-proof')
        assert 'full_media_missing' in ev.snapshot(job,url)['gaps']
        original_run=recovery.run
        refreshed='https://valid.cdninstagram.com/new.mp4'
        ev.atomic_json(job/'apify.item.json',{'videoUrl':'https://expired.cdninstagram.com/old.mp4'})
        recovery.run=lambda *a,**kw:subprocess.CompletedProcess([],0,json.dumps({'webpage_url':url,'duration':78,
            'formats':[{'url':refreshed,'ext':'mp4','acodec':'aac','vcodec':'h264'}]}),'')
        assert recovery.refresh_metadata(url,job,time.monotonic()+10)
        assert ev.read_json(job/'apify.item.json').get('videoUrl') is None
        assert ev.read_json(job/'apify.item.json')['video_url']==refreshed
        recovery.run=original_run
        original_download=enrich.download_media
        calls=[]
        enrich.download_media=lambda *a:calls.append(a[0])
        enrich.fetch_media(refreshed,job/'candidate.mp4',time.monotonic()+10)
        try:
            enrich.fetch_media(refreshed,job/'candidate.mp4',time.monotonic()+10)
            raise AssertionError('same URL was fetched twice')
        except ValueError as exc:
            assert str(exc)=='same_media_url_already_attempted'
        assert len(calls)==1
        assert enrich.weak_speech({'text':'欸'*100})
        assert not enrich.weak_speech({'text':'先拔掉電源，等候三十秒再繼續。','avg_logprob':-0.2})
        enrich.download_media=original_download
        damaged=root/'damaged-media';damaged.mkdir()
        replacement=root/'complete.mp4'
        for target,seconds in ((damaged/'cached-media.mp4',1),(replacement,4)):
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=size=32x32:rate=2',
                            '-t',str(seconds),'-c:v','libx264',str(target)],check=True)
        ev.atomic_json(damaged/'source.info.json',{'webpage_url':url,'duration':4})
        ev.atomic_json(damaged/'apify.item.json',{'video_url':refreshed})
        original_visual=enrich.enrich_visuals
        enrich.download_media=lambda u,target,deadline:shutil.copy2(replacement,target)
        enrich.enrich_visuals=lambda *a:None
        assert enrich.enrich(damaged,time.monotonic()+15)['ok']
        assert ev.read_json(damaged/'media-proof.json')['duration']==4
        assert list(damaged.glob('retained-media-*.mp4')),'partial original must be retained'
        enrich.download_media=original_download;enrich.enrich_visuals=original_visual
        ev.atomic_json(job/'visual.json',[{'timestamp':100,'visual_summary':'無效的片外時間'}])
        assert ev.snapshot(job,url)['evidence_status'] != 'ready'
        requirements={'must_cover':[{'id':'M1','evidence_ids':['E1']},{'id':'M2','evidence_ids':['E2']}]}
        messages=[{'type':'text','text':'不是先開啟，等候 30 秒。上限 20%。'}]
        valid={'status':'pass','limitations_accurate':True,'unsupported':[],'omitted_evidence':[],
               'contradictions':[],'provenance_errors':[],'coverage':[
               {'id':'M1','status':'covered','summary_quote':'等候 30 秒','evidence_ids':['E1']},
               {'id':'M2','status':'covered','summary_quote':'上限 20%','evidence_ids':['E2']}]}
        source={'items':[{'id':'E1'},{'id':'E2'}]}
        ending_source={'items':[{'id':'E1','kind':'speech','start':1},
                                {'id':'E2','kind':'visual_observation','start':9}]}
        ending_requirements={'must_cover':[{'id':'M1','description':'保留語音結論','evidence_ids':['E1']}],'unresolved':[]}
        with patch.object(review,'ask',return_value=ending_requirements):
            try:
                review.checklist(ending_source,'摘要',time.monotonic()+30,[])
                raise AssertionError('visual ending must be included in the source checklist')
            except ValueError as exc:
                assert str(exc)=='checklist_missing_visual_ending'
            ending_requirements['must_cover'][0]['evidence_ids'].append('E2')
            assert review.checklist(ending_source,'摘要',time.monotonic()+30,[])==ending_requirements
        assert review.audit_ok(valid,requirements,messages,source)
        assert not review.audit_ok({**valid,'coverage':valid['coverage'][:1]},requirements,messages,source)
        assert not review.audit_ok({**valid,'unsupported':['invented']},requirements,messages,source)
        assert not review.audit_ok(valid,requirements,[{'text':'先開啟，等 3 秒，上限 200%。'}],source)
        quoted={'coverage':[{'summary_quote':'先拔掉電源並等候……綠燈亮起後重新接電。'}]}
        actual=[{'text':'先拔掉電源並等候三十秒，待綠燈亮起後重新接電。'}]
        review.bind_quotes(quoted,actual)
        assert quoted['coverage'][0]['summary_quote']==actual[0]['text']
        invalid={'coverage':[{'summary_quote':'先拔掉電源並等候……紅燈亮起後重新接電。'}]}
        review.bind_quotes(invalid,actual)
        assert 'summary_quote_original' not in invalid['coverage'][0]
        reply=MagicMock();reply.choices[0].finish_reason='stop'
        plain='畫面寫著 "設定完成"。\n然後停止操作。'
        reply.choices[0].message.content=plain
        with patch('agent.auxiliary_client.call_llm',return_value=reply):
            try:
                review.call_worker({'stage':'audit','messages':[],'timeout':5})
                raise AssertionError('invalid audit JSON must fail closed')
            except json.JSONDecodeError:pass
        original='前段在餐廳。對話出現尷尬反應。片尾大笑。'
        edited=review.apply_revisions(original,{'replacements':[{'old':'對話出現尷尬反應。','new':'對話出現尷尬反應，並說「完了」。'}]})
        assert edited.startswith('前段在餐廳。') and edited.endswith('片尾大笑。')
        try:
            review.apply_revisions(original,{'replacements':[{'old':'不存在的文字','new':'其他內容'}]})
            raise AssertionError('revision must match an actual original span')
        except ValueError:pass
        render_requirements={'must_cover':[{'id':'M1','description':'不是先開啟，等候 30 秒。','evidence_ids':['E1']},
            {'id':'M2','description':'上限 20%。','evidence_ids':['E2']}],'unresolved':[]}
        render_source={'source':ev.identity(url),'evidence_status':'ready','gaps':[],'items':[
            {'id':'E1','kind':'speech','start':0},{'id':'E2','kind':'visible_text','start':24}]}
        ledger=[]
        with patch.object(review,'checklist',return_value=render_requirements),patch.object(review,'audit_candidate',return_value=valid),\
             patch.object(review,'ask',side_effect=AssertionError('must not expand a source checklist through another model call')):
            rendered=review.review(render_source,'摘要',time.monotonic()+30,ledger)
        assert rendered['audit']['status']=='pass' and ledger[0]['method']=='source_checklist_render'
        anchor1='Before changing the setting disconnect the power cable and wait thirty seconds.'
        anchor2='Only after the green indicator appears should you reconnect the cable and restart.'
        clip={'author':'same_creator','items':[{'kind':'speech','text':anchor1,'start':1},
                                            {'kind':'speech','text':anchor2,'start':12}]}
        original={'segments':[{'text':anchor1[:35],'start':21,'end':22},
                              {'text':anchor1[35:],'start':22,'end':26},
                              {'text':anchor2,'start':32,'end':38}]}
        assert recovery.align_transcripts(clip,{'uploader_id':'same_creator'},original)
        assert recovery.align_transcripts(clip,{'uploader_id':'different_creator'},original) is None
        unrelated={'segments':[{'text':anchor1,'start':21,'end':26},
                               {'text':anchor2,'start':100,'end':106}]}
        assert recovery.align_transcripts(clip,{'uploader_id':'same_creator'},unrelated) is None
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'result':{'content':[{'type':'text','text':
            '### 1. Same clip\n- **URL**: '+url+'\n- Public source'}]}}).encode()
        with patch('urllib.request.urlopen',return_value=response):
            searched=recovery.search_worker('public source')
        assert searched['backend']=='anysearch' and searched['candidates'][0]['url']==url
        pending=root/'provider-request.json'
        ev.atomic_json(pending,{'url':ev.identity(url)['url'],'backend':'brightdata','status':'pending',
                               'remote_task_id':'s_test','metered_requests':1})
        import brightdata_ig_fallback as bright
        bright.token=lambda:'fake-not-a-real-token'
        bright.http_json=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('no time left; must not call provider'))
        resumed=recovery._metered_fetch(url,pending,time.monotonic()-1,root)
        assert resumed['remote_task_id']=='s_test' and resumed['metered_requests']==1
        posts=[]
        def stub(method,endpoint,*a,**kw):
            if method=='POST':
                posts.append(endpoint)
            return {'snapshot_id':'s_new'} if method=='POST' else {'status':'running'}
        bright.http_json=stub
        # Only fake Bright Data transport is reachable in this scoped grant fixture.
        # Keep the real kill-switch test, while allowing positive-grant cases to run.
        with patch.dict(recovery.os.environ, {'NINAX_DISABLE_METERED_FETCH': '0'}):
            for name,expected in (('denied-absent','metered_authorization_absent'),
                                  ('denied-mismatch','metered_authorization_mismatch'),
                                  ('denied-expired','metered_authorization_expired'),
                                  ('denied-consumed','metered_authorization_consumed')):
                receipt=root/(name+'.json')
                if expected!='metered_authorization_absent':
                    recovery.authorize_metered_fetch(url,receipt,time.monotonic()+5)
                    doc=ev.read_json(receipt)
                    if expected=='metered_authorization_mismatch':
                        doc['authorization'].update(id='OtherReel99',url='https://www.instagram.com/reel/OtherReel99/')
                    elif expected=='metered_authorization_expired':
                        doc['authorization']['expires_at']=time.time()-1
                    else:
                        doc['authorization']['consumed']=True
                    ev.atomic_json(receipt,doc)
                denied=recovery._metered_fetch(url,receipt,time.monotonic()+5,root)
                assert denied['status']=='not_authorized' and denied['reason']==expected,(name,denied)
                assert denied['metered_requests']==0 and not posts,name
            # A denied receipt is not wedged: a later valid grant still authorizes one submission.
            posts.clear()
            recovery.authorize_metered_fetch(url,root/'denied-absent.json',time.monotonic()+5)
            refetched=recovery._metered_fetch(url,root/'denied-absent.json',time.monotonic()+1,root)
            assert refetched['status']=='submitted' and refetched['metered_requests']==1 and len(posts)==1
            granted=root/'authorized-fetch.json'
            grant=recovery.authorize_metered_fetch(url,granted,time.monotonic()+5)
            assert grant['ok'] and grant['authorization']['id']=='CaseSensitiveID' and not grant['authorization']['consumed']
            fetched=recovery._metered_fetch(url,granted,time.monotonic()+1,root)
            assert fetched['status']=='submitted' and fetched['remote_task_id']=='s_new' and fetched['metered_requests']==1
            receipt=ev.read_json(granted)
            consumed=receipt['authorization']
            assert consumed['consumed'] and consumed['consumed_at']>=consumed['authorized_at']
            try:
                recovery.authorize_metered_fetch(url,granted,time.monotonic()+5)
                raise AssertionError('an in-flight submission must not accept a new grant')
            except ValueError as exc:
                assert str(exc)=='metered_submission_in_progress'
            posts.clear()
            again=recovery._metered_fetch(url,granted,time.monotonic()+1,root)
            assert again['status']=='submitted' and not posts,'a submitted task is polled, never resubmitted'
            def completing(method,endpoint,*a,**kw):
                if method=='POST':
                    posts.append(endpoint)
                    return {'snapshot_id':'s_done'}
                return {'status':'ready'} if '/progress/' in endpoint else [{'url':url,'shortcode':'CaseSensitiveID'}]
            bright.http_json=completing
            done_state=root/'completed-fetch.json'
            recovery.authorize_metered_fetch(url,done_state,time.monotonic()+5)
            done=recovery._metered_fetch(url,done_state,time.monotonic()+8,root)
            assert done['ok'] and done['status']=='completed' and done['metered_requests']==1 and len(posts)==1
            assert Path(done['job']).is_dir() and ev.matches(done['job'],url)
            posts.clear()
            finished=recovery._metered_fetch(url,done_state,time.monotonic()+5,root)
            assert finished['status']=='completed' and finished.get('resumed') and not posts
            bright.http_json=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('provider_timeout'))
            unknown=root/'unknown-fetch.json'
            recovery.authorize_metered_fetch(url,unknown,time.monotonic()+5)
            lost=recovery._metered_fetch(url,unknown,time.monotonic()+5,root)
            assert lost['status']=='unknown' and lost['metered_requests']==1
            assert ev.read_json(unknown)['authorization']['consumed']
            posts.clear()
            retried=recovery._metered_fetch(url,unknown,time.monotonic()+5,root)
            assert retried.get('resumed') and retried['status']=='unknown' and not posts
            bright.http_json=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('http_403:permission denied'))
            rejected_state=root/'rejected-request.json'
            recovery.authorize_metered_fetch(url,rejected_state,time.monotonic()+5)
            rejected=recovery._metered_fetch(url,rejected_state,time.monotonic()+5,root)
            assert rejected['status']=='failed' and rejected['metered_requests']==0
            with patch.dict(recovery.os.environ,{'NINAX_DISABLE_METERED_FETCH':'1'}):
                assert recovery.metered_fetch(url,granted,time.monotonic()+5,root)=={'ok':False,'reason':'metered_fetch_disabled'}
        import psutil
        child_file=root/'child.pid'
        script=('import subprocess,sys,time; from pathlib import Path; '
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True); '
                f'Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(30)')
        try:
            ev.run([sys.executable,'-c',script],time.monotonic()+1.5)
            raise AssertionError('stage did not time out')
        except TimeoutError:
            pass
        child_pid=int(child_file.read_text())
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status()==psutil.STATUS_ZOMBIE
    print(json.dumps({'gate':'video-evidence-and-budget','status':'PASS','checks':[
         'caption_is_not_complete','platform_and_case_identity','ready_cache_no_calls',
         'timestamp_validation','parallel_turn_ledgers','audit_missing_claims',
         'source_alignment','expired_url_replaced','changed_media_invalidates_proof','same_url_fetch_deduplicated',
         'resume_accepted_task_without_post','metered_fetch_requires_one_shot_authorization','nested_child_cleanup']}))


if __name__=='__main__':
    check()
