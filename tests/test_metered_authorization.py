"""Stub-provider checks for the metered-fetch positive authorization gate (issue #4).

A metered provider POST requires a one-shot grant bound to the exact source
identity, recorded in the per-video state receipt by ``authorize_metered_fetch``
and consumed atomically before the POST. Absent, expired, consumed, mismatched
or malformed grants fail closed with ``status='not_authorized'`` and
``metered_requests=0``; ``submitted``/``pending``/``unknown`` receipts only poll
the recorded remote task and never repeat the POST. ``NINAX_DISABLE_METERED_FETCH``
remains the first, emergency kill-switch — never an approval itself. No real
provider is contacted here; ``bright.http_json`` is a counting stub.
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / 'video-loop' / 'work' / 'profile' / 'hooks'
sys.path.insert(0, str(HOOKS))

import apify_ig_fallback as apify
import brightdata_ig_fallback as bright
import video_evidence as ev
import video_recovery as recovery

URL = 'https://www.instagram.com/reel/CaseSensitiveID/'
OTHER_URL = 'https://www.instagram.com/reel/OtherReel99/'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Stubbed Bright Data transport; `posts` counts billable submissions."""
    posts = []

    def stub(method, endpoint, *args, **kwargs):
        if method == 'POST':
            posts.append(endpoint)
            return {'snapshot_id': 's_new'}
        return {'status': 'running'}

    monkeypatch.setattr(bright, 'token', lambda: 'fake-not-a-real-token')
    monkeypatch.setattr(bright, 'http_json', stub)
    monkeypatch.setattr(apify, 'token', lambda: '')
    monkeypatch.setattr(apify, 'api', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('apify must not run while the brightdata stub is active')))
    return tmp_path, posts


def _fetch(url, receipt, root, seconds=5):
    return recovery._metered_fetch(url, receipt, time.monotonic() + seconds, root)


def test_absent_grant_denies_before_provider(rig):
    root, posts = rig
    receipt = root / 'denied-absent.json'
    denied = _fetch(URL, receipt, root)
    assert denied['status'] == 'not_authorized'
    assert denied['reason'] == 'metered_authorization_absent'
    assert denied['metered_requests'] == 0 and not posts
    persisted = ev.read_json(receipt)
    assert persisted['status'] == 'not_authorized' and persisted['metered_requests'] == 0


@pytest.mark.parametrize('name,reason,tamper', [
    ('mismatch', 'metered_authorization_mismatch',
     lambda grant: grant.update(id='OtherReel99', url=OTHER_URL)),
    ('expired', 'metered_authorization_expired',
     lambda grant: grant.update(expires_at=time.time() - 1)),
    ('consumed', 'metered_authorization_consumed',
     lambda grant: grant.update(consumed=True)),
    ('invalid', 'metered_authorization_invalid',
     lambda grant: grant.pop('expires_at')),
    ('unscoped', 'metered_authorization_invalid',
     lambda grant: grant.pop('scope')),
    ('not-yet-valid', 'metered_authorization_invalid',
     lambda grant: grant.update(authorized_at=time.time() + 3600)),
    ('infinite-expiry', 'metered_authorization_invalid',
     lambda grant: grant.update(expires_at=float('inf'))),
    ('nan-expiry', 'metered_authorization_invalid',
     lambda grant: grant.update(expires_at=float('nan'))),
])
def test_positive_grant_required(rig, name, reason, tamper):
    root, posts = rig
    receipt = root / f'denied-{name}.json'
    recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    doc = ev.read_json(receipt)
    tamper(doc['authorization'])
    ev.atomic_json(receipt, doc)
    denied = _fetch(URL, receipt, root)
    assert denied['status'] == 'not_authorized' and denied['reason'] == reason
    assert denied['metered_requests'] == 0 and not posts


def test_denied_receipt_accepts_a_later_valid_grant(rig):
    root, posts = rig
    receipt = root / 'denied-then-authorized.json'
    assert _fetch(URL, receipt, root)['status'] == 'not_authorized'
    grant = recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    assert grant['ok'] and grant['authorization']['id'] == 'CaseSensitiveID'
    fetched = _fetch(URL, receipt, root)
    assert fetched['status'] == 'submitted' and fetched['metered_requests'] == 1
    assert len(posts) == 1


def test_valid_grant_allows_exactly_one_submission(rig):
    root, posts = rig
    receipt = root / 'authorized-fetch.json'
    grant = recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    assert grant['ok'] and grant['authorization']['scope'] == 'single_metered_submission'
    assert not grant['authorization']['consumed']
    fetched = _fetch(URL, receipt, root)
    assert fetched['status'] == 'submitted' and fetched['remote_task_id'] == 's_new'
    assert fetched['metered_requests'] == 1 and len(posts) == 1
    consumed = ev.read_json(receipt)['authorization']
    assert consumed['consumed'] and consumed['consumed_at'] >= consumed['authorized_at']
    posts.clear()
    again = _fetch(URL, receipt, root, seconds=1)
    assert again['status'] == 'submitted' and not posts, \
        'a submitted task is polled, never resubmitted'


def test_in_flight_submission_refuses_new_grant(rig):
    root, posts = rig
    receipt = root / 'in-flight.json'
    recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    assert _fetch(URL, receipt, root)['status'] == 'submitted'
    with pytest.raises(ValueError, match='metered_submission_in_progress'):
        recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    assert len(posts) == 1


def test_pending_receipt_polls_without_post_or_grant(rig):
    root, posts = rig
    receipt = root / 'pending-legacy.json'
    ev.atomic_json(receipt, {'url': ev.identity(URL)['url'], 'backend': 'brightdata',
                             'status': 'pending', 'remote_task_id': 's_test',
                             'metered_requests': 1})
    resumed = _fetch(URL, receipt, root, seconds=1)
    assert resumed['remote_task_id'] == 's_test' and resumed['metered_requests'] == 1
    assert not posts


def test_response_unknown_is_never_retriggered(rig, monkeypatch):
    root, posts = rig
    monkeypatch.setattr(bright, 'http_json', lambda *a, **kw: (_ for _ in ()).throw(
        TimeoutError('provider_timeout')))
    receipt = root / 'unknown-fetch.json'
    recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    lost = _fetch(URL, receipt, root)
    assert lost['status'] == 'unknown' and lost['metered_requests'] == 1
    assert ev.read_json(receipt)['authorization']['consumed']
    retried = _fetch(URL, receipt, root)
    assert retried.get('resumed') and retried['status'] == 'unknown'


def test_provider_rejection_marks_failed_without_billable_request(rig, monkeypatch):
    root, posts = rig
    monkeypatch.setattr(bright, 'http_json', lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError('http_403:permission denied')))
    receipt = root / 'rejected-request.json'
    recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    rejected = _fetch(URL, receipt, root)
    assert rejected['status'] == 'failed' and rejected['metered_requests'] == 0
    assert ev.read_json(receipt)['authorization']['consumed']


def test_completed_receipt_is_terminal_and_audited(rig, monkeypatch):
    root, posts = rig

    def completing(method, endpoint, *args, **kwargs):
        if method == 'POST':
            posts.append(endpoint)
            return {'snapshot_id': 's_done'}
        return {'status': 'ready'} if '/progress/' in endpoint else [
            {'url': URL, 'shortcode': 'CaseSensitiveID'}]

    monkeypatch.setattr(bright, 'http_json', completing)
    receipt = root / 'completed-fetch.json'
    recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    done = _fetch(URL, receipt, root, seconds=8)
    assert done['ok'] and done['status'] == 'completed' and done['metered_requests'] == 1
    assert len(posts) == 1
    posts.clear()
    finished = _fetch(URL, receipt, root)
    assert finished['status'] == 'completed' and finished.get('resumed') and not posts


def test_recorded_remote_task_blocks_new_grant(rig):
    root, _ = rig
    receipt = root / 'task-without-status.json'
    ev.atomic_json(receipt, {'url': ev.identity(URL)['url'],
                             'remote_task_id': 's_orphan'})
    with pytest.raises(ValueError, match='metered_submission_in_progress'):
        recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)


def test_authorize_rejects_non_finite_lifetime(rig):
    root, _ = rig
    with pytest.raises(ValueError, match='metered_authorization_invalid'):
        recovery.authorize_metered_fetch(URL, root / 'inf-grant.json',
                                         time.monotonic() + 5, seconds=float('inf'))
    assert not (root / 'inf-grant.json').exists()


def test_kill_switch_also_blocks_grant_issuance(rig, monkeypatch):
    root, _ = rig
    monkeypatch.setenv('NINAX_DISABLE_METERED_FETCH', '1')
    receipt = root / 'disabled-grant.json'
    assert recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5) == {
        'ok': False, 'reason': 'metered_fetch_disabled'}
    assert not receipt.exists()


def test_kill_switch_fails_closed_before_any_access(rig, monkeypatch):
    root, posts = rig
    receipt = root / 'disabled.json'
    recovery.authorize_metered_fetch(URL, receipt, time.monotonic() + 5)
    monkeypatch.setattr(bright, 'http_json', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('provider must not be reached while the kill-switch is set')))
    monkeypatch.setenv('NINAX_DISABLE_METERED_FETCH', '1')
    assert recovery.metered_fetch(URL, receipt, time.monotonic() + 5, root) == {
        'ok': False, 'reason': 'metered_fetch_disabled'}
    assert not posts


def test_receipt_identity_mismatch_fails_closed(rig):
    root, _ = rig
    receipt = root / 'foreign-receipt.json'
    recovery.authorize_metered_fetch(OTHER_URL, receipt, time.monotonic() + 5)
    with pytest.raises(ValueError, match='provider_request_identity_mismatch'):
        _fetch(URL, receipt, root)


def test_authorize_rejects_unsupported_and_foreign_sources(rig):
    root, _ = rig
    with pytest.raises(ValueError, match='metered_source_not_supported'):
        recovery.authorize_metered_fetch('https://example.com/not-a-video',
                                         root / 'unsupported.json', time.monotonic() + 5)
    with pytest.raises(ValueError, match='metered_source_not_supported'):
        recovery.authorize_metered_fetch('https://www.youtube.com/watch?v=abc123def45',
                                         root / 'youtube.json', time.monotonic() + 5)
