# NINAX LINE Hermes

NINAX 的 LINE 機器人修復與影片補查實作。影片資訊不足時，先核對來源與既有影音，再補查字幕、語音及畫面；摘要經獨立檢核後，才送出與本回合相符的 LINE 文字。

本次 goal 的程式已部署，包含審稿回饋補查、長片分段與整合修訂、畫面比對、可重建環境、CI 與品質統計。LINE 輸入另有版本化生命週期：`messageEdited` 與 webhook 重送會建立持久收據與修訂 head，同一 `webhookEventId` 不重複啟動工作，被取代的舊修訂無法通過傳送閘門，重啟後由磁碟收據恢復 dedupe 與 pending delivery 狀態。短片流程及 5 分 45 秒長片的多回合續接檢核通過；見 [goal](video-loop/GOAL.md)、[驗證紀錄](video-loop/evidence/goal-verification.json)及 [重建與驗收](video-loop/OPERATIONS.md)。GitHub CI 被帳戶付款／支出上限擋住；手機實收及跨平台長版原片仍待驗收。稽核發現先前兩輪隔離測試在未取得單次授權時觸發 Bright Data 並完成，實際費用未知；已停止新增計費驗收，修正測試邊界及紀錄。

## 檔案

- [補查規劃](VIDEO-SECOND-PASS-PLAN.md)、[實作迴圈](video-loop/LOOP.md)及 [版本與驗證證據](video-loop/evidence/release-evidence.json)。
- `video-loop/work/profile/`：正式影片 helper、LINE 平台外掛與操作 skill；包含既有 Bright Data／Apify helper 相依程式。
- `video-loop/work/hermes/gateway/run_turn_runner.py`：套用通用平台回合入口的 Hermes 檔案；[差異補丁](video-loop/evidence/hermes-core.patch)以 Hermes `13e72fb205b735df679e0fd5f5996a34ac4accc6` 為基準。
- `video-loop/work/hermes/plugins/platforms/line/adapter.py`：為 `_LineClient.push()` 加上 `X-Line-Retry-Key` 的 Hermes 檔案（同一差異補丁）；安裝器與 CI 會套用並核對此檔案。
- `video-loop/check_*.py`、`test_custom_turn.py`、`tests/test_line_input_lifecycle.py`：來源、預算、審稿、傳送、gateway 與 LINE 輸入版本化檢查。
- `video-loop/deploy.py`、`update_helpers.py`、`production_check.py`：本次主機的部署、更新、回復與唯讀驗證工具。
- `work/gate-e/`：前一輪修正的 writer guard 與啟動設定；背景見 [基礎修復紀錄](REPAIR-REPORT.md)。

## 環境與驗證

`video-loop/install.py`、`runtime-lock.json` 與 `video-settings.example.json` 提供新環境重建、版本核對、路徑設定及有收據的更新／回復。原始 `deploy.py` 與 `update_helpers.py` 保留作為歷史部署工具，不能直接套用到不同版本或主機。

GitHub-hosted Actions 受帳務限制時，可用 `video-loop/check_ci.py` 在既有 Linux 與 Windows 主機執行 workflow 的相同檢查，保存同一 commit 的三組收據後，回報獨立的 `ninax/local-ci` 狀態。步驟見 [重建與驗收](video-loop/OPERATIONS.md)。正式 LINE 已另以官方驗證端點檢查兩份摘要格式，均 HTTP 200；手機實收仍須獨立確認。

原主機的離線檢查：

```bash
H=/home/box/.hermes/hermes-agent
B=/home/box/irisx-failover-restore/20260906/repair-20260907/goal-v3
"$H/venv/bin/python" -B "$B/check_video.py"
"$H/venv/bin/python" -B "$B/check_delivery.py"
```

`check_live_summary.py` 與 `check_line_video.py` 會使用已配置的模型服務；後者把 LINE 傳送導向 localhost，影片案例必須提供 `--source-job`，並強制禁止計費抓片後備。正式手機驗收另行記錄。

Git 以明確清單追蹤程式與已整理的驗證檔，正式設定、金鑰、登入資料、對話資料庫、備份及影音不納入版本。GitHub 推送不會重新啟動服務或改變主機控制權。

Hermes 衍生檔案保留上游 [MIT 授權](video-loop/work/hermes/LICENSE)；固定的影片管線來源保留 [原專案 MIT 授權](video-loop/vendor/video-pipeline/LICENSE)。
