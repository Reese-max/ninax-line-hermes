# NINAX 修復與驗證紀錄

驗證時間：2026-09-07 15:15（台灣時間）。技術驗證 **PASS，結束碼 0**；修正已載入正式服務。手機端的實際收發仍待使用者確認。

本次確認 LINE 官方帳號名稱為 NINAX。正式服務位於 `standby-cursor-grokbot`，使用 `/home/box/irisx-failover-restore/20260906/profile-irisx`。主機控制權維持 generation 8。

## 修復內容

| 問題 | 根因與修正 |
|---|---|
| 回覆只剩最後一段 | 舊文字轉接服務把輸出依空行切開，只保留最後一段。NINAX 改用 Hermes 既有的 `openai-codex` 原生整合，完整傳遞模型回覆。 |
| 宣稱會執行工作，實際未執行 | 舊轉接服務未傳遞工具定義與結果，還會改寫下載失敗訊息。改走原生工具呼叫流程，並停用會覆寫回覆的 `video-takeaway-guard`。 |
| IG 影片摘要失敗 | 改為先核對影片 ID 並使用既有快取；從快取的影片下載位址補抓媒體，透過既有本機 Whisper 取得逐字稿。加入整體逾時與子程序清理，摘要器失敗時繼續下一個來源。僅有貼文資訊時會如實標示。 |
| 防護程序追蹤過期 PID | 改讀 Hermes 原生 `gateway.pid`，核對 profile、程序出生時間與環境，再用 pidfd 傳送停止訊號，避免誤停重用 PID 的其他程序。 |

主要模型維持 `gpt-5.3-codex-spark`；第一備援為原生 `gpt-5.6-luna`。沿用本機既有 ChatGPT 登入，未新增金鑰。

## 驗證

下列檢查均通過，結束碼為 0。

- `python -B staged/check_repair.py`：PID 更新、失去控制權時停止正確程序、拒絕重用 PID、精確影片比對、逐字稿快取、媒體 URL 檢查、子程序逾時與摘要器失敗處理。
- `python -B staged/check_line_flow.py`：真實 Hermes 與原生模型執行工具；LINE webhook 到回覆流程保留開頭、中段、結尾，資料庫確認有 1 筆工具結果。對 LINE API 的輸出在 localhost 擷取。
- `python -B staged/check_line_flow.py --video --fallback`：使用 `gpt-5.6-luna`，從 LINE 訊息進入影片 hook，依實際逐字稿產生完整摘要。對 LINE API 的輸出同樣在 localhost 擷取。
- 先前失敗的 `DY66ObpOlby`、`DZrNsyXivVc` 兩支影片均成功使用經核對的快取；前者完成本機語音辨識，後者匯入既有來源逐字稿。
- LINE 官方帳號資訊、正式 webhook 設定均回傳 HTTP 200；對公開 webhook 傳入簽章正確的空事件也回傳 HTTP 200。
- 6 份正式程式檔案 SHA256 與部署紀錄相符，Python 編譯檢查及 shell 語法檢查通過。
- Gateway PID `1123144`、防護程序 PID `1123259` 正常運行；重啟後至本次驗證時未出現新的 ERROR／CRITICAL 紀錄。

上述相對指令從遠端 `/home/box/irisx-failover-restore/20260906/repair-20260907` 執行，使用既有 Hermes venv 的 Python。原始檢查程式保存在本目錄的 `work/`，結果保存在 `evidence/`。

## 備份與驗收界線

正式修改前已建立設定、登入資料及原程式的備份；共確認 8 份備份存在，完整路徑記於 `evidence/deployment.json`。另透過 SQLite backup API 保存一致的 `state-before-repair.sqlite`，位於遠端修復目錄。登入資料及資料庫備份留在遠端，未匯出至本機證據目錄。

本次沒有代替使用者對外傳送 LINE 聊天訊息。公開路由、原生模型、工具、影片及隔離 LINE 流程均已驗證，但仍需要使用者在手機傳入訊息，確認實際收到完整回覆。這一步尚未完成，不把 HTTP 200 或隔離測試當作手機端驗收。

最終狀態見 `evidence/final-verification.json`：`technical_verification=PASS`、`phone_acceptance=pending_user`。
