# NINAX video delivery

This profile overrides the installed `line-platform` plugin through Hermes' normal plugin discovery. It inherits the native LINE adapter, webhook signature verification, authorization, queueing and ordinary conversation behavior.

The generic platform extension is `run_custom_turn(ctx) -> dict | None` on the adapter class. `TurnRunner.run_sync` calls it after gateway authorization and before creating any agent stream. `None` preserves the ordinary agent route; exceptions propagate without invoking another agent. A handled result carries the normal gateway result fields, including `messages`, `history_offset`, `session_id`, `agent_persisted`, and delivery flags. The platform must own its deadline and cancellation. NINAX uses this to run a bounded video workflow with the installed auxiliary LLM client and no agent tools.

Video work reuses the installed video pipeline, media and per-image caches. `evidence.json` stores source evidence and per-turn checklist/audit/approval records. A separate provider receipt records an accepted billable task immediately so a timeout cannot trigger a duplicate request. Draft streaming and the native slow-answer cache are disabled only for these video turns. The send gate compares the actual native LINE message objects, immutable turn identity, evidence revision and file stamps before sending or recording delivery. A send with uncertain server acceptance is not retried.

Limits per turn: 300 seconds including delivery, up to 3 searches and 3 candidate URLs, one extra single-video metered fetch, one summary revision, two audits. Title similarity does not establish video identity. Unverified related sources never become facts about the requested clip. An unavailable audit produces an explicit limitation notice, never an unaudited new summary.

Validation: `check_video.py` covers evidence, identity, cache, concurrency, audit coverage and child-process cleanup. `check_line_video.py` loads the actual modified TurnRunner and profile plugin in a temporary Hermes home, calls the real configured provider, redirects only LINE transport to localhost, and checks final payload/persistence/bypass behavior. Real phone acceptance remains a separate check.


## Edited messages and webhook redelivery

Before the native Hermes queue starts any work, the profile writes a durable receipt keyed by
`webhookEventId` and a versioned message record keyed by chat plus `message.id`. Exact canonical
message content is SHA-256 bound to its input revision; message bodies are not retained in these
receipts. A duplicate event or duplicate content never starts another job. An older `timestamp`,
or different content with the same timestamp, fails closed.

LINE `messageEdited` text events use the same path as messages but advance the revision. Advancing
a revision marks the prior job `SUPERSEDED`; every outbound payload checks that its input binding is
still current. Cost and delivery receipts share `input_id:revision`, so a delivered result can be
traced to the exact accepted content hash. These receipts live under
`$HERMES_HOME/line-input-lifecycle`.

Validation: `check_input_lifecycle.py` covers duplicate/redelivered events, edited content,
out-of-order arrival, equal-timestamp conflicts, restart recovery, one-start cost attribution and
delivery binding without contacting LINE or a model provider.

