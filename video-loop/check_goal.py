"""Offline invariants for bounded evidence repair, resumable review and truthful metrics.

Run on Linux: python3 video-loop/check_goal.py. No credentials, network or test framework.
"""
import copy
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent/'work/profile/hooks'))
import video_evidence as ev
import video_review as review
import video_recovery as recovery
import enrich_cached_video as enrich
import video_workflow as workflow


def text_payload(text):
    return [{'type':'text','text':text}]


def valid_audit(doc,requirements,messages,*args,**kwargs):
    return {'status':'pass','limitations_accurate':True,'unsupported':[],'omitted_evidence':[],
            'contradictions':[],'provenance_errors':[],
            'coverage':[{'id':item['id'],'status':'covered','summary_quote':item['description'],
                         'evidence_ids':item['evidence_ids']} for item in requirements['must_cover']]}


def check_recovery():
    url='https://www.instagram.com/reel/GoalCheck'
    doc={'source':ev.identity(url),'source_aliases':[url],'duration':30,'items':[
        {'id':'E1','kind':'speech','start':4,'end':7,'text':'等三十秒','source_url':url},
        {'id':'E2','kind':'visual_observation','start':8,'end':8,'text':'設定畫面','source_url':url},
        {'id':'E3','kind':'caption','start':None,'end':None,'text':'貼文說明','source_url':url}]}
    request=review.recovery_requests({'source_checks':[{'kind':'speech','evidence_ids':['E1'],'reason':'數字不清'}]},doc)
    assert request[0]['start']==3 and request[0]['end']==8
    attempted=copy.deepcopy(doc)
    attempted['processed_ranges']={'semantic_recovery':[{'kind':'speech','start':2,'end':9,'status':'completed'}]}
    crossed=review.recovery_requests({'source_checks':[{'kind':'speech','evidence_ids':['E1']}]},attempted)
    assert crossed[0]['kind']=='visual','repeating the same ASR cannot resolve a homophone'
    attempted['processed_ranges']['semantic_recovery'].append({'kind':'visual','start':2,'end':9,'status':'completed'})
    unresolved={'source_checks':[{'kind':'speech','evidence_ids':['E1'],'reason':'原字仍不清'}]}
    assert not review.recovery_requests(unresolved,attempted) and unresolved['unresolved']
    mixed=review.recovery_requests({'source_checks':[{'kind':'speech','evidence_ids':['E1','E2','E3']}]},doc)
    assert mixed[0]['start']==3 and mixed[0]['end']==9
    for bad in ([{'kind':'speech','evidence_ids':['E999']}],[{'kind':'speech','evidence_ids':['E3']}],
                [{'kind':'shell','evidence_ids':['E1']}]):
        try:
            review.recovery_requests({'source_checks':bad},doc)
            raise AssertionError('unbound or wrong-kind evidence must be rejected')
        except ValueError:
            pass
    with tempfile.TemporaryDirectory(prefix='ninax-goal-') as directory:
        job=Path(directory)
        speech={'segments':[{'start':0,'end':5,'text':'前半句'}, {'start':5,'end':9,'text':'原辨識'},
                            {'start':10,'end':15,'text':'保留後段'}]}
        calls=[]
        def transcribe(*args,**kwargs):
            body=json.loads(kwargs['input']);calls.append(body)
            return subprocess.CompletedProcess([],0,json.dumps({'segments':[{'start':0,'end':9,'text':'完整修正句'}]}),'')
        with patch.object(enrich,'provider_env',return_value={'GROQ_API_KEY':'isolated'}),patch.object(enrich,'run',side_effect=transcribe):
            assert enrich.repair_speech(job,job/'media.mp4',{'sha256':'fake','audio_duration':30},speech,time.monotonic()+30,request[0])
            assert calls[0]['start']==0 and calls[0]['end']==9
            assert speech['segments'][-1]['text']=='保留後段'
            enrich.repair_speech(job,job/'media.mp4',{'sha256':'fake','audio_duration':30},speech,time.monotonic()+30,request[0])
            assert len(calls)==1,'same bounded range must reuse the durable ASR receipt'
        gap_job=job/'gap';gap_job.mkdir()
        original={'segments':[{'start':0,'end':3,'text':'舊前段'},{'start':4,'end':6,'text':'不能遺失的中段'},
                              {'start':7,'end':9,'text':'舊後段'}]}
        response=subprocess.CompletedProcess([],0,json.dumps({'segments':[{'start':0,'end':3,'text':'新前段'},
                                              {'start':7,'end':9,'text':'新後段'}]}),'')
        with patch.object(enrich,'provider_env',return_value={'GROQ_API_KEY':'isolated'}),patch.object(enrich,'run',return_value=response):
            assert enrich.repair_speech(gap_job,gap_job/'media.mp4',{'sha256':'gap','audio_duration':12},original,
                                        time.monotonic()+30,{'start':0,'end':10})
        assert [s['text'] for s in original['segments']]==['新前段','不能遺失的中段','新後段']
        ids_before=ev.stable_items(job,doc['items'])
        inserted=ev.stable_items(job,[{'id':'temporary','kind':'speech','start':1,'end':2,'text':'補回的內容','source_url':url},*doc['items']])
        assert [x['id'] for x in inserted[1:]]==[x['id'] for x in ids_before]
        ev.JOBS=workflow.cascade.JOBS=job
        (job/'video').mkdir()
        source={**doc,'evidence_revision':'before','evidence_status':'ready','gaps':[],
                'recovery':{'new_evidence':False},'media_sha256':'fake','processed_ranges':{},'related_sources':[]}
        def repair(path,current,targets,deadline,ledger):
            assert targets==request
            return {**current,'evidence_revision':'after','recovery':{'new_evidence':True}}
        approved={'text':'等 30 秒，最後停止。','audit':{'status':'pass'},'must_cover':[{'id':'M1'}]}
        with patch.object(workflow.cascade,'cascade',return_value={'job':str(job/'video'),'evidence':source}),\
             patch.object(workflow.cascade,'repair_requested',side_effect=repair) as applied,\
             patch.object(workflow,'review_all',side_effect=[{'text':review.NOTICE,'audit':{'status':'blocked'},'must_cover':[],
                 'source_checks':request},approved]) as checked,patch.object(workflow,'payload',side_effect=text_payload):
            result=workflow.execute({'url':url,'question':'完整摘要','turn_id':'fixture','profile':str(job),
                                     'session_id':'isolated','session_key':'isolated'})
        assert applied.call_count==1 and checked.call_count==2
        assert checked.call_args_list[1].args[0]['evidence_revision']=='after'
        assert result['approval']['kind']=='summary' and result['evidence_revision']=='after'
        report=workflow.quality_report(job)
        assert report['turns']==1 and report['audit_pass_rate']==1 and report['cost'] is None
    rng=random.Random(194)
    source=[{'timestamp':stamp,'hash':''.join(rng.choice('01') for _ in range(64))} for stamp in (0,3,6,9,12)]
    target=[{**frame,'timestamp':frame['timestamp']+20} for frame in source]
    match=recovery.align_visuals(source,target,14)
    assert match and match['creator_verified'] is False and match['offset_seconds']==20
    assert recovery.align_visuals(source,[{**frame,'timestamp':100-frame['timestamp']} for frame in target],14) is None
    assert recovery.align_visuals([{'timestamp':i,'hash':'1'*64} for i in range(5)],target,14) is None
    repeated=target+[{**target[0],'timestamp':70}]
    assert recovery.align_visuals(source[:4],repeated,14) is None


def check_long_review():
    url='https://www.youtube.com/watch?v=GoalCheckLong'
    doc={'source':ev.identity(url),'duration':660,'evidence_status':'ready','gaps':[],
         'source_aliases':[url],'items':[{'id':f'E{i+1}','kind':'speech','text':f'第 {i+1} 個必要步驟，等候 {i+1} 秒。',
         'start':i*60,'end':i*60+20,'source_url':url} for i in range(11)]}
    sections=review.evidence_sections(doc)
    assert [x['id'] for section in sections for x in section['items']]==[x['id'] for x in doc['items']]
    calls=[];clock=[0]
    def section_review(section,*args):
        calls.append([x['id'] for x in section['items']]);clock[0]+=60
        requirements=[{'id':f'M{i}','description':x['text'],'evidence_ids':[x['id']]} for i,x in enumerate(section['items']) if x['id']!='E2']
        return {'text':'已核對。\n\n'+'\n'.join(x['text'] for x in section['items'])+'\n\n來源：'+url,
                'audit':{'status':'pass'},'must_cover':requirements}
    def final_audit(proof,*args,**kwargs):
        assert {x['id'] for x in proof['items']}=={x['id'] for x in doc['items']},'revision facts outside the original checklist must reach the final audit'
        return valid_audit(proof,*args,**kwargs)
    with tempfile.TemporaryDirectory(prefix='ninax-chunks-') as directory,\
         patch.object(review,'payload',side_effect=text_payload),patch.object(review,'review',side_effect=section_review),\
         patch.object(review,'audit_candidate',side_effect=final_audit),patch.object(review.time,'monotonic',side_effect=lambda:clock[0]):
        pending=review.review_all(doc,'摘要',150,[],Path(directory))
        assert pending['progress']['completed']==0 and not calls,'do not start a source audit with an unusable remaining budget'
        pending=review.review_all(doc,'摘要',200,[],Path(directory))
        assert pending['progress']['completed']==1 and pending['audit']['status']=='blocked'
        clock[0]=0
        with patch.object(review,'audit_candidate',side_effect=TimeoutError('model deadline')):
            pending=review.review_all(doc,'摘要',900,[],Path(directory))
        assert pending['progress']['completed']==len(sections) and pending['progress']['integration_pending']
        assert '最後整合檢核尚未完成' in review.status_notice(doc['source'],progress=pending['progress'])
        result=review.review_all(doc,'摘要',900,[],Path(directory))
        assert len(calls)==len(sections),'already audited sections must not call the model again'
        assert result['audit']['status']=='pass' and doc['items'][-1]['text'] in result['text']
        assert len(result['must_cover'])==len(doc['items'])-1
        bookkeeping={**doc,'processed_ranges':{'semantic_recovery':[{'kind':'visual','start':650,'end':655,'status':'completed'}]}}
        assert review.review_all(bookkeeping,'摘要',900,[],Path(directory))['audit']['status']=='pass'
        assert len(calls)==len(sections),'a different section repair must not invalidate unchanged source facts'
        def revise_then_pass(proof,requirements,messages,*args,**kwargs):
            audit=final_audit(proof,requirements,messages)
            if '已補查：' not in messages[0]['text']:
                audit.update(status='revise',contradictions=['需要標示第二個步驟的補查結果'])
            return audit
        with patch.object(review,'audit_candidate',side_effect=revise_then_pass),\
             patch.object(review,'ask',return_value={'replacements':[{'old':doc['items'][1]['text'],'new':'已補查：'+doc['items'][1]['text']}]}) as rewrite:
            revised=review.review_all(doc,'修訂測試',2000,[],Path(directory))
            assert revised['audit']['status']=='pass' and len(revised['audit']['checks'])==2 and rewrite.call_count==1
            assert '已補查：' in revised['text']
            assert review.review_all(doc,'修訂測試',2000,[],Path(directory))['audit']['status']=='pass'
            assert rewrite.call_count==1,'resume must reuse the saved revision and its real final audit'
        # A native message cap must block delivery, never silently discard the final chapter.
        def capped(text):
            return text_payload(text[:80] if text.startswith('以下依分段') else text)
        with patch.object(review,'payload',side_effect=capped):
            clipped=review.review_all(doc,'摘要',900,[],Path(directory))
        assert clipped['audit']['reason']=='line_payload_limit'
    print(json.dumps({'status':'PASS','checks':['target_binding','whole_segment_repair','no_duplicate_asr',
          'repair_then_reaudit','visual_order_without_creator_claim','durable_sections','tail_preserved',
          'line_cap_fails_closed','metrics_do_not_invent_cost']}))


if __name__=='__main__':
    check_recovery()
    check_long_review()
