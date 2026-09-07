# NINAX LINE Hermes

NINAX 的 LINE 機器人修復與影片補查實作。影片資訊不足時，先核對來源與既有影音，再補查字幕、語音及畫面；摘要經獨立檢核後，才送出與本回合相符的 LINE 文字。

目前版本已部署，46 項 Hermes 回歸、6 個審稿案例、兩支代表影片及一般聊天隔離流程通過。手機實收、計費抓片及跨平台長版原片仍待驗收；詳細測試指令、版本差異與限制見 [實作紀錄](video-loop/IMPLEMENTATION.md)。

## 檔案

- [補查規劃](VIDEO-SECOND-PASS-PLAN.md)、[實作迴圈](video-loop/LOOP.md)及 [版本與驗證證據](video-loop/evidence/release-evidence.json)。
- `video-loop/work/profile/`：正式影片 helper、LINE 平台外掛與操作 skill；包含既有 Bright Data／Apify helper 相依程式。
- `video-loop/work/hermes/gateway/run_turn_runner.py`：套用通用平台回合入口的 Hermes 檔案；[差異補丁](video-loop/evidence/hermes-core.patch)以 Hermes `13e72fb205b735df679e0fd5f5996a34ac4accc6` 為基準。
- `video-loop/check_*.py`、`test_custom_turn.py`：來源、預算、審稿、傳送與 gateway 檢查。
- `video-loop/deploy.py`、`update_helpers.py`、`production_check.py`：本次主機的部署、更新、回復與唯讀驗證工具。
- `work/gate-e/`：前一輪修正的 writer guard 與啟動設定；背景見 [基礎修復紀錄](REPAIR-REPORT.md)。

## 環境與驗證

此快照沿用原 Linux 主機的 Hermes、`/workspace/video-timeline-pipeline`、FFmpeg、本機 Whisper 與既有模型授權。程式保留當時的部署路徑，部署腳本會檢查來源雜湊與備份；換主機時先核對這些路徑及環境。

原主機的離線檢查：

```bash
H=/home/box/.hermes/hermes-agent
B=/home/box/irisx-failover-restore/20260906/repair-20260907/video-loop
"$H/venv/bin/python" -B "$B/check_video.py"
"$H/venv/bin/python" -B "$B/check_delivery.py"
```

`check_live_summary.py` 與 `check_line_video.py` 會使用已配置的模型服務；後者把 LINE 傳送導向 localhost。正式手機驗收另行記錄。

Git 以明確清單追蹤程式與已整理的驗證檔，正式設定、金鑰、登入資料、對話資料庫、備份及影音不納入版本。GitHub 推送不會重新啟動服務或改變主機控制權。

Hermes 衍生檔案保留上游 [MIT 授權](video-loop/work/hermes/LICENSE)。
