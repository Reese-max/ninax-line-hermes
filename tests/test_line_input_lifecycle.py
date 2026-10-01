"""Versioned LINE input lifecycle: redelivery dedupe, revision head, stale-delivery gate."""
import asyncio
import hashlib
import importlib.util
import json
import os
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


def test_ledger_duplicate_redelivery_still_resumes_lost_work(tmp_path, monkeypatch):
    # A crash between ledger.accept and begin_job leaves the revision current
    # but jobless; the ledger then dedupes the redelivery, so dispatch must
    # still reach the lifecycle store for the resume gate to fire.
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    adapter.allow_all = True
    seen = []

    async def spy(self, event):
        seen.append(event.get('_ninax_input'))

    monkeypatch.setattr(plugin.native.LineAdapter, '_handle_message_event', spy)

    class _AcceptedLedger:
        def reserve(self, event_id):
            return None  # already accepted before the crash

        def accept(self, event_id, owner):
            raise AssertionError('ledger already accepted this event')

        def release(self, event_id, owner):
            raise AssertionError('not our reservation')

    adapter._ledger = _AcceptedLedger()
    event = _event('evt-seen', 1000, 'recover me')
    event['source'] = {'type': 'user', 'userId': CHAT}
    asyncio.run(adapter._dispatch_event(event))
    assert seen and seen[0]['accepted'] and seen[0]['disposition'] == 'ACCEPTED'
    asyncio.run(adapter._dispatch_event(event))
    assert seen[1]['disposition'] == 'RESUME_ACCEPTED' and seen[1]['accepted']


def test_event_without_webhook_id_keeps_legacy_route(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    seen = []

    async def spy(self, event):
        seen.append(event)

    monkeypatch.setattr(plugin.native.LineAdapter, '_handle_message_event', spy)
    event = _event('', 1000, 'no id here')
    event['source'] = {'type': 'user', 'userId': CHAT}
    asyncio.run(adapter._handle_message_event(event))
    assert seen == [event] and '_ninax_input' not in seen[0]
    events_dir = tmp_path / 'hermes-home/line-input-lifecycle/events'
    assert not events_dir.exists() or not list(events_dir.glob('*.json'))


def test_corrupt_state_rebuilds_head_from_receipts(tmp_path):
    store = _store(tmp_path)
    root = tmp_path / 'line-input-lifecycle'
    delivered = _accepted(store, 'evt-1', 1000, 'old')
    store.finish_job(delivered, 'COMPLETED')
    store.record_delivery(delivered, 'DELIVERED', 'a' * 64)
    edited = _accepted(store, 'evt-2', 2000, 'new', kind='messageEdited')
    state_path = next((root / 'messages').glob('*.json'))
    state_path.write_text('{not json', encoding='utf-8')
    # Corruption must not wedge the identity: the head rebuilds from receipts.
    stale = store.accept(_event('evt-stale', 1500, 'ancient', kind='messageEdited'), CHAT)
    assert stale['disposition'] == 'STALE_EVENT' and not stale['accepted']
    assert store.is_current(edited) and not store.is_current(delivered)
    revived = store.accept(_event('evt-3', 3000, 'newer', kind='messageEdited'), CHAT)
    assert revived['accepted'] and revived['input_revision'] == 3
    assert list((root / 'messages').glob('*.corrupt-*')), 'corrupt state stays for audit'
    # A foreign-schema file still fails closed instead of being rewritten.
    rebuilt = next((root / 'messages').glob('*.json'))
    doc = json.loads(rebuilt.read_text())
    doc['schema_version'] = 99
    rebuilt.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(ValueError):
        store.accept(_event('evt-4', 4000, 'future', kind='messageEdited'), CHAT)


def test_postback_error_tap_records_no_answer_delivery(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = _accepted(store, 'evt-1', 1000, 'slow question')
    adapter._client = _FakeClient()
    rid = adapter._cache.register_pending(CHAT, delivery_key=(CHAT, 'm-1'))
    adapter._pending_buttons[(CHAT, 'm-1')] = rid
    adapter._reviewed_cache[rid] = (CHAT, 'b' * 64, binding)
    entry = adapter._cache.get(rid)
    entry.state = plugin.native.State.ERROR
    entry.payload = 'interrupted'
    asyncio.run(adapter._handle_postback_event(_postback(rid)))
    assert adapter._client.calls and adapter._client.calls[0][0] == 'reply'
    job = json.loads(
        next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.json')).read_text())
    assert job['delivery']['status'] == 'NOT_ATTEMPTED', \
        'an error tap must not record the answer payload as delivered'


def test_system_bypass_send_allowed_on_stale_binding(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    original = _accepted(store, 'evt-1', 1000, 'old')
    adapter._client = _FakeClient()
    token = plugin._INPUT.set(original)
    try:
        store.accept(_event('evt-2', 2000, 'edited', kind='messageEdited'), CHAT)
        result = asyncio.run(adapter.send(CHAT, '⚡ Interrupting previous task'))
        assert result.success and adapter._client.calls, \
            'an operational ack is not the stale answer and must still land'
        blocked = asyncio.run(adapter.send(CHAT, 'the actual stale answer'))
        assert not blocked.success and blocked.error == 'stale_input_revision'
    finally:
        plugin._INPUT.reset(token)
    job = json.loads(
        next((tmp_path / 'hermes-home/line-input-lifecycle/jobs').glob('*.1.json')).read_text())
    assert job['delivery']['status'] == 'REJECTED_STALE'


def test_superseded_turn_restores_newer_reply_token(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = store.accept(_event('evt-1', 1000, 'old'), CHAT)
    assert binding['accepted']
    key = (CHAT, 'm-1')
    newer = ('token-new', time.time() + 30)
    adapter._reply_tokens[key] = newer  # stashed by the newer revision's dispatch
    adapter._latest_reply_tokens[key] = newer

    async def during(event, session_key):
        store.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
        adapter._reply_tokens.pop(key, None)  # native cleanup pops blindly

    monkeypatch.setattr(adapter, '_process_current_message_background', during)
    event = SimpleNamespace(raw_message={'_ninax_input': binding},
                            source=SimpleNamespace(chat_id=CHAT), message_id='m-1', text='old')
    asyncio.run(adapter._process_message_background(event, 'line:test'))
    assert adapter._reply_tokens.get(key) == newer

    # A current task must not resurrect a token it already consumed.
    binding2 = store.accept(_event('evt-9', 5000, 'other', message_id='m-9'), CHAT)
    key2 = (CHAT, 'm-9')
    own = ('token-own', time.time() + 30)
    adapter._reply_tokens[key2] = own
    adapter._latest_reply_tokens[key2] = own

    async def during2(event, session_key):
        adapter._reply_tokens.pop(key2, None)

    monkeypatch.setattr(adapter, '_process_current_message_background', during2)
    event2 = SimpleNamespace(raw_message={'_ninax_input': binding2},
                             source=SimpleNamespace(chat_id=CHAT), message_id='m-9', text='other')
    asyncio.run(adapter._process_message_background(event2, 'line:test'))
    assert adapter._reply_tokens.get(key2) is None


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


def test_equal_timestamp_head_is_arrival_order_independent(tmp_path):
    # LINE gives no total order inside one millisecond. Whichever equal-timestamp
    # event arrives last must not decide the head, or a late event rolls the
    # current revision back to older content.
    def head_after(events):
        root = tmp_path / ('lc-' + '-'.join(f'{eid}{text}' for eid, text, _ in events))
        store = _module().InputLifecycle(root)
        current = None
        for event_id, text, kind in events:
            decision = store.accept(_event(event_id, 3000, text, kind=kind), CHAT)
            if decision['accepted']:
                current = decision
        return current, store.is_current(current)

    original = [('evt-1', 'first', 'message'), ('evt-2', 'second', 'messageEdited'),
                ('evt-3', 'current', 'messageEdited')]
    late = original + [('evt-late', 'first', 'message')]
    forward, forward_current = head_after(original)
    backward, backward_current = head_after(list(reversed(late)))
    assert forward_current and backward_current
    assert forward['input_sha256'] == backward['input_sha256'], \
        'a late equal-timestamp event changed the head only because it arrived last'

    # A plain message event is never newer than an edit at the same timestamp.
    store = _store(tmp_path / 'message-vs-edit')
    _accepted(store, 'evt-a', 1000, 'original')
    _accepted(store, 'evt-b', 1000, 'edited', kind='messageEdited')
    assert store.accept(_event('evt-c', 1000, 'original'), CHAT)['disposition'] == 'STALE_EVENT'


def test_equal_timestamp_edit_supersedes_without_wedging(tmp_path):
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'question')
    edited = store.accept(_event('evt-2', 1000, 'edited question', kind='messageEdited'), CHAT)
    assert edited['accepted'] and edited['input_revision'] == 2, \
        'a genuine edit reusing the message timestamp must not be dropped'
    assert not store.is_current(original) and store.is_current(edited)


def test_missing_state_file_rebuilds_head_from_receipts(tmp_path):
    root = tmp_path / 'line-input-lifecycle'
    store = _store(tmp_path)
    original = _accepted(store, 'evt-1', 1000, 'old')
    store.finish_job(original, 'COMPLETED')
    edited = _accepted(store, 'evt-2', 2000, 'new', kind='messageEdited')
    next((root / 'messages').glob('*.json')).unlink()
    # Losing the state file must not pose as a brand-new message: the surviving
    # job receipt would otherwise refuse every later revision forever. The head
    # rebuilds lazily on the next admission decision, failing closed until then.
    restarted = _module().InputLifecycle(root)
    assert not restarted.is_current(edited)
    stale = restarted.accept(_event('evt-3', 1500, 'ancient', kind='messageEdited'), CHAT)
    assert stale['disposition'] == 'STALE_EVENT' and not stale['accepted']
    assert restarted.is_current(edited) and not restarted.is_current(original)
    assert not restarted.begin_job(edited), 'the surviving job receipt still blocks a restart'
    revived = restarted.accept(_event('evt-4', 3000, 'newer', kind='messageEdited'), CHAT)
    assert revived['accepted'] and revived['input_revision'] == 3, 'the rebuilt head keeps its revision'
    assert restarted.begin_job(revived)


def test_stale_delivery_obligation_is_never_redelivered(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = _accepted(store, 'evt-1', 1000, 'old')
    store.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
    updates = []

    class _Ledger:
        @staticmethod
        def _update_state(obligation_id, state, error=''):
            updates.append((obligation_id, state, error))

    monkeypatch.setitem(sys.modules, 'gateway.delivery_ledger', _Ledger)
    stale = plugin.SendResult(success=False, error='stale_input_revision')
    asyncio.run(adapter._finalize_delivery_obligation('obl-1', stale, None, adapter))
    assert updates == [('obl-1', 'abandoned', 'stale_input_revision')], \
        "a 'failed' row is claimed by the boot sweep and redelivered without turn context"

    sent = []

    async def ok(self, obligation_id, result, event, delivery_adapter):
        sent.append(result)

    monkeypatch.setattr(plugin.native.LineAdapter, '_finalize_delivery_obligation', ok,
                        raising=False)
    asyncio.run(adapter._finalize_delivery_obligation('obl-2', plugin.SendResult(success=True),
                                                      None, adapter))
    assert sent, 'every other obligation keeps the native finalize path'


def test_dropped_turn_releases_its_reply_token(tmp_path, monkeypatch):
    plugin, adapter = _plugin_adapter(tmp_path, monkeypatch)
    store = adapter._input_lifecycle
    binding = store.accept(_event('evt-1', 1000, 'old'), CHAT)
    key = (CHAT, 'm-1')
    adapter._reply_tokens[key] = ('token-old', time.time() + 30)
    store.begin_job(binding)
    event = SimpleNamespace(raw_message={'_ninax_input': binding}, text='old',
                            message_id='m-1', source=SimpleNamespace(chat_id=CHAT))
    assert asyncio.run(adapter._process_message_background(event, 'session')) is None
    assert key not in adapter._reply_tokens, 'a dropped turn must not leave a stale reply token'

    # A newer revision's token under the same key survives the older drop.
    binding2 = store.accept(_event('evt-2', 2000, 'new', kind='messageEdited'), CHAT)
    newer = ('token-new', time.time() + 30)
    adapter._reply_tokens[key] = newer
    adapter._latest_reply_tokens[key] = newer
    event2 = SimpleNamespace(raw_message={'_ninax_input': binding2}, text='new',
                             message_id='m-1', source=SimpleNamespace(chat_id=CHAT))
    adapter._process_current_message_background = lambda *a: asyncio.sleep(0)
    asyncio.run(adapter._process_message_background(event2, 'session'))
    assert adapter._reply_tokens.get(key) == newer


def test_atomic_json_fsyncs_the_parent_directory(tmp_path, monkeypatch):
    module = _module()
    synced = []
    real_fsync, real_open = os.fsync, os.open

    def fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    def open_(path, flags, *args, **kwargs):
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, 'fsync', fsync)
    monkeypatch.setattr(module.os, 'open', open_)
    target = tmp_path / 'nested' / 'receipt.json'
    module._atomic_json(target, {'a': 1})
    assert target.exists() and len(synced) == 2, \
        'a rename that is not itself durable can drop the receipt on power loss'
