"""Durable, fail-closed LINE input revision and delivery bindings.

The LINE platform can redeliver the same webhook and can deliver events out of
order.  This store makes those facts explicit before any expensive work starts.
It intentionally stores hashes and identifiers, never message bodies.

One adapter process owns each store root: mutual exclusion is per-process, so
two gateways sharing a profile home would interleave their read/write cycles.
Receipts are append-only by design — they are the audit record, so retention
is a deployment policy, not something the store decides.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Dict


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1
TERMINAL_JOB_STATES = frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "SUPERSEDED"})

# LINE gives no total order for two events carrying the same millisecond, so the
# head must be resolved by a key that does not depend on arrival order.
_EVENT_RANK = {"message": 0, "messageEdited": 1}


def _outranks(event_type: str, content_sha256: str, current_event_type: Any, current_hash: Any) -> bool:
    """Deterministic, arrival-independent precedence for equal-timestamp events.

    A state file written before this field existed has no recorded type, so its
    head ranks below every known event type and the content hash decides.
    """
    if not isinstance(current_hash, str):
        return True
    return ((_EVENT_RANK.get(event_type, -1), content_sha256)
            > (_EVENT_RANK.get(current_event_type, -1) if isinstance(current_event_type, str) else -1,
               current_hash))


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _strict_read(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid lifecycle document: {path.name}")
    return value


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    # Fsync the directory too: without it a power loss can drop the rename while
    # an earlier receipt's rename survives, so a committed event outlives the
    # message state it depends on and the identity rebuilds from partial data.
    try:
        directory = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _quarantine(path: Path) -> None:
    """Move an unreadable receipt aside instead of letting it wedge or crash
    the pipeline. The bytes stay on disk for audit."""
    try:
        path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}"))
    except OSError:
        pass


def _message_content(message: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    message_id = message.get("id")
    message_type = message.get("type")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("LINE message.id must be a non-empty string")
    if not isinstance(message_type, str) or not message_type:
        raise ValueError("LINE message.type must be a non-empty string")
    if event_type == "messageEdited" and message_type != "text":
        raise ValueError("LINE messageEdited currently supports text messages only")
    if message_type == "text" and not isinstance(message.get("text"), str):
        raise ValueError("LINE text message must contain string text")
    # Message IDs establish identity, while access/quote tokens are transport
    # metadata that may rotate independently. Preserve text and all semantic
    # fields (including mentions, emoji descriptors and quoted-message context).
    return {k: v for k, v in message.items() if k not in {"id", "markAsReadToken", "quoteToken"}}


class InputLifecycle:
    """Persist input decisions and cost/delivery receipts for one adapter process."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._reconcile_restarted_jobs()

    def _reconcile_restarted_jobs(self) -> None:
        """A ``STARTED`` job surviving process restart is dead work: mark it
        ``INTERRUPTED`` so receipts stay honest. It is never restarted — the
        event receipt already dedupes redelivery, and re-running could repeat
        external side effects that cannot be proven un-executed."""
        for path in sorted((self.root / "jobs").glob("*.json")):
            try:
                job = _strict_read(path)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.warning("LINE: skipping unreadable lifecycle job receipt %s", path.name)
                continue
            if job.get("status") == "STARTED":
                _atomic_json(path, {**job, "status": "INTERRUPTED",
                                    "interrupted_reason": "process_restart",
                                    "finished_at": time.time()})

    @staticmethod
    def _identity(chat_id: str, message_id: str) -> str:
        return hashlib.sha256(f"{chat_id}\0{message_id}".encode()).hexdigest()

    def _state_path(self, identity: str) -> Path:
        return self.root / "messages" / f"{identity}.json"

    def _event_path(self, event_id: str) -> Path:
        return self.root / "events" / f"{hashlib.sha256(event_id.encode()).hexdigest()}.json"

    def _job_path(self, identity: str, revision: int) -> Path:
        return self.root / "jobs" / f"{identity}.{revision}.json"

    def accept(self, event: Dict[str, Any], chat_id: str) -> Dict[str, Any]:
        """Return a durable decision. Only ``ACCEPTED`` is eligible for work."""
        if not isinstance(event, dict) or event.get("type") not in {"message", "messageEdited"}:
            raise ValueError("unsupported LINE input event")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("LINE input requires a chat identity")
        event_id = event.get("webhookEventId")
        timestamp = event.get("timestamp")
        delivery = event.get("deliveryContext", {})
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("LINE webhookEventId must be a non-empty string")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("LINE timestamp must be a non-negative integer")
        if not isinstance(delivery, dict) or not isinstance(delivery.get("isRedelivery", False), bool):
            raise ValueError("LINE deliveryContext.isRedelivery must be boolean")
        message = event.get("message")
        if not isinstance(message, dict):
            raise ValueError("LINE input event requires a message object")
        content = _message_content(message, event["type"])
        message_id = message["id"]
        content_sha256 = _digest(content)
        identity = self._identity(chat_id, message_id)
        event_path = self._event_path(event_id)
        state_path = self._state_path(identity)

        with self._lock:
            try:
                existing_event = _strict_read(event_path)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.error("LINE: quarantining unreadable event receipt %s", event_path.name)
                _quarantine(event_path)
                existing_event = {}
            if existing_event:
                try:
                    old_value = self._binding_fields(existing_event.get("binding"))
                except (TypeError, ValueError):
                    logger.error("LINE: quarantining malformed event receipt %s", event_path.name)
                    _quarantine(event_path)
                    existing_event = {}
            if existing_event:
                job = self._read_job(self._job_path(old_value["input_id"], old_value["input_revision"]))
                # The recorded disposition is history, not the resume gate: any
                # receipt whose binding still resolves to the current head may
                # restart work while its job receipt is absent — including one
                # torn between the resume decision and begin_job.
                resume = self.is_current(old_value) and not job
                return {**old_value, "disposition": "RESUME_ACCEPTED" if resume else "DUPLICATE_EVENT",
                        "accepted": resume}

            state = self._read_state(state_path, identity)
            revisions = list(state.get("revisions", []))
            disposition = "ACCEPTED"
            supersedes = None
            if not state:
                revision = 1
            else:
                current_timestamp = state.get("current_event_timestamp_ms")
                current_hash = state.get("current_content_sha256")
                revision = state.get("current_revision")
                if content_sha256 == current_hash:
                    # The state commit intentionally precedes the event receipt.
                    # If the process died between those writes, a later event
                    # must resume this revision instead of being lost forever —
                    # regardless of which event id owns the bookkeeping, so a
                    # duplicate's timestamp takeover cannot strand the work.
                    job = self._read_job(self._job_path(identity, revision))
                    resume = not job
                    disposition = "RESUME_ACCEPTED" if resume else "DUPLICATE_CONTENT"
                    # Preserve the greatest observed occurrence time so a later
                    # out-of-order event cannot roll the current version back.
                    if timestamp > current_timestamp:
                        state["current_event_timestamp_ms"] = timestamp
                        state["current_webhook_event_id"] = event_id
                        state["current_event_type"] = event["type"]
                        _atomic_json(state_path, state)
                elif timestamp < current_timestamp:
                    disposition = "STALE_EVENT"
                elif timestamp == current_timestamp and not _outranks(event["type"], content_sha256,
                                                                     state.get("current_event_type"),
                                                                     current_hash):
                    # Equal timestamps are indistinguishable by the platform
                    # contract, so arrival order must not decide the head: a
                    # late event would otherwise roll the current revision back
                    # to older content. Order them by a deterministic,
                    # arrival-independent key (edit events outrank plain
                    # messages, then the content hash), which every replica and
                    # every replay resolves identically.
                    disposition = "STALE_EVENT"
                else:
                    supersedes = revision
                    revisions[-1] = {**revisions[-1], "status": "SUPERSEDED", "superseded_at": time.time()}
                    revision += 1

            binding = {
                "schema_version": SCHEMA_VERSION,
                "input_id": identity,
                "chat_id": chat_id,
                "message_id": message_id,
                "input_revision": revision,
                "input_sha256": content_sha256,
                "event_timestamp_ms": timestamp,
                "webhook_event_id": event_id,
            }
            accepted = disposition in {"ACCEPTED", "RESUME_ACCEPTED"}
            if accepted:
                if disposition == "ACCEPTED":
                    revisions.append({
                        "revision": revision,
                        "content_sha256": content_sha256,
                        "event_timestamp_ms": timestamp,
                        "webhook_event_id": event_id,
                        "event_type": event["type"],
                        "status": "CURRENT",
                    })
                    state = {
                        "schema_version": SCHEMA_VERSION,
                        "input_id": identity,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "current_revision": revision,
                        "current_content_sha256": content_sha256,
                        "current_event_timestamp_ms": timestamp,
                        "current_event_type": event["type"],
                        "current_webhook_event_id": event_id,
                        "revisions": revisions,
                    }
                    _atomic_json(state_path, state)
                    if supersedes is not None:
                        self._supersede_job(identity, supersedes, revision)

            receipt = {
                "schema_version": SCHEMA_VERSION,
                "recorded_at": time.time(),
                "event_type": event["type"],
                "is_redelivery": delivery.get("isRedelivery", False),
                "disposition": disposition,
                "eligible_for_expensive_work": accepted,
                "supersedes_revision": supersedes,
                "binding": binding,
            }
            _atomic_json(event_path, receipt)
            return {**binding, "disposition": disposition, "accepted": accepted}

    def _supersede_job(self, identity: str, revision: int, newer_revision: int) -> None:
        path = self._job_path(identity, revision)
        job = self._read_job(path)
        if job and job.get("status") not in TERMINAL_JOB_STATES | {"UNREADABLE"}:
            _atomic_json(path, {**job, "status": "SUPERSEDED", "superseded_by_revision": newer_revision,
                                "finished_at": time.time()})

    def _read_state(self, path: Path, identity: str) -> Dict[str, Any]:
        """Load the revision head; heal unreadable or absent v1 state from receipts.

        A file written by a newer schema still raises — an old binary must not
        rewrite state it cannot interpret. Unreadable, invalid or missing v1
        state is quarantined when present and the head rebuilt from the
        identity's surviving job and event receipts, so a lost file neither
        wedges the message forever nor lets a stale event pose as the latest
        input. A first-ever message has no receipts and stays revision 1.
        """
        if not path.exists():
            # A brand-new identity has no job receipt and needs no recovery scan;
            # only a lost file over surviving work has to rebuild.
            if not list((self.root / "jobs").glob(f"{identity}.*.json")):
                return {}
            logger.error("LINE: rebuilding missing lifecycle state %s from receipts", path.name)
            return self._rebuild_state(identity, path)
        try:
            state = _strict_read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.error("LINE: quarantining unreadable lifecycle state %s", path.name)
            _quarantine(path)
            return self._rebuild_state(identity, path)
        if state and state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported LINE lifecycle schema")
        if state and not self._state_is_valid(state):
            logger.error("LINE: quarantining invalid lifecycle state %s", path.name)
            _quarantine(path)
            return self._rebuild_state(identity, path)
        return state

    @staticmethod
    def _state_is_valid(state: Dict[str, Any]) -> bool:
        current_timestamp = state.get("current_event_timestamp_ms")
        revision = state.get("current_revision")
        revisions = state.get("revisions")
        return (isinstance(current_timestamp, int) and not isinstance(current_timestamp, bool)
                and isinstance(state.get("current_content_sha256"), str)
                and isinstance(revision, int) and not isinstance(revision, bool)
                and isinstance(revisions, list) and bool(revisions)
                and isinstance(revisions[-1], dict)
                and revisions[-1].get("revision") == revision)

    def _rebuild_state(self, identity: str, state_path: Path) -> Dict[str, Any]:
        """Reconstruct the head from surviving receipts after state loss.

        Job receipts and event receipts both carry the full input binding, so
        the newest accepted revision stays the head: older content remains
        stale instead of reviving, and new input supersedes normally.
        """
        head = None
        candidates = sorted((self.root / "jobs").glob(f"{identity}.*.json"))
        candidates += sorted((self.root / "events").glob("*.json"))
        for path in candidates:
            try:
                receipt = _strict_read(path)
                value = self._binding_fields(receipt.get("binding"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if value["input_id"] != identity:
                continue
            if head is None or value["input_revision"] > head["current_revision"]:
                head = {
                    "schema_version": SCHEMA_VERSION,
                    "input_id": identity,
                    "chat_id": value["chat_id"],
                    "message_id": value["message_id"],
                    "current_revision": value["input_revision"],
                    "current_content_sha256": value["input_sha256"],
                    "current_event_timestamp_ms": value["event_timestamp_ms"],
                    "current_event_type": (receipt.get("event_type")
                                           if receipt.get("event_type") in _EVENT_RANK else "recovered"),
                    "current_webhook_event_id": value["webhook_event_id"],
                    "revisions": [{"revision": value["input_revision"],
                                   "content_sha256": value["input_sha256"],
                                   "event_timestamp_ms": value["event_timestamp_ms"],
                                   "webhook_event_id": value["webhook_event_id"],
                                   "event_type": (receipt.get("event_type")
                                                  if receipt.get("event_type") in _EVENT_RANK else "recovered"),
                                   "status": "CURRENT"}],
                    "recovered_from_receipts": True,
                }
        if head is not None:
            _atomic_json(state_path, head)
        return head or {}

    def _read_job(self, path: Path) -> Dict[str, Any]:
        """Tolerant job read for admission. A corrupt receipt is kept and
        treated as existing-but-unknown: re-running work it may already have
        paid for is the worse failure."""
        try:
            return _strict_read(path)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.error("LINE: unreadable job receipt %s treated as present", path.name)
            return {"schema_version": SCHEMA_VERSION, "status": "UNREADABLE"}

    @staticmethod
    def _binding_fields(binding: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(binding, dict):
            raise ValueError("invalid LINE input binding")
        keys = ("schema_version", "input_id", "chat_id", "message_id", "input_revision", "input_sha256",
                "event_timestamp_ms", "webhook_event_id")
        value = {key: binding.get(key) for key in keys}
        if (value["schema_version"] != SCHEMA_VERSION
                or not isinstance(value["input_id"], str) or not value["input_id"]
                or not isinstance(value["chat_id"], str) or not value["chat_id"]
                or not isinstance(value["message_id"], str) or not value["message_id"]
                or not isinstance(value["webhook_event_id"], str) or not value["webhook_event_id"]
                or isinstance(value["input_revision"], bool)
                or not isinstance(value["input_revision"], int)
                or not isinstance(value["input_sha256"], str) or not value["input_sha256"]
                or isinstance(value["event_timestamp_ms"], bool)
                or not isinstance(value["event_timestamp_ms"], int)):
            raise ValueError("invalid LINE input binding")
        return value

    def is_current(self, binding: Dict[str, Any]) -> bool:
        try:
            value = self._binding_fields(binding)
            state = _strict_read(self._state_path(value["input_id"]))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return bool(state and state.get("chat_id") == value["chat_id"]
                    and state.get("message_id") == value["message_id"]
                    and state.get("current_revision") == value["input_revision"]
                    and state.get("current_content_sha256") == value["input_sha256"])

    def begin_job(self, binding: Dict[str, Any]) -> bool:
        """Write the cost-attribution receipt before work; never start twice."""
        value = self._binding_fields(binding)
        path = self._job_path(value["input_id"], value["input_revision"])
        with self._lock:
            if not self.is_current(value) or _strict_read(path):
                return False
            _atomic_json(path, {"schema_version": SCHEMA_VERSION, "binding": value, "status": "STARTED",
                                "cost_attribution_key": f"{value['input_id']}:{value['input_revision']}",
                                "started_at": time.time(), "delivery": {"status": "NOT_ATTEMPTED"}})
            return True

    def finish_job(self, binding: Dict[str, Any], status: str) -> None:
        if status not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise ValueError("invalid LINE job terminal state")
        value = self._binding_fields(binding)
        path = self._job_path(value["input_id"], value["input_revision"])
        with self._lock:
            job = _strict_read(path)
            if not job or job.get("status") != "STARTED":
                return
            _atomic_json(path, {**job, "status": status, "finished_at": time.time()})

    def record_delivery(self, binding: Dict[str, Any], status: str, payload_sha256: str) -> None:
        if status not in {"DELIVERED", "UNKNOWN", "REJECTED_STALE"} or not isinstance(payload_sha256, str):
            raise ValueError("invalid LINE delivery receipt")
        value = self._binding_fields(binding)
        path = self._job_path(value["input_id"], value["input_revision"])
        with self._lock:
            job = _strict_read(path)
            if not job:
                raise ValueError("LINE delivery has no job receipt")
            attempt = {"status": status, "payload_sha256": payload_sha256, "recorded_at": time.time()}
            attempts = job.get("delivery_attempts")
            if attempts is None:
                attempts = []
            elif not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                raise ValueError("invalid LINE delivery attempt history")
            else:
                attempts = list(attempts)
            previous = job.get("delivery")
            if (isinstance(previous, dict) and previous.get("status") != "NOT_ATTEMPTED"
                    and previous not in attempts):
                attempts.append(previous)
            attempts.append(attempt)
            # Keep the first successful delivery as the durable primary receipt.
            # Later stale/ambiguous attempts remain visible in append-only history.
            delivery = previous if isinstance(previous, dict) and previous.get("status") == "DELIVERED" else attempt
            _atomic_json(path, {**job, "delivery": delivery, "delivery_attempts": attempts})
