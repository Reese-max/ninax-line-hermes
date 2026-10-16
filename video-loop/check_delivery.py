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
HERMES=sys.argv[1] if len(sys.argv)>1 else '/home/box/.hermes/hermes-agent'


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
        assert adapter._video_request('繼續摘要','pending-long')['url'] is None
        plugin.atomic_json(adapter._session_path('pending-long'),{'url':'https://www.youtube.com/watch?v=LongFixture',
                           'review_question':'整理操作順序與片尾','last_was_video':True})
        assert adapter._video_request('繼續摘要','pending-long')['review_question']=='整理操作順序與片尾'
        class Client:
            def __init__(self):self.calls=[];self.ambiguous=False
            async def reply(self,token,messages):
                self.calls.append(('reply',token,messages))
                if self.ambiguous:raise TimeoutError('accepted but acknowledgement lost')
            async def push(self,chat,messages,*,retry_key=None):self.calls.append(('push',chat,messages))
        client=Client();adapter._client=client
        counter=[0]
        def state(turn):
            # Every revision-bound turn starts from a real accepted input receipt.
            counter[0]+=1
            nonce=turn+'-'+str(counter[0])
            event={'type':'message','webhookEventId':'evt-'+nonce,'timestamp':1000+counter[0],
                   'message':{'id':'msg-'+nonce,'type':'text','text':'request '+nonce}}
            input_binding=adapter._input_lifecycle.accept(event,'U_test')
            assert input_binding['accepted'] and adapter._input_lifecycle.begin_job(input_binding)
            binding={'profile':str(home),'session_id':'session','session_key':'line:test','turn_id':turn,
                     'input_id':input_binding['input_id'],'input_revision':input_binding['input_revision'],
                     'input_sha256':input_binding['input_sha256']}
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
        result={'gate':'line-delivery-boundaries','status':'PASS','real_line_message_sent':False,
                'checks':['no_preview','correct_chat_and_turn','owned_reply_token','durable_no_duplicate',
                          'no_retry_on_ambiguous_acceptance','postback_cannot_bypass','control_ack_preserved']}
        (BASE/'delivery-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))


asyncio.run(check())
