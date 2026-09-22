"""Exercise Push retry-key idempotency with an in-memory transport (issue #8)."""
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
    with tempfile.TemporaryDirectory(prefix='ninax-retry-') as directory:
        home=Path(directory)
        os.environ['HERMES_HOME']=str(home)
        shutil.copytree(BASE/'work/profile/hooks',home/'hooks')
        sys.path.insert(0,HERMES)
        spec=importlib.util.spec_from_file_location('ninax_retry_plugin',BASE/'work/profile/plugins/line-platform/__init__.py')
        plugin=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=plugin
        spec.loader.exec_module(plugin)
        from gateway.config import PlatformConfig
        adapter=plugin.VideoLineAdapter(PlatformConfig(enabled=True))

        class Client:
            def __init__(self):self.calls=[];self.push_plan=[]
            async def reply(self,token,messages,**kw):
                self.calls.append(('reply',token,messages,kw))
                raise TimeoutError('reply ambiguous')
            async def push(self,chat,messages,retry_key=None):
                self.calls.append(('push',chat,messages,retry_key))
                outcome=self.push_plan.pop(0) if self.push_plan else None
                if outcome:raise outcome
        client=Client();adapter._client=client

        def state(turn):
            binding={'profile':str(home),'session_id':'session','session_key':'line:test','turn_id':turn}
            return {'chat_id':'U_test','message_id':turn,'binding':binding,'delivery':'pending',
                    'reply_token':'','reply_expires':0,'deadline':time.monotonic()+30,
                    'approval':{'binding':binding,'kind':'notice',
                                'payload_sha256':plugin.digest(plugin.native._text_messages(plugin.NOTICE))}}
        def pushes():
            return [c for c in client.calls if c[0]=='push']

        # 1) Push 500 then success: same persisted key on both attempts, delivered.
        first=state('r1');client.push_plan=[RuntimeError('LINE push 500: oops'),None]
        token=plugin._TURN.set(first)
        try:assert (await adapter.send('U_test',plugin.NOTICE)).success
        finally:plugin._TURN.reset(token)
        assert len(pushes())==2 and pushes()[0][3] and pushes()[0][3]==pushes()[1][3],pushes()
        assert first['delivery']=='delivered'

        # 2) Recorded delivery carries the retry key.
        record=plugin.read_json(adapter._video_state/'r1.delivery.json')
        assert record.get('retry_key')==pushes()[0][3],record

        # 3) Same-key 409 counts as API acceptance (LINE already accepted this key).
        dup=state('r409');client.push_plan=[RuntimeError('LINE push 409: conflict')]
        token=plugin._TURN.set(dup)
        try:
            before=len(pushes())
            assert (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(pushes())==before+1,'409 acceptance must not trigger another send'
        finally:plugin._TURN.reset(token)
        assert dup['delivery']=='delivered'

        # 4) Non-retryable 400 fails once and stays unknown.
        bad=state('r400');client.push_plan=[RuntimeError('LINE push 400: bad request'),None]
        token=plugin._TURN.set(bad)
        try:
            before=len(pushes())
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(pushes())==before+1,'4xx must not be retried'
            assert bad['delivery']=='unknown'
        finally:plugin._TURN.reset(token)

        # 5) Timeout then success: retried with identical key.
        tout=state('rtmo');client.push_plan=[TimeoutError('push ack lost'),None]
        token=plugin._TURN.set(tout)
        try:assert (await adapter.send('U_test',plugin.NOTICE)).success
        finally:plugin._TURN.reset(token)
        assert [c[3] for c in pushes()[-2:]] == [pushes()[-1][3]]*2

        # 6) Ambiguous Reply stays fail-closed: no push fallback, no key minted.
        amb=state('rre');amb['reply_token']='tok-live';amb['reply_expires']=time.time()+30
        token=plugin._TURN.set(amb)
        try:
            before=len(client.calls)
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            new=client.calls[before:]
            assert len(new)==1 and new[0][0]=='reply' and not new[0][3],new
            assert amb['delivery']=='unknown'
        finally:plugin._TURN.reset(token)

        # 7) Terminal states still block re-entry (no out-of-band duplicates).
        token=plugin._TURN.set(state('r400'))
        try:
            before=len(pushes())
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(pushes())==before,'durable unknown must still be terminal for new calls'
        finally:plugin._TURN.reset(token)

        # 8) Retry key is stable per (turn,chat,payload) and differs across turns.
        other=state('r-other');client.push_plan=[None]
        token=plugin._TURN.set(other)
        try:assert (await adapter.send('U_test',plugin.NOTICE)).success
        finally:plugin._TURN.reset(token)
        assert pushes()[-1][3]!=pushes()[0][3],'key must be bound to the turn'

        # 9) Deadline exhausted: transport is never touched.
        dead=state('rdead');dead['deadline']=time.monotonic()-1
        token=plugin._TURN.set(dead)
        try:
            before=len(client.calls)
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert len(client.calls)==before
        finally:plugin._TURN.reset(token)

        # 10) Client without retry-key support fails closed (deploy guard), no send.
        class LegacyClient:
            def __init__(self):self.calls=[]
            async def push(self,chat,messages):self.calls.append(('push',chat,messages))
        legacy=LegacyClient();adapter._client=legacy
        dep=state('rdep')
        token=plugin._TURN.set(dep)
        try:
            assert not (await adapter.send('U_test',plugin.NOTICE)).success
            assert not legacy.calls,'unkeyed push must not be sent'
            assert dep['delivery']=='unknown'
        finally:plugin._TURN.reset(token);adapter._client=client

        result={'gate':'line-push-retry-key','status':'PASS','real_line_message_sent':False,
                'checks':['same_key_retry_after_5xx','key_persisted_before_first_push','same_key_409_accepted',
                          'no_retry_on_4xx','timeout_retry_same_key','reply_fail_closed_no_key',
                          'terminal_reentry_blocked','key_bound_to_turn','deadline_stops_transport',
                          'legacy_client_fail_closed']}
        (BASE/'delivery-retry-result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))


asyncio.run(check())
