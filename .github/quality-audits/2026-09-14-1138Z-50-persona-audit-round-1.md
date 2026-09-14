# NINAX LINE Hermes — Fixed 50-Persona Audit Round 1

- Run: `2026-09-14T11:38Z-persona-audit-11`
- Method: fixed A01–J05 synthetic persona simulation; **not human user research**.
- Protocol: `Reese-max/autodev-ng/docs/portfolio-audit/2026-09-06-50-persona-audit.md`
- Protocol blob: `6e3499d6ef5be7e123050e1526946f6a40f99263`
- Issue Quality v2 blob: `8167e10798071d2276addaff6b201c6b0e904a2a`
- Default branch: `main`
- Inspected/default product SHA: `e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6`
- Owner-approved/current product scope used: current `README.md`, `video-loop/GOAL.md`, `video-loop/OPERATIONS.md`, and existing Issue/PR tracking. Central portfolio material describes this repository as a LINE AI gateway / ordered-messaging and video-summary workflow; no broader product surface is assumed.
- Round status: **NOT CLEAN — 0/2**.

## Evidence boundary

This round separates source evidence from runtime evidence.

### Source-confirmed current default branch

1. `video-loop/work/profile/hooks/video_takeaway_cascade.py` can invoke `single_metered_fetch` when recovery still lacks full media.
2. `video-loop/work/profile/hooks/video_recovery.py::metered_fetch()` blocks only when `NINAX_DISABLE_METERED_FETCH=1`; otherwise `_metered_fetch()` can submit a Bright Data or Apify task when credentials exist. No positive request-scoped authorization is required at that shared boundary.
3. `video-loop/OPERATIONS.md` explicitly says metered acquisition needs explicit single-request authorization and one durable state receipt.
4. Current default branch still has no `messageEdited` implementation; existing Issue #1 remains open. PR #2 and PR #3 are unmerged and therefore are not current-product evidence.

Commit-fixed evidence links:
- https://github.com/Reese-max/ninax-line-hermes/blob/e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6/video-loop/work/profile/hooks/video_takeaway_cascade.py
- https://github.com/Reese-max/ninax-line-hermes/blob/e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6/video-loop/work/profile/hooks/video_recovery.py
- https://github.com/Reese-max/ninax-line-hermes/blob/e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6/video-loop/OPERATIONS.md
- https://github.com/Reese-max/ninax-line-hermes/blob/e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6/video-loop/evidence/goal-verification.json

### Actual execution evidence that exists

`video-loop/evidence/goal-verification.json` records actual execution for specific historical/current-package paths, including `check_goal.py`, `check_install.py`, `check_video.py`, `check_delivery.py`, 46 native Hermes gateway tests, a Windows pipeline self-check, localhost-routed real-model short/long video flows, and production health/webhook/bot-info checks. These receipts do **not** prove a real phone delivery: `real_line_message_sent=false` and `phone_acceptance=pending` are explicitly recorded.

The same evidence records a historical isolated-test incident: two distinct Bright Data tasks reached `completed` while `authorization_at_submission=not_obtained`; actual cost is unknown and further metered tests were stopped. This is executed evidence of the authorization failure mode in the isolated test environment, **not evidence of a new current-production charge**.

Current default-branch GitHub Actions run `34169961169` is a push run for `e2c4785...`, but all three jobs (`offline`, `hermes-contract`, `pipeline-windows`) have `runner_id=0` and `steps=[]`. It is therefore a CI/admission validation gap, not an application/test failure. No new repo-specific P0/P1/P2 is created from the zero-step result.

## Findings / tracking

| ID | Kind | Severity | Confidence | Current status | Tracker |
|---|---|---:|---|---|---|
| NINAX-R1-01 | Existing reliability/platform BUG | P1 | SOURCE_CONFIRMED on default; runtime fix not verified | `STILL_REPRODUCIBLE` on current main because `messageEdited` lifecycle is absent. Active PR #2/#3 exist, so Issue scope is `SKIPPED_LOCKED`; no audit rewrite. | https://github.com/Reese-max/ninax-line-hermes/issues/1 |
| NINAX-R1-02 | BUG | **P2** | SOURCE_CONFIRMED + historical isolated EXECUTED_REPRODUCTION | **NEW**: metered recovery lacks positive one-shot authorization at the shared boundary. | https://github.com/Reese-max/ninax-line-hermes/issues/4 |
| NINAX-R1-V1 | VALIDATION_GAP | not independently severity-escalated | EXECUTED CI metadata | Current Actions run never acquired runners; no steps executed. Shared/root cause unknown. | report only |
| NINAX-R1-V2 | VALIDATION_GAP | CLEAN blocker | EXECUTED evidence explicitly incomplete | Real phone receipt / mobile acceptance remains pending. | report only |

### NINAX-R1-02 fingerprint

`Reese-max/ninax-line-hermes + video recovery metered fallback + media still missing while provider credentials exist and NINAX_DISABLE_METERED_FETCH is unset + provider POST can start without a positive request-scoped authorization + shared metered_fetch only has a negative kill-switch`

Issue Quality v2 calibration: this is P2 rather than P1 because a real authorization-boundary violation is confirmed and a prior isolated test executed two unauthorized remote tasks, but there is no evidence of material monetary loss, production-wide outage, or uncontrolled request volume. The minimal fix is a fail-closed positive per-request authorization check that reuses the existing state receipt; no new database/ledger/framework is required.

## Fixed 50 persona × scenario matrix

All rows below are synthetic simulation against the same current product evidence. `SOURCE` means current default source/contract inspection. `EXECUTED` means an existing receipt records actual execution for the named path. `NEEDS_RUNTIME` means the path was not actually exercised by evidence applicable to the scenario.

| Persona | Goal | Preconditions / input | Steps | Expected | Observed | Evidence | Severity / tracker |
|---|---|---|---|---|---|---|---|
| A01 | First mobile video summary | LINE user sends supported video | Send URL → wait → read summary | Clear progress and usable phone result | Product path documented; real phone receipt still pending | SOURCE + NEEDS_RUNTIME | Validation gap |
| A02 | Use without CLI knowledge | Normal LINE chat/video | Follow only chat interaction | No host/CLI knowledge required | Primary surface is LINE; setup/runtime claims not phone-validated | SOURCE | none new |
| A03 | Inspect/customize technical path | Maintainer clone | Read README/operations/checks | Traceable install/test path | Rebuild/check tooling is documented and receipts exist | SOURCE + EXECUTED | none new |
| A04 | Fast core answer | Short supported video | Send once, avoid manual recovery | Core task completes within bounded flow | Historical localhost real-model short flow passed; phone delivery unverified | EXECUTED + NEEDS_RUNTIME | Validation gap |
| A05 | Understand progress/status | Long video | Send → continuation/review | Distinguish ready/partial/continue | Durable progress/review evidence exists; client presentation not phone-validated | SOURCE + EXECUTED | none new |
| B01 | Nontechnical office use | LINE available | Send URL/question | No deployment concepts exposed | Chat surface fits; no direct phone acceptance receipt | SOURCE + NEEDS_RUNTIME | Validation gap |
| B02 | Diagnose setup failure | Fresh environment | Bootstrap/doctor/check | Actionable failure and rollback | install/rollback checks recorded PASS | EXECUTED | none new |
| B03 | Avoid irreversible user action | Repeat/correct a request | Re-send/change message | Safe correction/reversal semantics | Current main lacks messageEdited revision lifecycle | SOURCE | P1 #1 |
| B04 | Trace source/export evidence | Video summary | Inspect source/evidence mapping | Claims trace to source evidence | Evidence/reviewer artifacts are explicit; phone output not revalidated | SOURCE + EXECUTED | none new |
| B05 | Fragmented mobile use | User leaves/reopens LINE | Start video → return later | State survives and result maps to current request | Durable turn/job design exists; edit/revision current-head gap remains | SOURCE | P1 #1 |
| C01 | Auditable public-service answer | Source-sensitive video | Request → inspect evidence | Exact source/uncertainty retained | Source/evidence checks recorded; delivery surface pending phone validation | SOURCE + EXECUTED | none new |
| C02 | Low-learning-cost repeated use | Multiple ordinary users | Normal LINE messages | Ordinary chat unaffected by video machinery | Native gateway tests recorded; multi-user live behavior not separately proven | EXECUTED + NEEDS_RUNTIME | none new |
| C03 | High-risk caution | Ambiguous source | Ask for summary | Uncertainty retained; no unsupported certainty | Reviewer/evidence rules retain uncertainty | SOURCE + EXECUTED | none new |
| C04 | Long workflow continuity | 5m45s video | Start → continue → final integration | Resume without losing verified sections | Recorded long real-model fixture passed/replayed; edits during work remain unsafe on main | EXECUTED + SOURCE | P1 #1 |
| C05 | Fail-closed operations | Missing media/provider available | Recovery reaches provider boundary | External effect requires explicit authority and bounded state | Current source can POST metered task with only negative kill-switch absent | SOURCE + historical EXECUTED | **P2 #4** |
| D01 | See only meaningful exception | Review quality/status | Read report/quality state | Partial/failure separated from success | Quality/recovery states are explicit | SOURCE | none new |
| D02 | Track responsibility/state | Interrupted long task | Inspect receipts/state | Current step and ownership traceable | State/receipts exist; current message revision not represented on main | SOURCE | P1 #1 |
| D03 | Safe deployment/rollback | Apply update | plan → apply → rollback | Drift refusal and reversible update | Recorded install/rollback checks PASS | EXECUTED | none new |
| D04 | Prevent surprise cost | Missing media, provider token configured | Normal recovery | No paid request without explicit one-shot approval | Shared metered boundary lacks positive authorization; prior isolated tasks completed without approval | SOURCE + EXECUTED | **P2 #4** |
| D05 | Audit consent/effects | Review provider/delivery receipts | Trace action → authority → result | External effect tied to explicit authority | Provider state records submission/result but not positive authorization | SOURCE | **P2 #4** |
| E01 | Ordinary desktop support | Maintainer/operator | Follow operations | Clear commands and warnings | Operations are detailed; end-user remains LINE-first | SOURCE | none new |
| E02 | Readable routine use | Desktop/LINE | Read/send summary | Legible output | No actual accessibility/large-text execution evidence | NEEDS_RUNTIME | CLEAN blocker only |
| E03 | Avoid accidental action | Missing media | Ordinary video request | No hidden paid side effect | Automatic recovery can reach metered POST without positive approval | SOURCE | **P2 #4** |
| E04 | Familiar operational handoff | Operator with limited cloud skills | Follow install/check docs | Explicit reconstruct/check path | Rebuild and doctor paths documented/tested | SOURCE + EXECUTED | none new |
| E05 | Long-session clarity | Long video/reviews | Continue across sections | Progress remains interpretable | Section progress receipts exist | EXECUTED | none new |
| F01 | Obvious simple controls | LINE chat | Send URL/read answer | Minimal interaction | No real phone accessibility execution | NEEDS_RUNTIME | CLEAN blocker only |
| F02 | High contrast/zoom | Phone client | Read summary at enlarged text | Content usable under OS/client scaling | Not evidenced by repository/runtime | NEEDS_RUNTIME | CLEAN blocker only |
| F03 | Low-precision touch | LINE client | Tap/send/follow-up | No tiny custom controls required | Product uses LINE surface; actual device interaction untested | SOURCE + NEEDS_RUNTIME | none new |
| F04 | Low memory load | Long task then later follow-up | Continue/repeat/edit | Persistent current state, no stale answer | Durable job state exists; message edit/current-revision gap remains | SOURCE | P1 #1 |
| F05 | Setup assisted, daily use solo | Bot preconfigured | Routine send/read | Daily use does not expose admin setup | Fits documented primary surface; phone acceptance pending | SOURCE + NEEDS_RUNTIME | none new |
| G01 | Keyboard-only | Desktop LINE/web console if used | Navigate/send/read | Full core task keyboard accessible | No applicable executed accessibility evidence | NEEDS_RUNTIME | CLEAN blocker only |
| G02 | Screen reader | Client reads summary | Send/read state/error | Status not color/visual-only | No screen-reader execution evidence | NEEDS_RUNTIME | CLEAN blocker only |
| G03 | Color perception constraint | Client status/error | Read progress/failure | Meaning conveyed textually | Repo outputs textual state, but client behavior not executed | SOURCE + NEEDS_RUNTIME | none new |
| G04 | 200% zoom/narrow screen | LINE mobile/narrow desktop | Send/read long summary | No lost core control/content | No device/layout execution evidence | NEEDS_RUNTIME | CLEAN blocker only |
| G05 | Slow/high-latency network | Delayed provider/webhook | Start video/edit/retry | No duplicate/stale delivery; bounded recovery | Timeout bounds exist in many recovery calls; revision/redelivery fix unmerged | SOURCE | P1 #1 |
| H01 | Windows maintainer | Windows pipeline self-test | Run documented CI-equivalent | Windows-specific check succeeds | Recorded Windows self-test PASS; current Actions Windows job zero-step | EXECUTED | validation gap only |
| H02 | macOS maintainer | Unsupported/unspecified host | Try documented rebuild | Scope clearly states support | Operations say package supports Linux; macOS not claimed | SOURCE | not applicable with reason |
| H03 | Linux/CI noninteractive | Fresh Linux/CI | Run offline/hermes checks | Deterministic noninteractive validation | Historical local checks PASS; current GitHub-hosted jobs zero-step | EXECUTED + CI gap | validation gap only |
| H04 | Self-host/deploy safely | Provider credentials + service | Operate recovery/update | External paid effect separately authorized; update reversible | Update path bounded, but metered fetch positive authority absent | SOURCE | **P2 #4** |
| H05 | New third-party maintainer | First repo handoff | README → operations → checks | Understand truth/source/runtime limits | Docs are strong, but provider authorization contract differs from executable boundary | SOURCE | **P2 #4** |
| I01 | Duplicate submit | Same request repeated | Redeliver/retry | No duplicate expensive work/paid submission | Provider request state avoids repeat after pending/unknown; webhook dedupe/edit lifecycle absent on main | SOURCE | P1 #1; P2 #4 authority applies |
| I02 | Process/page interruption | Stop during long work | Restart/continue | Resume current request without stale state | Long-task resume evidence exists; message-revision recovery fix is unmerged | EXECUTED + SOURCE | P1 #1 |
| I03 | Bad input | Unsupported/unsafe URL | Submit | Reject safely without provider effect | Identity/source validation exists in recovery paths | SOURCE | none new |
| I04 | timeout/429/5xx | Provider/search failure | Trigger failure | Bounded timeout, unknown acceptance preserved, no blind repeat | Request state handles failed/unknown; positive authorization still absent | SOURCE | **P2 #4** |
| I05 | Partial success then retry | Provider POST response lost | Retry | Do not duplicate charge; preserve authority/result state | No-repeat unknown state exists; original submission still lacks positive authority gate | SOURCE | **P2 #4** |
| J01 | Large video/data | Long source | Process sections | Bounded sections/resources, no silent truncation | 60k evidence/100-point caps and long fixture documented/executed | SOURCE + EXECUTED | none new |
| J02 | Concurrent users | Parallel turns | Run simultaneously | Job/turn isolation; no stale cross-user delivery | Parallel turn ledger check exists; message revision lifecycle absent on main | EXECUTED + SOURCE | P1 #1 relevant |
| J03 | Long-running/resource pressure | Long video + recovery | Continue until bounded partial/ready | Time/resource limits and no uncontrolled paid work | Time/byte bounds exist, but positive metered authorization absent | SOURCE | **P2 #4** |
| J04 | Security/privacy/cost sensitive | Provider credentials and LINE data | Review external effects | Secrets private; external effects explicitly authorized | Secrets excluded from Git; metered POST authority boundary incomplete | SOURCE | **P2 #4** |
| J05 | Expert shortest-path automation | Direct CLI/recovery automation | Invoke shortest supported path | Same safety invariants as UI path | Direct metered shared entry uses negative switch, not positive authorization | SOURCE | **P2 #4** |

## Ten test dimensions

1. **First understanding:** README explains purpose, deployment/runtime caveats, and pending phone validation. No new P2 from onboarding.
2. **Core task:** actual recorded localhost model/gateway video flows exist, but no real-phone acceptance receipt for current required mobile path.
3. **Error recovery:** strong durable retry/state handling exists; current main still lacks message-edit/current-revision handling (#1).
4. **Data/security:** secrets/config are excluded from Git; new cost-authority boundary defect tracked as #4.
5. **Observability:** state/evidence/reviewer receipts are extensive; current provider authority is not represented as a positive approval state.
6. **Accessibility/device:** no screen-reader/zoom/mobile-device execution receipt. This blocks CLEAN but is not promoted into an unproven product defect.
7. **Performance/cost:** long-flow bounds and actual long fixture exist; #4 is the actionable cost-safety finding.
8. **Maintainability:** reproducible install/check tooling and explicit hashes are strong; current GitHub Actions execution is unavailable at step level.
9. **Failure injection:** timeout/unknown/no-repeat semantics exist in source/tests; edit/redelivery lifecycle remains open #1.
10. **Trust:** reviewer/source uncertainty controls exist; metered external-effect authority is not fail-closed by positive approval (#4).

## Issue / PR / lock coordination

- Existing Issue #1 was read with all comments. Its prior locks are released/expired, but active PR #2 (`github-1-line-input-lifecycle`) and PR #3 (`devin/issue-1-message-edited`) cover the same fingerprint. This audit did **not** acquire or rewrite #1; status recorded here as `SKIPPED_LOCKED` for Issue mutation.
- New fingerprint was searched across open/closed issues before creation; no duplicate was found.
- New actionable finding: Issue #4, `[50-persona audit][P2] Require positive one-shot authorization before metered video fetch`.
- No fix agent, merge, deployment, repository setting, secret, CI configuration, or paid provider call was initiated.

## Regression / fix status

- #1: `STILL_REPRODUCIBLE` on current default source; candidate fixes are unmerged. Existing isolated PR tests do not prove current default or live LINE behavior.
- #4: new finding; no fix landed, so no post-fix rerun exists yet.
- CI zero-step: `CANNOT_VERIFY` application tests from the current GitHub-hosted run; runner/admission root cause is not inferred from job failure.

## CLEAN decision

**NOT CLEAN — streak 0/2.** This round cannot qualify for CLEAN because:

- open applicable P1 #1 remains on current default branch;
- new applicable P2 #4 is open;
- required mobile/real-phone acceptance evidence remains missing;
- current GitHub-hosted workflow did not execute steps.

The 50/50 synthetic matrix is complete for this audit round, but completing a persona simulation is not itself a CLEAN-qualified round when blockers/runtime evidence remain.

## Next verification

After a relevant fix actually enters `main`, rerun the same affected persona inputs and failure triggers. For #4, verify with an isolated HTTP stub: credentials configured + kill-switch unset must produce zero provider POSTs without a valid one-shot authorization, one logical POST with a matching authorization, and zero replay after `pending/unknown`. No paid-provider call is required for closure. For #1, wait for an actual default-branch fix, then rerun the same edit/redelivery/out-of-order/restart scenarios plus the required real/test LINE canary evidence.
