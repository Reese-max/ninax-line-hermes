"""Profile-local LINE specialization; ordinary chat keeps the native Hermes route."""
import asyncio
import contextvars
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

_PLUGIN_DIR = Path(__file__).parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
from line_input_lifecycle import InputLifecycle

from hermes_constants import get_hermes_home
from plugins.platforms.line import adapter as native
from gateway.platforms.base import BasePlatformAdapter, SendResult

_HOOKS = Path(get_hermes_home())/'hooks'
sys.path.insert(0, str(_HOOKS))
from video_evidence import artifact_stamps, atomic_json, digest, evidence_revision, identity, read_json, run
from video_review import MISSING_SOURCE_NOTICE, NOTICE, status_notice

_TURN = contextvars.ContextVar('ninax_video_delivery', default=None)
_INPUT = contextvars.ContextVar('ninax_line_input', default=None)
URL_RE = re.compile(r'https://[^\s<>"\']+')
RECALL = re.compile(r'再(?:說|講)一次|重複.{0,4}(?:摘要|重點)')
RECOVER = re.compile(r'完整一點|補查|再查一次|重新查|補充.{0,6}(?:影片|內容|重點)')
FOLLOW = re.compile(r'(?:這支|那支|這部|那部|剛才的|上一支).{0,6}(?:影片|內容|重點)|影片.{0,6}(?:摘要|重點|說什麼|在講)')


class _VideoRun:
    is_interrupted = False

    def interrupt(self, *args, **kwargs):
        self.is_interrupted = True


class VideoLineAdapter(native.LineAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self._video_home = Path(get_hermes_home()).resolve()
        self._video_state = self._video_home/'video-turns'
        self._video_state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._input_lifecycle = InputLifecycle(self._video_home/'line-input-lifecycle')
        self._video_jobs = (getattr(config,'extra',{}) or {}).get('video_jobs_root', '/workspace/video-timeline-pipeline/jobs')
        self._active_videos = {}
        self._reviewed_cache = {}

    def _session_path(self, session_key):
        return self._video_state/(digest([str(self._video_home),session_key])+'.json')

    def _video_request(self, message, session_key):
        urls = [identity(u.rstrip(').,，。]）')) for u in URL_RE.findall(message or '')]
        urls = [u for u in urls if u]
        if urls:
            return {'url':urls[0]['url'], 'force':bool(RECOVER.search(message)), 'recall':{}}
        if not (RECALL.search(message or '') or RECOVER.search(message or '') or FOLLOW.search(message or '')):
            return None
        saved = read_json(self._session_path(session_key))
        if not identity(saved.get('url') or ''):
            return {'url':None,'force':True,'recall':{}} if (RECOVER.search(message) or FOLLOW.search(message)) else None
        if RECALL.search(message) and saved.get('last_was_video') is False and not FOLLOW.search(message):
            return None
        return {'url':saved['url'], 'force':bool(RECOVER.search(message)),
                'recall':saved.get('result',{}) if RECALL.fullmatch(message.strip(' 。！!')) else {}}

    async def _dispatch_event(self, event):
        event_type = event.get('type') if isinstance(event,dict) else None
        if event_type not in {'message','messageEdited'}:
            return await super()._dispatch_event(event)
        source = event.get('source') or {}
        if self._bot_user_id and source.get('userId','') == self._bot_user_id:
            return
        if not native._allowed_for_source(source, allow_all=self.allow_all, user_ids=self.allowed_users,
                                          group_ids=self.allowed_groups, room_ids=self.allowed_rooms):
            return
        chat_id, _ = native._resolve_chat(source)
        try:
            decision = self._input_lifecycle.accept(event, chat_id)
        except (OSError,ValueError,TypeError,json.JSONDecodeError):
            # A missing/corrupt identity or receipt must not start paid work.
            return
        if not decision['accepted']:
            return
        message_id = decision['message_id']
        previous = self._active_videos.get((chat_id,message_id))
        if previous and decision['input_revision'] > previous.get('input_binding',{}).get('input_revision',0):
            handle = previous.get('run_handle')
            if handle:
                handle.interrupt()
        event = {**event, '_ninax_input':decision}
        return await self._handle_message_event(event)

    async def _process_message_background(self, event, session_key):
        raw = event.raw_message if isinstance(event.raw_message,dict) else {}
        input_binding = raw.get('_ninax_input')
        if not isinstance(input_binding,dict) or not self._input_lifecycle.begin_job(input_binding):
            return None
        input_token = _INPUT.set(input_binding)
        status = 'FAILED'
        try:
            result = await self._process_current_message_background(event, session_key)
            status = 'COMPLETED'
            return result
        except asyncio.CancelledError:
            status = 'INTERRUPTED'
            raise
        finally:
            self._input_lifecycle.finish_job(input_binding,status)
            _INPUT.reset(input_token)

    async def _process_current_message_background(self, event, session_key):
        request = self._video_request(event.text, session_key)
        if not request:
            result = await super()._process_message_background(event, session_key)
            saved = read_json(self._session_path(session_key))
            if saved:
                atomic_json(self._session_path(session_key),{**saved,'last_was_video':False})
            return result
        state = {'request':request, 'message_id':event.message_id, 'chat_id':event.source.chat_id,
                 'session_key':session_key, 'approval':None, 'delivery':'pending',
                 'input_binding':_INPUT.get()}
        raw = event.raw_message if isinstance(event.raw_message,dict) else {}
        state['reply_token'] = raw.get('replyToken','')
        state['reply_expires'] = float(raw.get('timestamp') or time.time()*1000)/1000+native.LINE_REPLY_TOKEN_TTL_SECONDS
        token = _TURN.set(state)
        self._active_videos[(state['chat_id'],state['message_id'])] = state
        try:
            await super()._process_message_background(event, session_key)
        finally:
            key = (state['chat_id'],state['message_id'])
            if self._active_videos.get(key) is state:
                self._active_videos.pop(key,None)
            _TURN.reset(token)

    def run_custom_turn(self, ctx):
        state = _TURN.get()
        if not state:
            if self._video_request(ctx.message, ctx.session_key):
                raise RuntimeError('video_turn_context_missing')
            return None
        handle = _VideoRun()
        state['run_handle'] = handle
        state['deadline'] = time.monotonic()+300
        ctx.agent_holder[0] = handle
        turn_id = digest([str(self._video_home),ctx.session_id,ctx.session_key,
                          ctx.inbound_message_id or state['message_id'],ctx.run_generation,
                          state['input_binding']['input_revision'],state['input_binding']['input_sha256']])
        request = {**state['request'], 'profile':str(self._video_home), 'session_id':ctx.session_id,
                   'session_key':ctx.session_key, 'turn_id':turn_id, 'question':ctx.message,
                   **{k:state['input_binding'][k] for k in ('input_id','input_revision','input_sha256')},
                   'jobs_root':self._video_jobs, 'seconds':290}
        if not request['url']:
            for row in reversed(ctx.history or []):
                if row.get('role')!='user' or not isinstance(row.get('content'),str):
                    continue
                found = [identity(u.rstrip(').,，。]）')) for u in URL_RE.findall(row['content'])]
                found = [u for u in found if u]
                if found:
                    request['url'] = found[0]['url']
                    break
                if not (FOLLOW.search(row['content']) or RECOVER.search(row['content']) or RECALL.search(row['content'])):
                    break
        state['binding'] = {k:request[k] for k in ('profile','session_id','session_key','turn_id',
                                                    'input_id','input_revision','input_sha256')}
        try:
            if not request['url']:
                raise LookupError('video_source_missing')
            result = run([sys.executable, str(self._video_home/'hooks/video_workflow.py')], time.monotonic()+290,
                         input=json.dumps(request,ensure_ascii=False), cwd='/home/box/.hermes/hermes-agent',
                         is_current=lambda: not handle.is_interrupted and ctx._run_still_current())
            doc = json.loads(result.stdout) if result.returncode == 0 else {}
            if not doc.get('approval'):
                raise ValueError('video_workflow_failed')
        except InterruptedError:
            return self._result(ctx, '', interrupted=True)
        except Exception as exc:
            notice = MISSING_SOURCE_NOTICE if not request['url'] else NOTICE
            doc = {'text':notice, 'audit':{'status':'not_checked','reason':type(exc).__name__},
                   'approval':{'binding':state['binding'], 'kind':'notice',
                               'payload_sha256':digest(native._text_messages(notice)), 'approved_at':time.time()}}
        state.update(approval=doc['approval'], result=doc)
        atomic_json(self._session_path(ctx.session_key), {'url':request['url'], 'result':doc,'last_was_video':True})
        return self._result(ctx, doc['text'])

    @staticmethod
    def _result(ctx, text, interrupted=False):
        messages = list(ctx.history or []) + [{'role':'user','content':ctx.message}, {'role':'assistant','content':text}]
        return {'final_response':text, 'messages':messages, 'history_offset':len(ctx.history or []),
                'session_id':ctx.session_id, 'agent_persisted':False, 'response_previewed':False,
                'response_transformed':True, 'completed':not interrupted, 'interrupted':interrupted,
                'api_calls':0, 'tools':[]}

    async def _keep_typing(self, chat_id, *args, **kwargs):
        if _TURN.get():
            # The native slow-response button caches unreviewed text; video turns use typing only.
            return await BasePlatformAdapter._keep_typing(self, chat_id, *args, **kwargs)
        return await super()._keep_typing(chat_id, *args, **kwargs)

    def _wants_auto_tts(self, *args, **kwargs):
        return False if _TURN.get() else super()._wants_auto_tts(*args,**kwargs)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        input_binding = _INPUT.get()
        if input_binding and not self._input_lifecycle.is_current(input_binding):
            try:
                self._input_lifecycle.record_delivery(input_binding,'REJECTED_STALE',
                    digest(native._text_messages(content)))
            except (OSError,ValueError,json.JSONDecodeError):
                pass
            return SendResult(success=False,error='stale_input_revision')
        state = _TURN.get()
        if state:
            return await self._send_messages(chat_id, native._text_messages(content), text=True)
        if any(k[0] == chat_id for k in self._active_videos):
            if native._is_system_bypass(content):
                return await native.LineAdapter._send_messages(self,chat_id,native._text_messages(content),text=True)
            return SendResult(success=False, error='unbound_video_delivery')
        rid = self._pending_buttons.get(chat_id)
        deferred = bool(rid and not native._is_system_bypass(content))
        if deferred:
            self._reviewed_cache[rid] = (chat_id, digest(native._text_messages(content)), input_binding)
        if input_binding:
            payload_sha256 = digest(native._text_messages(content))
            try:
                self._input_lifecycle.record_delivery(input_binding,'UNKNOWN',payload_sha256)
            except (OSError,ValueError,json.JSONDecodeError):
                return SendResult(success=False,error='input_delivery_receipt_failed')
            result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
            if result.success and not deferred:
                try:
                    self._input_lifecycle.record_delivery(input_binding,'DELIVERED',payload_sha256)
                except (OSError,ValueError,json.JSONDecodeError):
                    pass  # Transport may have accepted; never invite a duplicate retry.
            return result
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def _handle_postback_event(self, event):
        try:
            data = json.loads(event.get('postback',{}).get('data',''))
        except (ValueError,TypeError):
            return
        rid = data.get('request_id','')
        entry = self._cache.get(rid)
        chat_id, _ = native._resolve_chat(event.get('source') or {})
        owner = (self._reviewed_cache.get(rid) or (None,))[0]
        if not owner:
            owner = next((chat for chat,pending in self._pending_buttons.items() if pending==rid),None)
        if not entry or owner != chat_id:
            return
        reviewed = None
        if entry.state is native.State.READY:
            reviewed = self._reviewed_cache.get(rid)
            if (not reviewed or reviewed[:2] != (chat_id,digest(native._text_messages(str(entry.payload or ''))))
                    or not reviewed[2] or not self._input_lifecycle.is_current(reviewed[2])):
                if reviewed and reviewed[2]:
                    try:
                        self._input_lifecycle.record_delivery(reviewed[2],'REJECTED_STALE',reviewed[1])
                    except (OSError,ValueError,json.JSONDecodeError):
                        pass
                return
            try:
                self._input_lifecycle.record_delivery(reviewed[2],'UNKNOWN',reviewed[1])
            except (OSError,ValueError,json.JSONDecodeError):
                return
        result = await super()._handle_postback_event(event)
        if entry.state is native.State.DELIVERED and reviewed:
            try:
                self._input_lifecycle.record_delivery(reviewed[2],'DELIVERED',reviewed[1])
            except (OSError,ValueError,json.JSONDecodeError):
                pass
        return result

    def _approved(self, state, chat_id, messages):
        approval = state.get('approval') or {}
        binding = approval.get('binding') or {}
        expected = state.get('binding') or {}
        if (state['chat_id'] != chat_id or not expected or
                any(binding.get(k) != v for k,v in expected.items()) or
                digest(messages) != approval.get('payload_sha256')):
            return False
        if approval.get('kind') == 'notice':
            result = state.get('result') or {}
            return any(messages==native._text_messages(text) for text in
                       (NOTICE,MISSING_SOURCE_NOTICE,status_notice(result.get('source'),result.get('gaps',[]))))
        result = state.get('result') or {}
        if result.get('audit',{}).get('status') != 'pass':
            return False
        current = read_json(Path(result['job'])/'evidence.json')
        saved = current.get('turns',{}).get(binding['turn_id'],{})
        return (saved.get('approved_delivery') == approval and saved.get('binding') == binding
                and saved.get('summary_audit',{}).get('status') == 'pass'
                and digest(saved.get('must_cover')) == binding.get('requirement_revision')
                and current.get('evidence_revision') == binding.get('evidence_revision')
                and evidence_revision(saved['evidence_snapshot']) == binding.get('evidence_revision')
                and artifact_stamps(result['job']) == saved['evidence_snapshot'].get('artifact_stamps'))

    async def _send_messages(self, chat_id, messages, *, force_push=False, text=False):
        state = _TURN.get()
        if not state:
            if any(k[0] == chat_id for k in self._active_videos):
                return SendResult(success=False, error='unbound_video_delivery')
            return await super()._send_messages(chat_id,messages,force_push=force_push,text=text)
        input_binding = state.get('input_binding')
        if not input_binding or not self._input_lifecycle.is_current(input_binding):
            if input_binding:
                try:
                    self._input_lifecycle.record_delivery(input_binding,'REJECTED_STALE',digest(messages))
                except (OSError,ValueError,json.JSONDecodeError):
                    pass
            return SendResult(success=False,error='stale_input_revision')
        if state['delivery'] != 'pending' or not self._approved(state,chat_id,messages):
            return SendResult(success=False,error='video_delivery_not_approved')
        prior = read_json(self._video_state/(state['binding']['turn_id']+'.delivery.json'))
        if prior.get('status') in {'sending','unknown','delivered'}:
            return SendResult(success=False,error='video_delivery_already_attempted')
        state['delivery'] = 'sending'
        self._record_delivery(state)
        owned_token = state.pop('reply_token','')
        if (self._reply_tokens.get(chat_id) or ('',0))[0] == owned_token:
            self._reply_tokens.pop(chat_id,None)
        token = owned_token if time.time()<state.get('reply_expires',0) else ''
        used_reply = bool(token)
        try:
            remaining = min(10,state.get('deadline',time.monotonic()+10)-time.monotonic())
            if remaining<=0:
                raise TimeoutError('video_delivery_deadline')
            async with asyncio.timeout(remaining):
                if used_reply and not force_push:
                    try:
                        await self._client.reply(token,messages)
                    except RuntimeError as exc:
                        # Only a definite invalid-token rejection permits push. A timeout may have posted.
                        if not (str(exc).startswith('LINE reply 400:') and 'Invalid reply token' in str(exc)):
                            raise
                        await self._client.push(chat_id,messages)
                else:
                    await self._client.push(chat_id,messages)
        except Exception as exc:
            state['delivery'] = 'unknown'
            self._record_delivery(state)
            return SendResult(success=False,error=type(exc).__name__)
        state['delivery'] = 'delivered'
        self._record_delivery(state)
        return SendResult(success=True,message_id=state['binding']['turn_id'])

    def _record_delivery(self, state):
        input_binding = state['input_binding']
        receipt = {'binding':state['binding'],'input_binding':input_binding,'status':state['delivery'],
                   'payload_sha256':state['approval']['payload_sha256'],'time':time.time()}
        atomic_json(self._video_state/(state['binding']['turn_id']+'.delivery.json'),receipt)
        lifecycle_status = 'DELIVERED' if state['delivery']=='delivered' else 'UNKNOWN'
        self._input_lifecycle.record_delivery(input_binding,lifecycle_status,state['approval']['payload_sha256'])


def register(ctx):
    # Keep native setup/auth/limits/standalone delivery metadata in one place.
    native.register(SimpleNamespace(register_platform=lambda **kw:
        ctx.register_platform(**{**kw,'adapter_factory':lambda cfg:VideoLineAdapter(cfg)})))
