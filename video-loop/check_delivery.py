"""Exercise the real profile adapter's egress decisions with an in-memory transport."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

BASE=Path(__file__).parent
HERMES='/home/box/.hermes/hermes-agent'


async def check():
    with tempfile.TemporaryDirectory(prefix='ninax-egress-') as directory:
        home=Path(directory)
        os.environ['HERMES_HOME']=str(home)
        shutil.copytree(BASE/'work/profile/hooks',home/'hooks')
        sys.path.insert(0,HERMES)
        spec=importlib.util.spec_from_file_location('ninax_delivery_plugin',BASE/'work/profile/plugins/line-platform/__init__.py')
        plugin=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=plugin
        spec.loader.exec_module(plugin)
        from gateway.config import PlatformConfig
        adapter=plugin.VideoLineAdapter(PlatformConfig(enabled=True))
        class Client:
            def __init__(self):self.calls=[];self.ambiguous=False
            async def reply(self,token,messages):
                self.calls.append(('reply',token,messages))
                if self.ambiguous:raise TimeoutError('accepted but acknowledgement lost')
            async def push(self,chat,messages):self.calls.append(('push',chat,messages))
        client=Client();adapter._client=client
        inputs={}
        def state(turn):
            if turn not in inputs:
                decision=adapter._input_lifecycle.accept(
                    {'type':'message','webhookEventId':'event-'+turn,'timestamp':1000+len(inputs),
                     'deliveryContext':{'isRedelivery':False},
                     'message':{'id':turn,'type':'text','text':'input-'+turn}},'U_test')
                assert decision['accepted'] and adapter._input_lifecycle.begin_job(decision)
                inputs[turn]=decision
            input_binding=inputs[turn]
            binding={'profile':str(home),'session_id':'session','session_key':'line:test','turn_id':turn,
                     **{k:input_binding[k] for k in ('input_id','input_revision','input_sha256')}}
            return {'chat_id':'U_test','message_id':turn,'binding':binding,'input_binding':input_binding,
                    'delivery':'pending','reply_token':'token-'+turn,'reply_expires':time.time()+30,
                    'approval':{'binding':binding,'kind':'notice',
                                'payload_sha256':plugin.digest(plugin.native._text_messages(plugin.NOTICE))}}
        first=state('first')
        token=plugin._TURN.set(first)
        adapter._reply_tokens['U_test']=('a-newer-message-token',time.time()+30)
        try:
            assert not (await adapter.send('U_test','An unreviewed draft')).success
            assert not (await adapter.send('different-chat',plugin.NOTICE)).success
            assert (await adapter.send('U_test',plugin.NOTICE)).success
            assert client.calls[0][1]=='token-first','must not borrow a later turn reply token'
            assert adapter._reply_tokens['U_test'][0]=='a-newer-message-token'
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
        finally:plugin._TURN.reset(token)
        partial=state('partial')
        partial['result']={'source':{'url':'https://instagram.com/reel/ValidVideo'},'gaps':['full_media_missing']}
        notice=plugin.status_notice(**partial['result'])
        partial['approval']['payload_sha256']=plugin.digest(plugin.native._text_messages(notice))
        token=plugin._TURN.set(partial)
        try:
            assert '未取得完整影音' in notice and (await adapter.send('U_test',notice)).success
        finally:plugin._TURN.reset(token)
        replay=state('first');token=plugin._TURN.set(replay)
        try:assert not (await adapter.send('U_test',plugin.NOTICE)).success
        finally:plugin._TURN.reset(token)
        lost=state('lost');token=plugin._TURN.set(lost);client.ambiguous=True
        try:
            before=len(client.calls)
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(client.calls)==before+1 and client.calls[-1][0]=='reply'
            assert lost['delivery']=='unknown'
        finally:plugin._TURN.reset(token);client.ambiguous=False
        stale=state('edited')
        newer=adapter._input_lifecycle.accept(
            {'type':'messageEdited','webhookEventId':'event-edited-new','timestamp':2000,
             'deliveryContext':{'isRedelivery':False},
             'message':{'id':'edited','type':'text','text':'corrected'}},'U_test')
        assert newer['accepted'] and adapter._input_lifecycle.begin_job(newer)
        token=plugin._TURN.set(stale)
        try:
            before=len(client.calls)
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(client.calls)==before,'superseded input must never reach LINE transport'
        finally:plugin._TURN.reset(token)
        stale_rid=adapter._cache.register_pending('U_test')
        adapter._cache.set_ready(stale_rid,plugin.NOTICE)
        adapter._pending_buttons['U_test']=stale_rid
        adapter._reviewed_cache[stale_rid]=('U_test',plugin.digest(plugin.native._text_messages(plugin.NOTICE)),
                                           stale['input_binding'])
        before=len(client.calls)
        await adapter._handle_postback_event({'replyToken':'stale-postback',
            'source':{'type':'user','userId':'U_test'},
            'postback':{'data':json.dumps({'action':'show_response','request_id':stale_rid})}})
        assert len(client.calls)==before,'superseded postback cache must not reach LINE transport'
        expired=state('expired');expired['deadline']=time.monotonic()-1;token=plugin._TURN.set(expired)
        try:
            before=len(client.calls)
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(client.calls)==before,'deadline must stop transport before making a request'
        finally:plugin._TURN.reset(token)
        rid=adapter._cache.register_pending('U_test')
        adapter._cache.set_ready(rid,'poisoned unreviewed cache')
        before=len(client.calls)
        await adapter._handle_postback_event({'replyToken':'postback','source':{'type':'user','userId':'U_test'},
             'postback':{'data':json.dumps({'action':'show_response','request_id':rid})}})
        assert len(client.calls)==before
        adapter._active_videos[('U_test','pending')]=state('pending')
        assert not (await adapter.send('U_test','unbound interim draft')).success
        assert (await adapter.send('U_test','⏳ Queued: the next request')).success
        adapter.allow_all=True
        dispatched=[]
        async def capture_input(event):
            binding=event['_ninax_input']
            assert adapter._input_lifecycle.begin_job(binding)
            dispatched.append(binding)
        adapter._handle_message_event=capture_input
        raw={'type':'message','webhookEventId':'dispatch-original','timestamp':5000,
             'deliveryContext':{'isRedelivery':False},'source':{'type':'user','userId':'U_dispatch'},
             'message':{'id':'dispatch-message','type':'text','text':'before edit'}}
        await adapter._dispatch_event(raw)
        await adapter._dispatch_event({**raw,'deliveryContext':{'isRedelivery':True}})
        await adapter._dispatch_event({**raw,'type':'messageEdited','webhookEventId':'dispatch-edit',
            'timestamp':6000,'replyToken':'edited-token',
            'message':{**raw['message'],'text':'after edit'}})
        await adapter._dispatch_event({**raw,'webhookEventId':'dispatch-stale','timestamp':5500,
            'deliveryContext':{'isRedelivery':True},'message':{**raw['message'],'text':'late old value'}})
        assert len(dispatched)==2 and [x['input_revision'] for x in dispatched]==[1,2]
        result={'gate':'line-delivery-boundaries','status':'PASS','real_line_message_sent':False,
                'checks':['no_preview','correct_chat_and_turn','owned_reply_token','durable_no_duplicate',
                          'no_retry_on_ambiguous_acceptance','stale_revision_blocked','stale_postback_blocked',
                          'postback_cannot_bypass','control_ack_preserved','edited_event_routed',
                          'redelivery_and_out_of_order_dropped']}
        (BASE/'delivery-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))


asyncio.run(check())
