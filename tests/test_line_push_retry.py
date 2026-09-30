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
import shutil
import sys
import time
from pathlib import Path

import pytest

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


class Client:
    """Fake LINE client shaped like the pinned ``_LineClient`` transport."""

    def __init__(self):
        self.posts = []
        self.plan = []
        self.replies = []
        self.reply_error = None
        self.legacy_pushes = []
        self._headers = {'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'}
        self._timeout = 15.0

    def _session(self, timeout):
        return _Session(self)

    async def reply(self, token, messages):
        self.replies.append((token, messages))
        if self.reply_error:
            raise self.reply_error

    async def push(self, chat, messages):
        # Unkeyed transport (the pinned client shape): never carries a retry key.
        self.legacy_pushes.append((chat, messages))


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
