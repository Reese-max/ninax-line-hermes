# Issue #1 — messageEdited / redelivery 版本化輸入與 stale-summary 失效（設計）

Design deliverable for `Reese-max/ninax-line-hermes#1`。
LINE 2026-08 新增 group-chat `messageEdited` event；webhook redelivery
不保證順序或 exactly-once。

## 1. Schema

### 1.1 `WebhookEventReceipt`（dedupe）

```jsonc
{
  "webhookEventId": "...",          // LINE 官方 dedupe key
  "firstSeenAt": "...", "deliveryCount": 2,
  "isRedelivery": true,
  "jobId": "job_... | null"         // 已建立的昂貴 job；redelivery 不再開新 job
}
```

同一 `webhookEventId` redeliver N 次 → 一個 logical event / 一個 video job。

### 1.2 `MessageRevision`

```jsonc
{
  "messageId": "line-msg-id",
  "revision": 2,
  "contentHash": "sha256(text)",
  "source": "message | messageEdited | unsend",
  "status": "CURRENT | SUPERSEDED | UNSENT",
  "receivedAt": "...", "eventTimestamp": "..."   // 判斷 late event 靠 timestamp 不靠到達序
}
```

### 1.3 `StaleJobInvalidation`

```jsonc
{ "jobId": "job_...", "messageId": "...", "revision": 1,
  "disposition": "cancelled-local | cost-incurred-uncancellable | completed-stale",
  "deliveryGate": "BLOCKED-STALE | ALLOWED-CURRENT" }
```

## 2. 規則（issue 驗收）

- edit 到達 → 舊 revision 標 `SUPERSEDED`；可取消的本機階段 cancel；
  已送出的外部/付費 request 不假裝能取消（保留 cost/unknown receipt）。
- stale job 完成 → delivery gate 拒送 stale result。
- 舊回答已送出後 user 才 edit → 可送短 correction 提示，**不假裝可撤回
  已送 LINE 訊息**；correction policy 有 noise limit。
- restart 後從 durable state 恢復 current head / active job / delivered
  receipt；不靠 arrival order。
- 若 `messageEdited` schema 的 message identity 與普通 message 不完全相同
  → 依官方 schema 建 adapter，不自造 relation。

## 3. 對本 repo 的接線

此 repo 是驗證 harness（`video-loop/check_*.py`、`production_check.py` 打
`/line/webhook`）；webhook handler 在別處。建議新增
`check_message_revision.py` fixture 類：
- `duplicate-webhookEventId` → 只建一個 job
- `edit-supersedes` → revision 1 → SUPERSEDED，stale job 被 delivery gate 擋
- `late-redelivery` → event timestamp 比 current head 舊 → 不覆寫
- `unsend` → revision 標 UNSENT，不送 summary

## 4. 不做什麼

- 不把 webhook redelivery 當 exactly-once。
- 不因 duplicate event 丟棄所有 late event（edit event 本身是新狀態）。
- 不把外部付費 request 的 timeout 當「沒產生成本」。
