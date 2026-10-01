"""Versioned LINE input lifecycle: redelivery dedupe, revision head, stale-delivery gate."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / 'video-loop/work/profile/plugins/line-platform'
HOOKS = ROOT / 'video-loop/work/profile/hooks'
CHAT = 'C-room-1'


def _module():
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    import line_input_lifecycle
    return line_input_lifecycle


def _store(tmp_path):
    return _module().InputLifecycle(tmp_path / 'line-input-lifecycle')


def _event(event_id, timestamp, text, *, kind='message', redelivery=False,
           message_id='m-1', **fields):
    message = {'id': message_id, 'type': 'text', 'text': text}
    message.update(fields)
    return {'type': kind, 'webhookEventId': event_id, 'timestamp': timestamp,
            'deliveryContext': {'isRedelivery': redelivery}, 'message': message}


def _plugin_adapter(tmp_path, monkeypatch):
    """Load the real profile plugin and adapter against a temp HERMES_HOME."""
    home = tmp_path / 'hermes-home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    shutil.copytree(HOOKS, home / 'hooks')
    spec = importlib.util.spec_from_file_location('ninax_test_plugin', PLUGIN_DIR / '__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    from gateway.config import PlatformConfig
    return plugin, plugin.VideoLineAdapter(PlatformConfig(enabled=True))


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def reply(self, token, messages):
        self.calls.append(('reply', token, messages))

    async def push(self, chat, messages):
        self.calls.append(('push', chat, messages))


def _accepted(store, event_id, ts, text, *, message_id='m-1', kind='message', **fields):
    decision = store.accept(_event(event_id, ts, text, message_id=message_id, kind=kind, **fields), CHAT)
    assert decision['accepted'], decision
    assert store.begin_job(decision)
    return decision


def _video_state(plugin, adapter, binding, turn='turn-1'):
    binding = dict(binding)
    approval_binding = {k: binding[k] for k in ('input_id', 'input_revision', 'input_sha256')}
    approval_binding.update(profile=str(adapter._video_home), session_id='session',
                            session_key='line:test', turn_id=turn)
    return {'chat_id': CHAT, 'message_id': binding['message_id'], 'binding': approval_binding,
            'input_binding': binding, 'delivery': 'pending', 'reply_token': '', 'reply_expires': 0,
            'result': {},
            'approval': {'binding': approval_binding, 'kind': 'notice',
                         'payload_sha256': plugin.digest(plugin.native._text_messages(plugin.NOTICE))}}


def test_duplicate_webhook_redelivery_creates_one_job(tmp_path):
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'summarize this')
    duplicate = store.accept(_event('evt-1', 1000, 'summarize this', redelivery=True), CHAT)
    assert duplicate['disposition'] == 'DUPLICATE_EVENT' and not duplicate['accepted']
    assert not store.begin_job(original), 'a delivered event must not start expensive work twice'
    assert len(list((tmp_path / 'line-input-lifecycle/jobs').glob('*.json'))) == 1


def test_event_receipt_keeps_identity_and_content_hash(tmp_path):
    store = _store(tmp_path)
    store.accept(_event('evt-1', 1000, 'exact content'), CHAT)
    receipt = json.loads(next((tmp_path / 'line-input-lifecycle/events').glob('*.json')).read_text())
    binding = receipt['binding']
    assert binding['webhook_event_id'] == 'evt-1' and binding['message_id'] == 'm-1'
    assert binding['input_revision'] == 1 and len(binding['input_sha256']) == 64
    assert receipt['is_redelivery'] is False and receipt['event_type'] == 'message'
    assert receipt['eligible_for_expensive_work'] is True


def test_edit_before_work_supersedes_previous_revision(tmp_path):
    store = _store(tmp_path)
    original = store.accept(_event('evt-1', 1000, 'old question'), CHAT)
    assert original['accepted'] and original['input_revision'] == 1
    edited = store.accept(_event('evt-2', 2000, 'new question', kind='messageEdited'), CHAT)
    assert edited['accepted'] and edited['input_revision'] == 2 and edited['disposition'] == 'ACCEPTED'
    assert not store.is_current(original) and store.is_current(edited)
    assert not store.begin_job(original), 'superseded revision must not start work'
    state = json.loads(next((tmp_path / 'line-input-lifecycle/messages').glob('*.json')).read_text())
    assert state['revisions'][0]['status'] == 'SUPERSEDED'
    assert state['revisions'][1]['status'] == 'CURRENT' and state['current_revision'] == 2


def test_edit_during_work_marks_running_job_superseded(tmp_path):
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'old')
    store.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
    job = json.loads(next((tmp_path / 'line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['status'] == 'SUPERSEDED' and job['superseded_by_revision'] == 2
    store.finish_job(original, 'COMPLETED')
    job = json.loads(next((tmp_path / 'line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['status'] == 'SUPERSEDED', 'late completion must not overwrite SUPERSEDED'


def test_out_of_order_late_event_cannot_overwrite_head(tmp_path):
    store = _store(tmp_path)
    _accepted(store, 'evt-1', 1000, 'old')
    current = _accepted(store, 'evt-2', 3000, 'new', kind='messageEdited')
    late = store.accept(_event('evt-late', 2000, 'late edit', kind='messageEdited'), CHAT)
    assert late['disposition'] == 'STALE_EVENT' and not late['accepted']
    assert store.is_current(current)
    state = json.loads(next((tmp_path / 'line-input-lifecycle/messages').glob('*.json')).read_text())
    assert state['current_revision'] == 2 and state['current_event_timestamp_ms'] == 3000


def test_same_content_dedupe_and_equal_timestamp_tiebreak(tmp_path):
    store = _store(tmp_path)
    edited = _accepted(store, 'evt-1', 1000, 'new')
    # Edit events may reuse the original message timestamp; distinct content at
    # an equal timestamp supersedes by arrival order instead of wedging.
    edited_again = store.accept(_event('evt-2', 1000, 'edited again', kind='messageEdited'), CHAT)
    assert edited_again['accepted'] and edited_again['input_revision'] == 2
    assert not store.is_current(edited) and store.is_current(edited_again)
    same = store.accept(_event('evt-3', 2000, 'edited again', kind='messageEdited'), CHAT)
    # Revision 2 was accepted but never began work; an identical later event
    # resumes it rather than stranding the revision as a dead duplicate.
    assert same['disposition'] == 'RESUME_ACCEPTED' and same['accepted']
    assert store.begin_job(same)
    again = store.accept(_event('evt-5', 2500, 'edited again', kind='messageEdited'), CHAT)
    assert again['disposition'] == 'DUPLICATE_CONTENT' and not again['accepted']
    late = store.accept(_event('evt-4', 1500, 'old again', kind='messageEdited'), CHAT)
    assert late['disposition'] == 'STALE_EVENT' and not late['accepted']
    assert store.is_current(edited_again)


def test_token_rotation_is_not_a_new_revision(tmp_path):
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'mention', quotedMessageId='q-1')
    rotated = store.accept(_event('evt-2', 2000, 'mention', kind='messageEdited',
                                  quotedMessageId='q-1', quoteToken='rotated',
                                  markAsReadToken='rotated'), CHAT)
    assert rotated['disposition'] == 'DUPLICATE_CONTENT' and not rotated['accepted']
    changed = store.accept(_event('evt-3', 3000, 'mention', kind='messageEdited',
                                  quotedMessageId='q-2'), CHAT)
    assert changed['accepted'] and changed['input_revision'] == 2
    assert changed['input_sha256'] != original['input_sha256']


def test_two_messages_with_same_text_are_distinct_inputs(tmp_path):
    store = _store(tmp_path)
    first = _accepted(store, 'evt-1', 1000, 'same text', message_id='m-1')
    second = _accepted(store, 'evt-2', 2000, 'same text', message_id='m-2')
    assert first['input_id'] != second['input_id']
    assert store.is_current(first) and store.is_current(second)


def test_superseded_job_keeps_cost_receipt_not_refund(tmp_path):
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'old')
    store.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
    job = json.loads(next((tmp_path / 'line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['status'] == 'SUPERSEDED'
    assert job['cost_attribution_key'] == f"{original['input_id']}:{original['input_revision']}"
    store.record_delivery(original, 'UNKNOWN', 'a' * 64)
    job = json.loads(next((tmp_path / 'line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['delivery']['status'] == 'UNKNOWN', 'an ambiguous send is a receipt, not a refund'


def test_edit_after_delivery_keeps_delivered_receipt(tmp_path):
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'old')
    store.finish_job(original, 'COMPLETED')
    store.record_delivery(original, 'DELIVERED', 'a' * 64)
    edited = store.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
    assert edited['accepted'] and edited['input_revision'] == 2
    store.record_delivery(original, 'REJECTED_STALE', 'b' * 64)
    job = json.loads(next((tmp_path / 'line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['status'] == 'COMPLETED' and job['delivery']['status'] == 'DELIVERED'
    assert job['delivery']['payload_sha256'] == 'a' * 64
    assert [a['status'] for a in job['delivery_attempts']] == ['DELIVERED', 'REJECTED_STALE']


def test_restart_recovers_dedupe_revision_and_delivery_state(tmp_path):
    root = tmp_path / 'line-input-lifecycle'
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'old')
    edited = _accepted(store, 'evt-2', 2000, 'new', kind='messageEdited')
    store.finish_job(edited, 'COMPLETED')
    store.record_delivery(edited, 'DELIVERED', 'a' * 64)

    restarted = _module().InputLifecycle(root)
    assert restarted.is_current(edited) and not restarted.is_current(original)
    duplicate = restarted.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
    assert duplicate['disposition'] == 'DUPLICATE_EVENT' and not duplicate['accepted']
    job = json.loads(next((root / 'jobs').glob('*.2.json')).read_text())
    assert job['delivery']['status'] == 'DELIVERED'

    resumed = restarted.accept(_event('evt-3', 3000, 'after restart'), CHAT)
    assert resumed['disposition'] == 'ACCEPTED' and resumed['accepted']


def test_torn_receipt_resumes_same_revision(tmp_path):
    root = tmp_path / 'line-input-lifecycle'
    store = _store(tmp_path)
    accepted = store.accept(_event('evt-torn', 1000, 'recover me'), CHAT)
    assert accepted['accepted']
    # Crash after the message state commit but before the event receipt commit.
    receipt = root / 'events' / f"{hashlib.sha256(b'evt-torn').hexdigest()}.json"
    receipt.unlink()
    recovered = _module().InputLifecycle(root).accept(_event('evt-torn', 1000, 'recover me'), CHAT)
    assert recovered['disposition'] == 'RESUME_ACCEPTED' and recovered['accepted']
    assert recovered['input_revision'] == accepted['input_revision']
    assert _module().InputLifecycle(root).begin_job(recovered)


def test_torn_resume_survives_duplicate_takeover(tmp_path):
    root = tmp_path / 'line-input-lifecycle'
    store = _store(tmp_path)
    original = store.accept(_event('evt-a', 1000, 'same'), CHAT)
    assert original['accepted']
    # Crash after the message state commit but before the event receipt write.
    (root / 'events' / f"{hashlib.sha256(b'evt-a').hexdigest()}.json").unlink()
    # A distinct same-content event at a later timestamp takes over the
    # observed-event bookkeeping and carries the resume while no job exists.
    carrier = store.accept(_event('evt-b', 2000, 'same'), CHAT)
    assert carrier['disposition'] == 'RESUME_ACCEPTED' and carrier['accepted']
    # The original event's redelivery still resumes the same revision;
    # ownership of the bookkeeping must not strand accepted work.
    resumed = store.accept(_event('evt-a', 1000, 'same', redelivery=True), CHAT)
    assert resumed['disposition'] == 'RESUME_ACCEPTED' and resumed['accepted']
    assert resumed['input_revision'] == original['input_revision']
    # A redelivery of the bookkeeping event can also carry the resume.
    via_dup = store.accept(_event('evt-b', 2000, 'same', redelivery=True), CHAT)
    assert via_dup['disposition'] == 'RESUME_ACCEPTED' and via_dup['accepted']
    # A RESUME_ACCEPTED receipt is itself resumable after another torn write.
    resumed_again = store.accept(_event('evt-a', 1000, 'same', redelivery=True), CHAT)
    assert resumed_again['disposition'] == 'RESUME_ACCEPTED' and resumed_again['accepted']
    assert store.begin_job(resumed_again)
    # Once the job exists, every same-content event is a plain duplicate.
    assert store.accept(_event('evt-a', 1000, 'same', redelivery=True), CHAT)['disposition'] == 'DUPLICATE_EVENT'
    assert store.accept(_event('evt-b', 2000, 'same', redelivery=True), CHAT)['disposition'] == 'DUPLICATE_EVENT'


def test_restart_marks_orphaned_started_job_interrupted(tmp_path):
    root = tmp_path / 'line-input-lifecycle'
    store = _store(tmp_path)
    _accepted(store, 'evt-1', 1000, 'crash me')
    restarted = _module().InputLifecycle(root)
    job = json.loads(next((root / 'jobs').glob('*.json')).read_text())
    assert job['status'] == 'INTERRUPTED' and job['interrupted_reason'] == 'process_restart'
    duplicate = restarted.accept(_event('evt-1', 1000, 'crash me', redelivery=True), CHAT)
    assert duplicate['disposition'] == 'DUPLICATE_EVENT' and not duplicate['accepted']


def test_malformed_and_boolean_revision_fail_closed(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.accept(_event('evt-x', 1000, 'x', kind='unsupportedType'), CHAT)
    with pytest.raises(ValueError):
        store.accept(_event('evt-x', 'not-an-int', 'x'), CHAT)
    accepted = _accepted(store, 'evt-1', 1000, 'ok')
    malformed = dict(accepted)
    malformed['input_revision'] = True
    with pytest.raises(ValueError):
        store._binding_fields(malformed)
    with pytest.raises(ValueError):
        store.begin_job(malformed)


def test_stale_revision_cannot_pass_delivery_gate(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    original = _accepted(store, 'evt-1', 1000, 'old')
    state = _video_state(plugin, adapter, original)
    adapter._client = _FakeClient()
    token = plugin._TURN.set(state)
    try:
        store.accept(_event('evt-2', 2000, 'edited', kind='messageEdited'), CHAT)
        result = asyncio.run(adapter._send_messages(CHAT, plugin.native._text_messages(plugin.NOTICE)))
        assert not result.success and result.error == 'stale_input_revision'
        assert adapter._client.calls == [], 'stale summary must never reach LINE'
    finally:
        plugin._TURN.reset(token)
    job = json.loads(next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['delivery']['status'] == 'REJECTED_STALE'


def test_current_revision_delivers_with_revision_receipt(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = _accepted(store, 'evt-1', 1000, 'current')
    state = _video_state(plugin, adapter, binding)
    adapter._client = _FakeClient()
    token = plugin._TURN.set(state)
    try:
        result = asyncio.run(adapter._send_messages(CHAT, plugin.native._text_messages(plugin.NOTICE)))
        assert result.success, result.error
        assert adapter._client.calls == [('push', CHAT, plugin.native._text_messages(plugin.NOTICE))]
    finally:
        plugin._TURN.reset(token)
    job = json.loads(next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['delivery']['status'] == 'DELIVERED'
    delivery = json.loads(next((tmp_path / 'hermes-home/video-turns').glob('turn-1.delivery.json')).read_text())
    input_binding = delivery['input_binding']
    assert input_binding['webhook_event_id'] == 'evt-1' and input_binding['message_id'] == 'm-1'
    assert input_binding['input_revision'] == 1 and input_binding['input_sha256'] == binding['input_sha256']


def test_send_rejects_stale_input_for_ordinary_turn(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    original = _accepted(store, 'evt-1', 1000, 'old')
    adapter._client = _FakeClient()
    token = plugin._INPUT.set(original)
    try:
        store.accept(_event('evt-2', 2000, 'edited', kind='messageEdited'), CHAT)
        result = asyncio.run(adapter.send(CHAT, 'answer to old text'))
        assert not result.success and result.error == 'stale_input_revision'
        assert adapter._client.calls == []
    finally:
        plugin._INPUT.reset(token)
    job = json.loads(next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['delivery']['status'] == 'REJECTED_STALE'


def test_dispatch_routes_message_edited_through_ledger_and_lifecycle(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    adapter.allow_all = True
    calls = []

    class _Ledger:
        def reserve(self, event_id):
            calls.append(('reserve', event_id))
            return 'owner'

        def accept(self, event_id, owner):
            calls.append(('accept', event_id, owner))

        def release(self, event_id, owner):
            calls.append(('release', event_id, owner))

    adapter._ledger = _Ledger()
    event = _event('evt-edit', 1000, 'edited text', kind='messageEdited')
    event['source'] = {'type': 'user', 'userId': 'U' + '9' * 31}
    asyncio.run(adapter._dispatch_event(event))
    assert calls == [('reserve', 'evt-edit'), ('accept', 'evt-edit', 'owner')]
    receipt = json.loads(
        next((tmp_path / 'hermes-home/line-input-lifecycle/events').glob('*.json')).read_text())
    assert receipt['event_type'] == 'messageEdited' and receipt['disposition'] == 'ACCEPTED'
    assert adapter._input_lifecycle.is_current(receipt['binding'])


def _postback(rid, token='tap'):
    return {'replyToken': token, 'source': {'type': 'user', 'userId': CHAT},
            'postback': {'data': json.dumps({'action': 'show_response', 'request_id': rid})}}


def test_deferred_send_and_postback_delivery_follow_revision(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = _accepted(store, 'evt-1', 1000, 'slow question')
    adapter._client = _FakeClient()
    rid = adapter._cache.register_pending(CHAT, delivery_key=(CHAT, 'm-1'))
    adapter._pending_buttons[(CHAT, 'm-1')] = rid
    turn_token = adapter._delivery_turn.set((CHAT, 'm-1'))
    input_token = plugin._INPUT.set(binding)
    try:
        result = asyncio.run(adapter.send(CHAT, 'deferred answer'))
        assert result.success and adapter._client.calls == []
        job = json.loads(
            next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.json')).read_text())
        assert job['delivery']['status'] == 'NOT_ATTEMPTED', 'a cached payload was not delivered'
    finally:
        plugin._INPUT.reset(input_token)
        adapter._delivery_turn.reset(turn_token)
    asyncio.run(adapter._handle_postback_event(_postback(rid)))
    assert adapter._client.calls == [('reply', 'tap', plugin.native._text_messages('deferred answer'))]
    job = json.loads(
        next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.json')).read_text())
    assert job['delivery']['status'] == 'DELIVERED'
    assert rid not in adapter._reviewed_cache


def test_stale_postback_tap_is_rejected(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = _accepted(store, 'evt-1', 1000, 'slow question')
    adapter._client = _FakeClient()
    rid = adapter._cache.register_pending(CHAT, delivery_key=(CHAT, 'm-1'))
    adapter._pending_buttons[(CHAT, 'm-1')] = rid
    turn_token = adapter._delivery_turn.set((CHAT, 'm-1'))
    input_token = plugin._INPUT.set(binding)
    try:
        asyncio.run(adapter.send(CHAT, 'deferred answer'))
    finally:
        plugin._INPUT.reset(input_token)
        adapter._delivery_turn.reset(turn_token)
    store.accept(_event('evt-2', 2000, 'edited question', kind='messageEdited'), CHAT)
    asyncio.run(adapter._handle_postback_event(_postback(rid)))
    assert adapter._client.calls == [], 'a superseded cached answer must not be delivered'
    job = json.loads(
        next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['delivery']['status'] == 'REJECTED_STALE'
    assert rid not in adapter._reviewed_cache


def test_new_revision_interrupts_active_video_run(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    original = _accepted(store, 'evt-1', 1000, 'old')
    handle = SimpleNamespace(interrupted=False)
    handle.interrupt = lambda: setattr(handle, 'interrupted', True)
    adapter._active_videos[(CHAT, 'm-1')] = {'input_binding': original, 'run_handle': handle}
    event = _event('evt-2', 2000, 'edited', kind='messageEdited')
    event['source'] = {'type': 'user', 'userId': CHAT}
    asyncio.run(adapter._handle_message_event(event))
    assert handle.interrupted, 'a newer revision must interrupt the superseded video run'


def test_superseded_input_drops_before_background_work(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    original = _accepted(store, 'evt-1', 1000, 'old')
    store.accept(_event('evt-2', 2000, 'edited', kind='messageEdited'), CHAT)
    event = SimpleNamespace(raw_message={'_ninax_input': original}, text='old',
                            message_id='m-1', source=SimpleNamespace(chat_id=CHAT))
    reached = []
    adapter._process_current_message_background = lambda *a: reached.append(a) or asyncio.sleep(0)
    result = asyncio.run(adapter._process_message_background(event, 'session'))
    assert result is None and reached == [], 'superseded input must not start a turn'
