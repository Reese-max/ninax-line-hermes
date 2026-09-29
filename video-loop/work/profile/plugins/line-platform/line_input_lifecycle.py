"""Durable, fail-closed LINE input revision and delivery bindings.

The LINE platform can redeliver the same webhook and can deliver events out of
order.  This store makes those facts explicit before any expensive work starts.
It intentionally stores hashes and identifiers, never message bodies.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Dict


SCHEMA_VERSION = 1
TERMINAL_JOB_STATES = frozenset({"COMPLETED", "FAILED", "INTERRUPTED", "SUPERSEDED"})


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
            existing_event = _strict_read(event_path)
            if existing_event:
                old_binding = existing_event.get("binding")
                if not isinstance(old_binding, dict):
                    raise ValueError("invalid LINE event receipt")
                old_value = self._binding_fields(old_binding)
                job = _strict_read(self._job_path(old_value["input_id"], old_value["input_revision"]))
                resume = (existing_event.get("disposition") == "ACCEPTED"
                          and self.is_current(old_value) and not job)
                return {**old_binding, "disposition": "RESUME_ACCEPTED" if resume else "DUPLICATE_EVENT",
                        "accepted": resume}

            state = _strict_read(state_path)
            revisions = list(state.get("revisions", []))
            disposition = "ACCEPTED"
            supersedes = None
            if not state:
                revision = 1
            else:
                current_timestamp = state.get("current_event_timestamp_ms")
                current_hash = state.get("current_content_sha256")
                revision = state.get("current_revision")
                if (isinstance(current_timestamp, bool) or not isinstance(current_timestamp, int)
                        or not isinstance(current_hash, str) or isinstance(revision, bool)
                        or not isinstance(revision, int)):
                    raise ValueError("invalid LINE lifecycle state")
                if timestamp < current_timestamp:
                    disposition = "STALE_EVENT"
                elif timestamp == current_timestamp and content_sha256 != current_hash:
                    disposition = "TIMESTAMP_CONFLICT"
                elif content_sha256 == current_hash:
                    # The state commit intentionally precedes the event receipt.
                    # If the process died between those writes, the same event
                    # must resume this revision instead of being lost forever.
                    job = _strict_read(self._job_path(identity, revision))
                    resume = (state.get("current_webhook_event_id") == event_id and not job)
                    disposition = "RESUME_ACCEPTED" if resume else "DUPLICATE_CONTENT"
                    # Preserve the greatest observed occurrence time so a later
                    # out-of-order event cannot roll the current version back.
                    if timestamp > current_timestamp and not resume:
                        state["current_event_timestamp_ms"] = timestamp
                        state["current_webhook_event_id"] = event_id
                        _atomic_json(state_path, state)
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
        job = _strict_read(path)
        if job and job.get("status") not in TERMINAL_JOB_STATES:
            _atomic_json(path, {**job, "status": "SUPERSEDED", "superseded_by_revision": newer_revision,
                                "finished_at": time.time()})

    @staticmethod
    def _binding_fields(binding: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("schema_version", "input_id", "chat_id", "message_id", "input_revision", "input_sha256",
                "event_timestamp_ms", "webhook_event_id")
        value = {key: binding.get(key) for key in keys}
        if (value["schema_version"] != SCHEMA_VERSION or not isinstance(value["input_id"], str)
                or isinstance(value["input_revision"], bool)
                or not isinstance(value["input_revision"], int) or not isinstance(value["input_sha256"], str)):
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
            if not job or job.get("status") == "SUPERSEDED":
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
