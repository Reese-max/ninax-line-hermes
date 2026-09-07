"""Isolated lifecycle fixtures: duplicates, edits, disorder, restart and cost receipts."""
import json
import hashlib
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent/'work/profile/plugins/line-platform'))
from line_input_lifecycle import InputLifecycle


def event(event_id, timestamp, text, *, kind="message", redelivery=False):
    return {"type": kind, "webhookEventId": event_id, "timestamp": timestamp,
            "deliveryContext": {"isRedelivery": redelivery},
            "message": {"id": "m-1", "type": "text", "text": text}}


with tempfile.TemporaryDirectory(prefix="ninax-line-input-") as directory:
    root = Path(directory)
    store = InputLifecycle(root)
    original = store.accept(event("evt-1", 1000, "old"), "C-room")
    assert original["accepted"] and original["input_revision"] == 1
    assert store.begin_job(original)
    assert not store.begin_job(original), "one revision must start expensive work once"

    duplicate = store.accept(event("evt-1", 1000, "old", redelivery=True), "C-room")
    assert duplicate["disposition"] == "DUPLICATE_EVENT" and not duplicate["accepted"]

    edited = store.accept(event("evt-2", 2000, "new", kind="messageEdited"), "C-room")
    assert edited["accepted"] and edited["input_revision"] == 2
    assert not store.is_current(original) and store.is_current(edited)
    assert store.begin_job(edited)

    stale = store.accept(event("evt-late", 1500, "old arrives late", redelivery=True), "C-room")
    assert stale["disposition"] == "STALE_EVENT" and not stale["accepted"]

    same = store.accept(event("evt-same", 3000, "new", kind="messageEdited"), "C-room")
    assert same["disposition"] == "DUPLICATE_CONTENT" and not same["accepted"]
    conflict = store.accept(event("evt-conflict", 3000, "ambiguous", kind="messageEdited"), "C-room")
    assert conflict["disposition"] == "TIMESTAMP_CONFLICT" and not conflict["accepted"]

    restarted = InputLifecycle(root)
    repeat_after_restart = restarted.accept(event("evt-2", 2000, "new", kind="messageEdited"), "C-room")
    assert repeat_after_restart["disposition"] == "DUPLICATE_EVENT"
    assert restarted.is_current(edited)
    restarted.finish_job(edited, "COMPLETED")
    restarted.record_delivery(edited, "DELIVERED", "a" * 64)

    first_job = json.loads(next((root / "jobs").glob("*.1.json")).read_text())
    second_job = json.loads(next((root / "jobs").glob("*.2.json")).read_text())
    assert first_job["status"] == "SUPERSEDED" and first_job["superseded_by_revision"] == 2
    assert second_job["status"] == "COMPLETED"
    assert second_job["delivery"]["status"] == "DELIVERED"
    assert second_job["binding"]["input_sha256"] == edited["input_sha256"]

    crash_event = event("evt-crash", 4000, "recover after receipt", kind="messageEdited")
    crash_accept = restarted.accept(crash_event, "C-other")
    assert crash_accept["accepted"]
    recovered = InputLifecycle(root).accept(crash_event, "C-other")
    assert recovered["disposition"] == "RESUME_ACCEPTED" and recovered["accepted"]
    assert InputLifecycle(root).begin_job(recovered)

    # Simulate a crash after the current state is durable but before its event
    # receipt is committed. Redelivery must resume the same revision exactly.
    torn_event = event("evt-torn", 5000, "state committed first", kind="messageEdited")
    torn = restarted.accept(torn_event, "C-torn")
    receipt = root / "events" / f"{hashlib.sha256(b'evt-torn').hexdigest()}.json"
    receipt.unlink()
    torn_recovered = InputLifecycle(root).accept(torn_event, "C-torn")
    assert torn_recovered["disposition"] == "RESUME_ACCEPTED"
    assert torn_recovered["accepted"] and torn_recovered["input_revision"] == torn["input_revision"]
    assert InputLifecycle(root).begin_job(torn_recovered)

    malformed = dict(torn_recovered)
    malformed["input_revision"] = True
    try:
        InputLifecycle(root).begin_job(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("boolean input revision must fail closed")

print(json.dumps({"gate": "line-input-lifecycle", "status": "PASS", "checks": 21}))
