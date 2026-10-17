# Fixed 50-Persona Audit — Round 3 — 2026-09-29 19:16Z

## Scope and immutable inputs

- Repository: `Reese-max/ninax-line-hermes`
- Default branch checked immediately before this report: `main@b71a827f577cd7689f72e80dab75165f0cfa444b`
- Inspected product SHA: `e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6`
- Why product SHA differs from HEAD: `b71a827...` is the Round 2 audit-only commit and `638274...` is the Round 1 audit-only commit; the product tree at `e2c4785...` is unchanged (`git diff` between the inspected product SHA and current default shows only the two audit reports). No product/config/CI change landed on default after the inspected product commit.
- Fixed-persona protocol blob: `6e3499d6ef5be7e123050e1526946f6a40f99263`
- Issue Quality v2 blob: `8167e10798071d2276addaff6b201c6b0e904a2a`
- Runtime lock: Hermes Agent `13e72fb205b735df679e0fd5f5996a34ac4accc6`
- Personas: fixed A01–J05 only. These are 50 synthetic simulations, not 50 people or 50 independent validations.
- Round type: full fixed-50 re-review on the same product SHA because a third complete review was due under tracker issue #5. This is **not** a qualifying CLEAN round because applicable P0/P1/P2 and required runtime gaps remain.

The product purpose remains the deployed NINAX LINE/Hermes video-recovery and audited-summary-delivery path documented in the repository. No product source, workflow, configuration, secret, repository setting, deployment, paid-provider authorization, external LINE message, branch, merge, or repair worker was changed or started by this audit.

## Current coordination / change check

Current `main` is unchanged since Round 2 (the Round 2 report itself is the only new commit). Open implementation ownership remains outside this audit:

- Issue #1 (`messageEdited` / redelivery lifecycle) has open implementation/documentation PRs and branches (`github-1-line-input-lifecycle` → PR #2 at `24be119`, `devin/issue-1-message-edited` → PR #3 at `5b1a809`). Active PR/branch scope remains owner-controlled.
- Issue #4 (positive authorization before metered fetch) has active branches `fix/issue-4-metered-auth-gate` → PR #7 at `0c776ac` and `devin/issue-4` → PR #9 at `3e8041e`. Those unmerged changes are not current-product evidence.
- Issue #8 (LINE Push idempotent recovery via `X-Line-Retry-Key`) now has an open candidate PR #10 from branch `fix/issue-8-push-retry-key` at `e81c5a7`. In Round 2 no implementation PR/branch existed for #8; this round records the new ownership without counting it as landed. The unmerged change is not current-product evidence.
- Issue #5 is this fixed-persona umbrella tracker; this document is its Round 3 record.
- Issue #6 remains a research tracker; it is not evidence of a product P0/P1/P2 by itself.

## Current issue-quality ledger

| Issue | Kind / severity | Current default evidence | Round-3 decision |
|---|---|---|---|
| #1 | BUG / P1 | Default still lacks the proposed revision lifecycle; candidate PRs #2/#3 are unmerged | OPEN; `SKIPPED_LOCKED` for scope mutation due active implementation ownership; no fix credit |
| #4 | BUG / P2 | Default shared metered-fetch path still has no landed positive one-shot authorization implementation; candidate PRs #7/#9 unmerged | OPEN; `SKIPPED_LOCKED` for scope mutation; no fix credit |
| #8 | BUG / P2 | Source fingerprint re-verified on default this round; candidate PR #10 open/unmerged | OPEN; `SKIPPED_LOCKED` for scope mutation; no fix credit |
| #6 | RESEARCH | Research tracker; not evidence of a product P0/P1/P2 by itself | No severity promotion; no new issue |

## Finding re-verification this round

- #1 fingerprint: repo-wide source search on current default finds no `messageEdited` handling anywhere in tracked product or check code (the identifier appears only in the audit reports themselves); no landed revision lifecycle. Still reproducible.
- #4 fingerprint: `video-loop/work/profile/hooks/video_recovery.py::_metered_fetch()` still blocks only on `NINAX_DISABLE_METERED_FETCH=1`; no `authorize_metered_fetch` or equivalent positive grant exists on default. Still reproducible.
- #8 fingerprint: `video-loop/work/profile/plugins/line-platform/__init__.py::_send_messages()` still records `sending` before transport, calls `_client.push()` with no `X-Line-Retry-Key`, converts any exception to durable `delivery=unknown`, and blocks same-turn re-attempt via `video_delivery_already_attempted`. Repo-wide search for `X-Line-Retry-Key` returns no implementation on default. Still reproducible.
- No new independent actionable finding was source-confirmed this round; the inspected product SHA is identical to Round 2, so the fixed matrix is re-reviewed against unchanged product code plus the updated coordination state above.

## Current GitHub Actions execution state

- Current default-branch (`b71a827...`) push run `35311908343`: all three jobs (`offline`, `hermes-contract`, `pipeline-windows`) have `runner_id=0` and `steps=[]` with conclusion `failure`. Same admission gap as Round 2 — still not evidence of an application/test failure.
- Candidate-side runs for PR #10 head `e81c5a7` (`35766717402`, `35766759224`, 2026-09-22) also show all jobs with `runner_id=0` and `steps=[]`. Candidate fixes therefore currently have no hosted execution evidence either; their real status must come from their own receipts, not from green-looking CI.

## Runtime evidence boundary

Executed evidence retained from repository receipts applies only to the exact recorded paths: offline/install/video/delivery checks, native Hermes gateway tests, Windows self-check, localhost-routed model/video flows and production health/webhook/bot-info probes. It explicitly does not establish phone receipt. Current default-branch GitHub Actions provides zero executed steps, so it adds no current-SHA test coverage.

Required before CLEAN remains:

- landed default-branch resolution / traceable owner `not_planned` for applicable P0/P1/P2;
- same-scenario regression verification after any relevant fix actually lands;
- safe real LINE test-channel evidence for core delivery/edited-redelivery behavior, including the relevant mobile/phone outcome boundary;
- valid execution evidence for current/recent product code rather than runner-admission failure alone.

## Fixed A01–J05 matrix — Round 3

Evidence legend: `SRC` = current source/config inspection; `EXEC-HIST` = repository execution receipt from an earlier exact recorded environment; `CI0` = current default-branch Actions run exists but executed zero steps; `EXT` = first-party external contract; `NRV` = needs runtime verification. `#1/#4/#8` refer to the existing Issues. Each row preserves the fixed persona identity and exercises at least one applicable core scenario.

| Persona | Goal / precondition | Input / steps | Expected | Round-3 observation | Evidence | Severity / finding |
|---|---|---|---|---|---|---|
| A01 | Mobile novice gets a trustworthy video answer | Send supported video URL, wait, read result | Clear final answer only after review | Reviewed-delivery boundary exists; real phone receipt still pending | SRC + EXEC-HIST + NRV | no new; phone runtime gap |
| A02 | Office/non-CLI user completes chat-only flow | Send URL and follow-up in LINE | No shell/ops knowledge required | User path remains chat based; operational recovery is maintainer-side | SRC | no new |
| A03 | Technical user can understand/reproduce boundaries | Inspect README, receipts, fixed scripts | Traceable source/evidence separation | SHA-pinned Hermes and receipts remain documented | SRC | no new |
| A04 | Time-pressure user receives completion after long processing | Submit short video; wait through long turn | Final reviewed result without silent loss | Long path evidenced historically; Push ambiguity can strand completion; candidate PR #10 unmerged | SRC + EXEC-HIST + EXT | P2 #8 |
| A05 | Visual learner receives readable text takeaway | Submit video needing visual evidence | Text reflects checked visual/source evidence | Workflow has evidence/review gate; visual accuracy only as recorded | SRC + EXEC-HIST | no new |
| B01 | Spreadsheet/non-programmer uses normal LINE flow | URL -> result -> recall | No developer-only interaction | Normal interaction remains LINE messages | SRC | no new |
| B02 | Junior engineer diagnoses transport errors | Force fake transport failure in isolated fixture | Explicit recoverable vs unknown state | Reply ambiguity covered; Push idempotent retry not covered on default; PR #10 pending | SRC | P2 #8 |
| B03 | UI designer expects reversible user actions | Ask recall/recovery after prior video | Repeat/recover without stale output | Session recall exists; edit lifecycle remains unlanded; PRs #2/#3 pending | SRC | P1 #1 |
| B04 | Research user wants source-backed summary | Video with incomplete source | Gaps surfaced, no fabricated completeness | Partial/missing-source notice path exists | SRC + EXEC-HIST | no new |
| B05 | Fragmented mobile user returns after delay | Long video then follow-up | Context/review stays bound to turn | Session/turn binding exists; phone/runtime continuity still NRV | SRC + NRV | no new |
| C01 | Public-service user needs correctness/audit | Request sensitive/high-stakes summary | Audited payload only | Approval hash/evidence binding present | SRC + EXEC-HIST | no new |
| C02 | Multiuser/teacher avoids cross-chat leakage | Concurrent chats / postback ownership | Output bound to correct chat/turn | Chat/turn binding and postback owner checks present | SRC + EXEC-HIST | no new |
| C03 | High-risk user needs uncertainty disclosure | Missing/partial media | Fail closed or show gap notice | Notice/gap pathway exists | SRC + EXEC-HIST | no new |
| C04 | Creator runs long workflow | >reply-token-duration video | Reviewed result eventually delivered/recoverable | Long flow evidenced; Push timeout/5xx can become terminal unknown; PR #10 unmerged | SRC + EXEC-HIST + EXT | P2 #8 |
| C05 | SRE expects fail-closed recovery | Inject ambiguous Reply and Push failures separately | No duplicate; safely recover where primitive exists | Reply fail-closed is covered; Push safe-retry primitive unused on default | SRC + EXT | P2 #8 |
| D01 | Manager reviews anomalies | Inspect failure receipts/statuses | Distinguish app failure from infra/admission | Default-SHA run 35311908343 and PR-side runs all zero-step; not treated as app failure | CI0 | validation gap only |
| D02 | PM wants clear responsibility | Review open issues/PRs/current branch | Unmerged work not called fixed | #1/#4/#8 PRs remain off default; #8 gained PR #10; report preserves ownership | SRC | no new |
| D03 | IT admin needs controlled deployment/backup | Read install/runtime lock/ops | Rebuild uses pinned dependencies | Runtime lock pins Hermes SHA; deployment not changed | SRC | no new |
| D04 | Procurement/cost owner controls paid actions | Missing media with provider credentials | Paid fetch requires positive grant | Current default still lacks landed grant; PRs #7/#9 unmerged | SRC | P2 #4 |
| D05 | Compliance reviewer needs evidence provenance | Compare audit, runtime and phone claims | No source/test/phone overclaim | Historical receipts explicitly say no real LINE message | SRC + EXEC-HIST | no new |
| E01 | Low-confidence UI user sends first request | Basic LINE URL input | Simple success/failure language | Chat path simple; real-device comprehension not executed | SRC + NRV | no new |
| E02 | Large-text desktop user consumes summary | Use enlarged LINE client | Content remains text and readable | No custom UI dependency; device rendering not executed | SRC + NRV | no new |
| E03 | Low digital confidence recovers from failure | Retry after interruption | Clear safe next action | Unknown Push state currently gives no safe same-turn retry | SRC + EXT | P2 #8 |
| E04 | Non-cloud/office user uses supported channel | Standard LINE client only | No extra cloud dashboard required | User path stays LINE; backend dependencies remain hidden | SRC | no new |
| E05 | Long-use readability | Multiple summaries/follow-ups | Stable text chunks and clear status | LINE chunking/status mechanisms exist in pinned runtime | SRC | no new |
| F01 | 65+ user needs straightforward interaction | URL then wait/read | Few controls; no hidden confirmation needed | Basic chat path applies; real-device test absent | SRC + NRV | no new |
| F02 | Low-vision user | Larger client text / long result | Content not dependent on visual-only cues | Result is text, but actual accessibility rendering untested | SRC + NRV | no new |
| F03 | Poor-motor user minimizes precision gestures | Send message; avoid complex UI | Core result accessible without precise control | Text-message path applies; postback is not required for video result | SRC | no new |
| F04 | Memory-load user resumes context | '再說一次' / '完整一點' | Prior video context can be recalled safely | Session recall exists; edited-source lifecycle remains #1 | SRC | P1 #1 applies to edited input |
| F05 | Assisted setup then solo use | Preconfigured bot; ordinary video requests | No admin steps for each normal request | Normal no-metered path can run; paid fallback authority remains maintainer boundary | SRC | P2 #4 on paid fallback |
| G01 | Keyboard-only user | Desktop LINE keyboard interaction | Core request/result works without custom mouse UI | No custom web UI in core path; end-device verification absent | SRC + NRV | no new |
| G02 | Screen-reader user | Read result/status text | Semantics conveyed in text | Core payload is text; screen-reader behavior not directly tested | SRC + NRV | no new |
| G03 | Color-vision user | Consume status/result | Meaning not color-only | Status delivered as text | SRC | no new |
| G04 | 200% zoom/narrow display | Read chunked result | Text remains consumable | LINE client owns rendering; real narrow-device verification absent | SRC + NRV | no new |
| G05 | Slow/high-latency network | Long video then Push timeout | Safe completion/retry without duplicate | Current Push exception -> unknown -> future attempt blocked; PR #10 unmerged | SRC + EXT | P2 #8 |
| H01 | Windows maintainer | Rebuild/check Windows path | Pinned reproducible checks | Historical Windows self-check exists; current default Actions Windows job executes zero steps | EXEC-HIST + CI0 | validation gap only |
| H02 | macOS maintainer | Inspect/rebuild portable pieces | Document platform constraints | Repo is host/Hermes oriented; no new P2 proven for macOS | SRC | no new |
| H03 | Linux CI noninteractive | Run workflow checks | Tests actually execute | Default-SHA run has no steps/runner; do not call it green/red product test | CI0 | validation gap only |
| H04 | Self-host/deployment maintainer | Recovery with configured provider | Explicit authority before paid fallback | Default lacks positive one-shot gate; PRs #7/#9 unmerged | SRC | P2 #4 |
| H05 | First-time maintainer | Read README/OPERATIONS and recover | Avoid accidental deploy/paid action | Docs expose boundaries; product default still needs #4 fix | SRC | P2 #4 |
| I01 | Double-submit adversary | Redeliver same webhook / duplicate action | Dedupe/stale result cannot duplicate side effects | Proposed input lifecycle is off-default; #1 remains | SRC | P1 #1 |
| I02 | Interrupt/recover adversary | Edit original input while work is running | Superseded result cannot ship | Candidate fixes #2/#3 remain unmerged | SRC | P1 #1 |
| I03 | Bad-input adversary | Unsupported/missing video source | Fail closed with notice | Missing-source notice path exists | SRC + EXEC-HIST | no new |
| I04 | Timeout/429/5xx adversary | Provider/LINE transport transient failures | Bounded safe retry or explicit terminal state | Paid-provider authority tracked in #4; LINE Push timeout recovery gap #8 | SRC + EXT | P2 #4 + #8 |
| I05 | Partial-success retry adversary | Reviewed result ready, transport uncertain | Preserve approved work and recover without duplicate | Push exception preserves unknown receipt but cannot idempotently retry on default | SRC + EXT | P2 #8 |
| J01 | Large-data user | Long/large video summary | Resource limits/gaps explicit | Long workflow exists; no new scale defect proven this round | SRC + EXEC-HIST | no new |
| J02 | Concurrent expert | Overlapping events/edits | Revision ordering prevents stale delivery | Current default lacks landed revision lifecycle | SRC | P1 #1 |
| J03 | Long-run automation expert | >reply-token-duration workflow + transient Push failure | Completion remains recoverable | Push uses no stable retry key; unknown becomes terminal | SRC + EXEC-HIST + EXT | P2 #8 |
| J04 | Security/privacy expert | Verify binding and secret/data boundaries | Wrong chat/payload cannot be sent | Approval/chat/hash checks exist; no new privacy finding | SRC + EXEC-HIST | no new |
| J05 | Automation maintainer | Noninteractive checks + retry/recovery | Idempotent, observable automation | CI execution gap remains on default; Push recovery lacks supported idempotency primitive | SRC + CI0 + EXT | P2 #8 + validation gap |

## Ten-dimension coverage summary

- First understanding/success: A01/A02/E01/F01/H05.
- Core task: A04/B04/C04/J01.
- Error recovery: B02/E03/I02/I04/I05.
- Data/security: C01/C02/D05/J04.
- Observability: D01/D02/H03/J05.
- Accessibility/device: E02/F01–F05/G01–G05.
- Performance/cost: D04/H04/J01/J03.
- Maintainability: A03/D03/H01–H05.
- Failure injection: I01–I05, C05, G05.
- Trust: B04/C01/C03/D05/J04.

## CLEAN decision

**NOT CLEAN — streak 0/2.** This round cannot qualify for CLEAN because:

- applicable P1 #1 remains open on current default with only unmerged candidates;
- applicable P2 #4 and #8 remain open on current default with only unmerged candidates (#7/#9 and #10);
- required mobile/real-phone acceptance evidence remains missing;
- current default-branch GitHub Actions run executed zero steps, so no hosted current-SHA test evidence exists.

The 50/50 synthetic matrix is complete for this audit round, but completing a persona simulation is not itself a CLEAN-qualified round while blockers/runtime evidence remain. The streak stays at 0/2 and must not be incremented until all applicable P0/P1/P2 are closed or traceably `not_planned`, required runtime paths have current/recent evidence, and two independent complete qualifying rounds are obtained.

## Next verification

- Re-review after any of PR #2/#3, #7/#9, or #10 lands on default, or is withdrawn/`not_planned` by the owner; each landing requires same-scenario regression evidence before fix credit.
- Obtain at least one GitHub Actions run for the current product SHA with executed steps, or an equivalent hosted/noninteractive receipt, before treating CI state as anything other than a validation gap.
- Obtain safe real LINE test-channel evidence for delivery/edited-redelivery before any CLEAN-qualified round.
