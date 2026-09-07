---
name: video-timeline-pipeline
description: >
  Summarize public video links using source-bound media evidence, bounded second-pass
  retrieval, and an independent completeness audit. Used by NINAX's LINE video workflow.
metadata:
  hermes:
    tags: [video, facebook, instagram, youtube, tiktok, summarize, required]
---

# NINAX 影片摘要

LINE 影片回合由 profile 的 line-platform 外掛處理：先取得證據，必要時補查，再產生與審查摘要。一般聊天維持 Hermes 原流程。

- 不以草稿非空、字數、標題數或 transcript.json 存在，判定摘要完整。
- 以平台與大小寫敏感影片 ID 比對來源。標題相似、資料夾名稱、搜尋結果片段不能證明是同一影片。
- 有本機媒體時，重用 video-settings.json 指定的 video-pipeline wrapper（原主機預設 /workspace/bin/video-pipeline）、取樣與影像快取，補做全段音訊及必要畫面分析。完整下載與逐幀檢視是不同事實。
- 固定間隔取樣以外，另取距片尾約 0.25 秒的畫面。音訊若出現大量重複字或低品質區段，沿用已配置的 Groq 補轉錄一次，範圍最多 120 秒；無法修復的區段列為缺口。
- 缺原始內容時，以已取得的公開作者、字幕、畫面文字查詢，最多 3 次搜尋、3 個候選來源。其他影片須通過作者與帶時間語音片段比對，或多個獨特畫面的順序／時間差比對。後者只證明內容版本相符，作者身分仍未確認；原片與補查版本的時間及事實分開標示。
- 審稿可引用實際 E 編號提出最多 2 個精準補查要求，每回合最多回到蒐集階段 1 次，再用新證據重審。語音最多 120 秒、畫面最多 30 秒；已補聽仍不清的同音字可改查畫面文字。相同種類及範圍不重複處理，仍不清楚時保留不確定。
- 每回合最多 1 次新增單片付費擷取；已接受的遠端工作保留 task ID，逾時後先查進度，不重複送單。全流程含傳送上限 300 秒。
- evidence.json 的 evidence_status 為 ready／partial／insufficient。每個回合有自己的 turns[turn_id]、must_cover、summary_audit 與 approved_delivery；不可用別回合的通過紀錄代替。
- 每次來源審稿最多修訂 1 次、檢查 2 次；partial 仍需事實檢查通過。超過 5 分鐘或單次證據上限時分段核對，保存已通過的段落；最後另外檢查跨段矛盾與完整 LINE payload。分段末端不可冒稱全片結尾。
- 影片草稿不得由串流、推播或快取按鈕提早送出。送出內容須與核准的 LINE payload、來源版本與本回合完全一致。
- 「再說一次」可重用相同證據與審稿政策版本的已審摘要；「完整一點／補查／再查一次」重新評估缺漏；「繼續摘要」沿用原需求接續已保存的長片進度。沒有新資料時，明說已重新核對既有資料。
- 依實際內容整理目的、主要情節／論點、必要條件／順序、轉折與結尾；不硬套教學步驟。不要整段抄字幕、引用歌詞或猜人物身分、地點、歌名與作者。

人工除錯只看結構化證據與檢查紀錄；內部路徑、憑證、signed CDN URL 與 provider 回應不放進 LINE。
底層單次蒐集入口為 profile 的 hooks/video_takeaway_cascade.py；它輸出證據，不能把它的輸出直接當已審摘要送出。
