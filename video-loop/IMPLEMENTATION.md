# NINAX 影片補查與摘要檢核實作（原始版本）

本檔保留 `88d4366` 的歷史部署與驗證資料。後續 goal 的版本、限制與實測結果見 [GOAL.md](GOAL.md)、[OPERATIONS.md](OPERATIONS.md) 及 [goal 驗證紀錄](evidence/goal-verification.json)。

2026-09-07：補查、摘要檢核與 LINE 傳送檢查已實作並部署至唯一的 standby gateway；隔離流程通過，手機實收尚未驗收。完整測試與版本紀錄見 [evidence/release-evidence.json](evidence/release-evidence.json)。

## 已實作

- 先核對影片身分、實際媒體、片長、音訊處理範圍與畫面證據，再決定 ready／partial／insufficient。
- 過期網址重新解析，修正舊 `videoUrl` 蓋過新網址的問題。不符片長時補取媒體並保留原檔；同一下載網址不重複嘗試。
- 畫面沿用既有取樣與快取，額外補距片尾約 0.25 秒的畫面。明確要求補查時，取樣上限可由 8 增至 16、32、40；保留已觀察的畫面，只分析新增影格。影片畫面仍是抽樣，沒有宣稱逐幀檢視。
- 語音出現大量重複字或低品質時，沿用現有 Groq 設定補轉錄一次，最多涵蓋 120 秒；保留原辨識結果與補查紀錄。
- AnySearch 優先搜尋公開影片線索；服務或連線失敗時才使用已驗證的 Hermes 原生搜尋。候選必須核對來源，不能因標題相似就混入摘要。
- 從原始證據建立必要重點並直接組成摘要，必要清單必須包含視覺片尾；獨立審稿核對實際 LINE 文字的重大遺漏、數字、條件、否定、結尾與來源。最多以逐句替換修訂一次，再重驗；未通過時回覆具體缺口或狀態。
- 「再說一次」可重用已審摘要；「完整一點」重新評估證據。沒有新內容時明說，不把改寫舊稿當作補查成功。
- LINE 傳送綁定本回合、證據版本與完整 payload；防止提前串流、錯用其他回合 token、未審快取按鈕及不確定結果的重複推播。

## 實際驗證

以下命令在備援主機執行。`H=/home/box/.hermes/hermes-agent`；`B=/home/box/irisx-failover-restore/20260906/repair-20260907/video-loop`。

| 命令 | 結束碼與證據 |
|---|---|
| `H/venv/bin/python -B B/check_video.py` | exit 0；來源、快取、時間戳、並行紀錄、過期網址、媒體異動、重複下載、原片對齊與程序停止反例通過 |
| `H/venv/bin/python -B B/check_delivery.py` | exit 0；未審稿、錯誤回合、重複傳送、逾時、快取按鈕等檢查通過 |
| `H/venv/bin/python -B B/check_live_summary.py` | exit 0；6 案通過：正確摘要放行，漏結尾、錯數字、條件反轉、必要清單漏結尾及錯時間 5 個錯誤稿被攔下 |
| `H/venv/bin/python -B B/check_line_video.py --case short` | exit 0；110.6 秒，含摘要、「再說一次」、「完整一點」三回合；各送一次，新增畫面保留原取樣且重新審稿 |
| `H/venv/bin/python -B B/check_line_video.py --case long` | exit 0；78 秒來源，132.3 秒完成；語音、重要內容、視覺片尾與實際送出文字通過檢核 |
| `H/venv/bin/python -B B/check_line_video.py --case normal` | exit 0；21.1 秒，實際原生工具結果與一般對話均有儲存 |
| `H/venv/bin/python -B B/work/profile/hooks/video_recovery.py --search '"DZrNsyXivVc" Instagram'` | exit 0；AnySearch 連線成功並取得 1 個候選 |
| `HERMES_PYTHON=B/test-venv/bin/python bash scripts/run_tests.sh -j 2 tests/gateway/test_line_plugin.py tests/gateway/test_stream_final_contract.py tests/gateway/test_stream_final_adoption_gate.py B/test_custom_turn.py -q` | exit 0；已修改的核心原始碼，46 passed、0 failed |
| `H/venv/bin/python -B B/production_check.py` | exit 0；正式健康檢查、簽章空 webhook、Bot info 與官方 webhook 均 HTTP 200 |
| `H/venv/bin/python -B B/update_helpers.py` | exit 0；3 份 helper 更新，部署前檢查雜湊與閒置狀態，更新後正式服務檢查通過 |

隔離 LINE 測試使用真實 Hermes gateway、外掛、影片檔與模型；只有 LINE 傳送導向本機測試 API，沒有對真實聊天發訊息。也已人工查看兩支影片的代表開頭與片尾、長片中段畫面；這不等同於人工逐秒聽讀完整影片。

短片最後一輪對應最終部署版本。長片通過後，只再修改補取樣保留舊影格的分支，該分支已由短片追問重驗。一般聊天測試所涵蓋的原生路徑未再變更；各次版本差異均留在證據檔，沒有把舊測試改標為新版本執行結果。

## 部署與回復

正式 profile：`/home/box/irisx-failover-restore/20260906/profile-irisx`。LINE 名稱 NINAX，gateway PID `1227872`；原 guard PID `1123259`、holder `standby-cursor-grokbot`、generation `8` 維持不變。WSL primary 的兩個舊服務仍為 inactive／disabled。

12 份檔案及後續 3 份 helper 更新的備份與 SHA256 見 [evidence/deployment.json](evidence/deployment.json)。核心只增加通用 `run_custom_turn(ctx)` 入口，profile 啟用 `line-platform` 並移除舊 video prefetch hook 註冊；一般模型、授權與入口保持原配置。Hermes 核心是尚未提交的本機修補，升級 Hermes 時須重新核對 [evidence/hermes-core.patch](evidence/hermes-core.patch)。

需要回復此功能時，在主機先執行 `H/venv/bin/python -B B/deploy.py --rollback`，再執行 `H/venv/bin/python -B B/production_check.py --restart --restored`。既有檔案從備份還原，新檔改為帶日期的保留名稱；不刪資料。回復程序未在正式服務演練。

## 尚未驗收與已知限制

- 手機實際收件，以及使用者對內容、片尾與「完整一點」追問的確認，仍待回覆。
- 本輪沒有新增付費擷取工作。現有 Bright Data token 的[餘額查詢](https://docs.brightdata.com/api-reference/account-management-api/Get_total_balance_through_API)回傳權限不足；不能據此確認剩餘額度或宣稱擷取一定可用。Apify token 未配置，因此不是目前可驗收的備援來源。
- 長版原片核對已測試正確對齊與錯誤作者／時間反例；尚無實際跨平台長版原片的完整驗收。
- 取樣畫面及自動語音辨識仍可能有誤；本輪審稿採原始證據核對，不能保證辨識本身永遠正確。證據包超過 65,000 字元時停止並回覆限制，不默默截掉內容。
- 全回合 300 秒、最多 3 組查詢、3 個候選、1 次額外單片擷取、1 次摘要修訂；超過預算或證據不足時保留進度與限制。無法保證所有未公開或高難度影片都能補齊。
