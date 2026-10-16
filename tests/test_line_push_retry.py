"""Isolated fake-transport checks for LINE Push retry-key recovery (issue #8).

Scope is the video-turn Push path only: one stable ``X-Line-Retry-Key``
persisted per (turn, recipient, approved payload) before the first Push
attempt, bounded same-key retries for ambiguous 5xx/timeout outcomes,
same-key 409 counted as API acceptance, no retry on other 4xx, and the
unchanged fail-closed ambiguous-Reply rule (no key on Reply, no Push
fallback). Real LINE sends are never attempted here.
"""
import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest

from plugins.platforms.line.adapter import _LineClient

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'video-loop' / 'work' / 'profile'
PLUGIN_PATH = PROFILE / 'plugins' / 'line-platform' / '__init__.py'
PUSH_URL = 'https://api.line.me/v2/bot/message/push'


class _Resp:
    """Minimal aiohttp-style response object for the fake session."""

    def __init__(self, status, body='', headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body


class _Session:
    """aiohttp-shaped session returning scripted outcomes per Push attempt."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.client.posts.append({'url': url, 'headers': dict(headers or {}), 'json': json})
        outcome = self.client.plan.pop(0) if self.client.plan else _Resp(200)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Client(_LineClient):
    """Fake LINE client shaped like the pinned ``_LineClient`` transport."""

    def __init__(self):
        self.posts = []
        self.plan = []
        self.replies = []
        self.reply_error = None
        self.legacy_pushes = []
        super().__init__('test-token')

    def _session(self, timeout):
        return _Session(self)

    async def reply(self, token, messages):
        self.replies.append((token, messages))
        if self.reply_error:
            raise self.reply_error


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv('HERMES_HOME', str(home))
    shutil.copytree(PROFILE / 'hooks', home / 'hooks')
    spec = importlib.util.spec_from_file_location('ninax_push_retry_plugin', PLUGIN_PATH)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    from gateway.config import PlatformConfig
    adapter = plugin.VideoLineAdapter(PlatformConfig(enabled=True))
    client = Client()
    adapter._client = client
    return plugin, adapter, client, home


def _state(plugin, home, turn, **overrides):
    binding = {'profile': str(home), 'session_id': 'session', 'session_key': 'line:test',
               'turn_id': turn}
    state = {'chat_id': 'U_test', 'message_id': turn, 'binding': binding, 'delivery': 'pending',
             'reply_token': '', 'reply_expires': 0, 'deadline': time.monotonic() + 30,
             'approval': {'binding': binding, 'kind': 'notice',
                          'payload_sha256': plugin.digest(plugin.native._text_messages(plugin.NOTICE))}}
    state.update(overrides)
    return state


def _send(plugin, adapter, state):
    token = plugin._TURN.set(state)
    try:
        return asyncio.run(adapter.send('U_test', plugin.NOTICE))
    finally:
        plugin._TURN.reset(token)


def _keys(client):
    return [post['headers'].get('X-Line-Retry-Key') for post in client.posts]


def test_push_500_then_success_retries_identical_request(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-5xx')
    client.plan = [_Resp(500, 'oops'), _Resp(200)]
    assert _send(plugin, adapter, state).success
    assert len(client.posts) == 2, client.posts
    keys = _keys(client)
    assert keys[0] and keys[0] == keys[1], 'retry must reuse the persisted retry key'
    assert all(post['url'] == PUSH_URL for post in client.posts)
    assert all(post['json'] == client.posts[0]['json'] for post in client.posts), \
        'same recipient and approved payload on every attempt'
    assert client.posts[0]['json']['to'] == 'U_test'
    assert state['delivery'] == 'delivered'
    record = plugin.read_json(adapter._video_state / 'retry-5xx.delivery.json')
    assert record.get('retry_key') == keys[0], 'key must be persisted for recovery'
    assert not client.legacy_pushes, 'delivery must not fall back to an unkeyed push'


def test_push_timeout_retries_with_same_key(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-timeout')
    client.plan = [TimeoutError('ack lost'), _Resp(200)]
    assert _send(plugin, adapter, state).success
    assert len(client.posts) == 2
    keys = _keys(client)
    assert keys[0] == keys[1] and keys[0]


def test_same_key_409_counts_as_api_acceptance(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-409')
    client.plan = [_Resp(409, 'conflict', {'x-line-accepted-request-id': 'req-1'})]
    assert _send(plugin, adapter, state).success
    assert len(client.posts) == 1, 'same-key 409 means already accepted; never resend'
    assert state['delivery'] == 'delivered'


def test_non_retryable_4xx_is_not_retried(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-400')
    client.plan = [_Resp(400, 'bad request'), _Resp(200)]
    assert not _send(plugin, adapter, state).success
    assert len(client.posts) == 1, 'request-invalid 4xx must not be retried'
    assert state['delivery'] == 'unknown'


def test_ambiguous_reply_stays_fail_closed(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'reply-ambiguous',
                   reply_token='tok-live', reply_expires=time.time() + 30)
    client.reply_error = TimeoutError('accepted but acknowledgement lost')
    assert not _send(plugin, adapter, state).success
    assert len(client.replies) == 1 and not client.posts, \
        'no Push fallback after an ambiguous Reply outcome'
    assert not client.legacy_pushes
    assert state['delivery'] == 'unknown'


def test_retry_key_is_bound_to_turn_recipient_payload(rig):
    plugin, adapter, client, home = rig
    first = _state(plugin, home, 'key-a')
    client.plan = [_Resp(200)]
    assert _send(plugin, adapter, first).success
    second = _state(plugin, home, 'key-b')
    client.plan = [_Resp(200)]
    assert _send(plugin, adapter, second).success
    keys = _keys(client)
    assert len(keys) == 2 and keys[0] and keys[0] != keys[1], \
        'retry key must be bound to the turn (and thereby recipient/payload)'


def test_retry_loop_is_bounded(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-bound')
    client.plan = [_Resp(500, 'still failing')] * 10
    assert not _send(plugin, adapter, state).success
    assert 1 < len(client.posts) <= 3, 'retries must be bounded'
    assert len(set(_keys(client))) == 1
    assert state['delivery'] == 'unknown'


def test_expired_deadline_sends_nothing(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-deadline', deadline=time.monotonic() - 1)
    assert not _send(plugin, adapter, state).success
    assert not client.posts and not client.legacy_pushes and not client.replies


def test_durable_unknown_still_blocks_new_attempt(rig):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'retry-terminal')
    client.plan = [TimeoutError('ack lost')] * 10
    assert not _send(plugin, adapter, state).success
    record = plugin.read_json(adapter._video_state / 'retry-terminal.delivery.json')
    assert record.get('status') == 'unknown' and record.get('retry_key')
    attempted = len(client.posts)
    again = _state(plugin, home, 'retry-terminal')
    assert not _send(plugin, adapter, again).success
    assert len(client.posts) == attempted, \
        'a durable unknown stays terminal outside the bounded retry loop'


def test_transport_without_push_support_fails_closed(rig):
    plugin, adapter, client, home = rig

    class BareClient:
        async def reply(self, token, messages):
            raise AssertionError('reply must not be attempted without a token')

    adapter._client = BareClient()
    state = _state(plugin, home, 'retry-bare')
    assert not _send(plugin, adapter, state).success
    assert state['delivery'] == 'unknown'


def test_review_push_key_is_uuid_and_persisted_before_send(rig, monkeypatch):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'uuid-key')
    receipts = []
    original = _Session.post

    def post(session, *args, **kwargs):
        receipts.append(plugin.read_json(adapter._video_state / 'uuid-key.delivery.json'))
        return original(session, *args, **kwargs)

    monkeypatch.setattr(_Session, 'post', post)
    assert _send(plugin, adapter, state).success
    key = _keys(client)[0]
    assert str(UUID(key)) == key, 'LINE requires a hexadecimal UUID, not a SHA-256 digest'
    assert receipts[0]['status'] == 'sending' and receipts[0]['retry_key'] == key
    assert receipts[0]['binding'] == state['binding']
    assert receipts[0]['payload_sha256'] == state['approval']['payload_sha256']


@pytest.mark.parametrize('invalid_reply', [False, True])
def test_review_hung_push_retries_inside_delivery_deadline(rig, monkeypatch, invalid_reply):
    plugin, adapter, client, home = rig
    monkeypatch.setattr(plugin, '_PUSH_BACKOFF_S', (0, 0))
    state = _state(plugin, home, 'hung-push', deadline=time.monotonic() + 1)
    if invalid_reply:
        state.update(reply_token='expired-at-server', reply_expires=time.time() + 30)
        client.reply_error = RuntimeError('LINE reply 400: Invalid reply token')

    class HungResponse(_Resp):
        async def __aenter__(self):
            await asyncio.Future()

    client.plan = [HungResponse(200), _Resp(200)]
    assert _send(plugin, adapter, state).success
    assert len(client.posts) == 2 and len(set(_keys(client))) == 1
    assert len(client.replies) == int(invalid_reply)


class _UnreadableResp(_Resp):
    async def text(self):
        raise TimeoutError('error response body stalled')


@pytest.mark.parametrize('response', [_Resp(409), _Resp(302), _UnreadableResp(400)])
def test_review_unaccepted_response_fails_without_retry(rig, response):
    plugin, adapter, client, home = rig
    client.plan = [response, _Resp(200)]
    state = _state(plugin, home, 'unaccepted-response')
    assert not _send(plugin, adapter, state).success
    assert len(client.posts) == 1 and state['delivery'] == 'unknown'


def test_review_accepted_409_does_not_wait_for_error_body(rig):
    plugin, adapter, client, home = rig
    client.plan = [_UnreadableResp(409, headers={'x-line-accepted-request-id': 'req-1'})]
    assert _send(plugin, adapter, _state(plugin, home, 'accepted-response')).success
    assert len(client.posts) == 1


@pytest.mark.parametrize('error', [RuntimeError('Session is closed'), ValueError('invalid header')])
def test_review_non_transient_transport_error_is_not_retried(rig, error):
    plugin, adapter, client, home = rig
    client.plan = [error, _Resp(200)]
    state = _state(plugin, home, 'permanent-error')
    assert not _send(plugin, adapter, state).success
    assert len(client.posts) == 1 and state['delivery'] == 'unknown'


def test_review_ci_runs_push_retry_regressions(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('ninax_ci', ROOT / 'video-loop/check_ci.py')
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)
    hermes = tmp_path / 'hermes'
    for name in ('gateway/run_turn_runner.py', 'plugins/platforms/line/adapter.py'):
        target = hermes / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'video-loop/work/hermes' / name, target)
    lock = json.loads((ROOT / 'video-loop/runtime-lock.json').read_text())
    monkeypatch.setattr(ci.sys, 'argv', ['check_ci.py', 'hermes-contract', '--hermes', str(hermes),
                                      '--out', str(tmp_path / 'receipt.json')])
    monkeypatch.setattr(ci.sys, 'platform', 'linux')
    monkeypatch.setattr(ci.sys, 'version_info', (3, 11))
    monkeypatch.setattr(ci.subprocess, 'check_output', lambda *a, **kw: lock['hermes']['commit'])
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(ci.subprocess, 'run', run)
    assert ci.main() == 0
    assert any(str(Path(__file__).resolve()) in command for command in commands), commands


@pytest.mark.parametrize('status', [401, 403, 404, 429])
def test_other_nonretryable_4xx(rig, status):
    plugin, adapter, client, home = rig
    client.plan = [_Resp(status), _Resp(200)]
    assert not _send(plugin, adapter, _state(plugin, home, 'reject-' + str(status))).success
    assert len(client.posts) == 1


def test_native_15_second_timeout_reaches_keyed_retry(rig):
    plugin, adapter, client, home = rig

    class DelayedTimeout(_Resp):
        async def __aenter__(self):
            # A real pending transport, not an immediately raised mock exception.
            # The native timeout must get its full 15s before a keyed retry starts.
            async with asyncio.timeout(client._timeout):
                await asyncio.sleep(client._timeout + 1)

    state = _state(plugin, home, 'native-delayed-timeout', deadline=time.monotonic() + 60)
    client.plan = [DelayedTimeout(200), _Resp(200)]
    started = time.monotonic()
    assert _send(plugin, adapter, state).success
    elapsed = time.monotonic() - started
    assert elapsed >= client._timeout and elapsed < 25
    assert len(client.posts) == 2 and len(set(_keys(client))) == 1
    assert client.posts[0]['json'] == client.posts[1]['json']


def test_hung_attempts_stop_at_turn_deadline(rig, monkeypatch):
    plugin, adapter, client, home = rig
    monkeypatch.setattr(plugin, '_PUSH_BACKOFF_S', (0, 0))

    class HungResponse(_Resp):
        async def __aenter__(self):
            await asyncio.Future()

    client.plan = [HungResponse(200)] * 10
    started = time.monotonic()
    state = _state(plugin, home, 'all-hung', deadline=started + 0.6)
    assert not _send(plugin, adapter, state).success
    assert time.monotonic() - started < 1.5
    assert 1 < len(client.posts) <= 3 and len(set(_keys(client))) == 1
    assert state['delivery'] == 'unknown'


@pytest.mark.parametrize('error', [RuntimeError('LINE reply 500: unavailable'), TimeoutError('reply stalled')])
def test_ambiguous_reply_never_uses_push_key(rig, error):
    plugin, adapter, client, home = rig
    state = _state(plugin, home, 'reply-failed', reply_token='owned', reply_expires=time.time() + 30)
    client.reply_error = error
    assert not _send(plugin, adapter, state).success
    assert len(client.replies) == 1 and not client.posts


def test_legacy_native_push_fails_before_transport(rig):
    plugin, adapter, client, home = rig

    class LegacyClient:
        def __init__(self):
            self.calls = []

        async def push(self, chat, messages):
            self.calls.append((chat, messages))

    legacy = LegacyClient()
    adapter._client = legacy
    assert not _send(plugin, adapter, _state(plugin, home, 'legacy-native')).success
    assert not legacy.calls


def test_install_update_and_rollback_native_adapter(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / 'video-loop'))
    import install

    # Clone only the pinned local fixture; no provider, LINE or remote Git access.
    pinned = Path(sys.modules[_LineClient.__module__].__file__).resolve().parents[3]
    hermes = tmp_path / 'hermes'
    subprocess.run(['git', 'clone', '--shared', '--quiet', str(pinned), str(hermes)], check=True)
    native_path = hermes / 'plugins/platforms/line/adapter.py'
    original = native_path.read_bytes()
    profile = tmp_path / 'profile'
    settings = {'hermes_root': str(hermes), 'pipeline_root': str(tmp_path / 'pipeline'),
                'pipeline_command': str(profile / 'bin/video-pipeline'),
                'stt_python': str(tmp_path / 'stt/bin/python'), 'jobs_root': str(tmp_path / 'jobs'),
                'provider_env_files': [str(profile / '.env')]}
    published = install.BASE
    prior_package = tmp_path / 'prior-package'
    shutil.copytree(published, prior_package)
    # A self-contained prior package works with CI's shallow repository checkout.
    prior_native = (prior_package / 'work/hermes/plugins/platforms/line/adapter.py').read_bytes() + b'\n# prior packaged revision\n'
    (prior_package / 'work/hermes/plugins/platforms/line/adapter.py').write_bytes(prior_native)
    monkeypatch.setattr(install, 'BASE', prior_package)
    native_path.unlink()
    with pytest.raises(ValueError, match='unsupported_hermes_core'):
        install.build_plan(profile, hermes, settings)
    native_path.write_bytes(original)
    first_plan = install.build_plan(profile, hermes, settings)
    assert str(native_path) in {row['path'] for row in first_plan}
    first_receipt = tmp_path / 'install.json'
    first = install.apply(first_plan, first_receipt)
    assert native_path.read_bytes() == prior_native
    assert install.build_plan(profile, hermes, settings, first) == []

    monkeypatch.setattr(install, 'BASE', published)
    update_plan = install.build_plan(profile, hermes, settings, first)
    assert {row['path'] for row in update_plan} == {str(native_path)}
    update_receipt = tmp_path / 'update.json'
    updated = install.apply(update_plan, update_receipt)
    assert install.build_plan(profile, hermes, settings, updated) == []

    # Load the adapter actually installed by build_plan, not the source snapshot.
    spec = importlib.util.spec_from_file_location('ninax_installed_native', native_path)
    installed = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = installed
    spec.loader.exec_module(installed)
    transport = Client()
    real_client = installed._LineClient('fake-token')
    real_client._session = transport._session
    key = '123e4567-e89b-12d3-a456-426614174000'
    asyncio.run(real_client.push('U_test', [{'type': 'text', 'text': 'approved'}], retry_key=key))
    assert _keys(transport) == [key]

    packaged_bytes = native_path.read_bytes()
    native_path.write_bytes(packaged_bytes + b'\n# unrecorded drift\n')
    with pytest.raises(ValueError, match='rollback_refuses_drift'):
        install.rollback(update_receipt)
    with pytest.raises(ValueError, match='unsupported_hermes_core'):
        install.build_plan(profile, hermes, settings, updated)
    native_path.write_bytes(packaged_bytes)
    install.rollback(update_receipt)
    assert native_path.read_bytes() == prior_native
    install.rollback(first_receipt)
    assert native_path.read_bytes() == original
    assert not (profile / 'plugins/line-platform/__init__.py').exists()
