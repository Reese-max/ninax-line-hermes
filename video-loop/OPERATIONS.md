# 重建、更新與驗收

此套件支援 Linux。固定 Hermes commit 見 `runtime-lock.json`；影片管線保留伺服器實際運作的 MIT 來源與 SHA256，沒有拿不同版本的 GitHub 主線冒充部署來源。

## 重建

先準備 FFmpeg／ffprobe、Git、Python 3.11.16 與 3.13.5。以一般服務帳戶執行；指定一個尚不存在的新目錄：

```bash
python3 video-loop/install.py --bootstrap /srv/ninax \
  --python311 /path/to/python3.11 --python313 /path/to/python3.13
/srv/ninax/hermes-agent/venv/bin/python video-loop/install.py \
  --doctor --settings /srv/ninax/settings.json
/srv/ninax/hermes-agent/venv/bin/python video-loop/install.py \
  --profile /srv/ninax/profile --settings /srv/ninax/settings.json \
  --apply --receipt /srv/ninax/install-receipt.json
```

Bootstrap 以鎖定版本建立三個 venv，取得固定的 Whisper 模型 revision。需要網路下載程式、套件與模型；不複製金鑰、對話或既有帳戶。失敗會保留目錄供檢查，不自動刪除重來。LINE 與模型登入資料由服務帳戶自行設定；安裝器不啟動 gateway、不取得 lease，也不變更 webhook。

`video-settings.example.json` 展示非機密路徑。原主機可繼續使用 `/workspace/bin/video-pipeline`；新環境會安裝 profile 專用 wrapper。供應商環境檔只在執行時讀取，不能把金鑰放進路徑設定或 Git。

## 有界更新與回復

先確認 gateway 沒有正在處理的回合，執行 doctor；再用上一份實際部署收據建立計畫。沒有 `--apply` 時只檢查並列出差異：

```bash
python video-loop/install.py --profile /srv/ninax/profile \
  --settings /srv/ninax/settings.json --baseline /srv/ninax/install-receipt.json
python video-loop/install.py --profile /srv/ninax/profile \
  --settings /srv/ninax/settings.json --baseline /srv/ninax/install-receipt.json \
  --apply --receipt /srv/ninax/update-receipt.json
python video-loop/install.py --rollback /srv/ninax/update-receipt.json
```

安裝器拒絕未知 Hermes commit、核心差異、未記錄的 helper 修改與符號連結越界。所有舊檔的日期備份及 prepared 收據會先落盤，再更新檔案。回復先核對全部檔案及備份；有其他修改就拒絕還原。新檔移到帶日期的保留名稱，未完成的更新也能回復。再次安裝同一版應為零差異。

更新 LINE 外掛後，由既有 supervisor 對精確的 gateway PID 進行正常停止與重啟；不要啟動第二個 writer。原主機仍使用 `production_check.py` 檢查 guard、lease generation、正式 webhook 與 Bot 名稱。這些只證明服務連通，不能代替手機收件。

## 檢查與統計

```bash
python3 -B video-loop/check_goal.py
python3 -B video-loop/check_install.py
python -B video-loop/check_video.py /path/to/hermes-agent
python -B video-loop/check_delivery.py /path/to/hermes-agent
python /srv/ninax/profile/hooks/video_workflow.py --report /srv/ninax/video-pipeline/jobs
```

前兩項只使用標準函式庫，在 Linux 執行。原影片管線的 `--self-test` 包含 Windows 排程案例，因此 CI 在真正的 Windows runner 執行它；不偽裝 Linux 平台。原生 Hermes 測試必須經 `scripts/run_tests.sh`。GitHub CI 包含離線回圈、回復、Windows 管線、原生 LINE 邊界與 gateway 回歸，不需要服務帳戶的金鑰。

GitHub Actions 無法啟動時，可以在既有主機執行相同的三組檢查；workflow 與本機共用 `check_ci.py`。Linux 使用 Python 3.11，Windows 使用 Python 3.13 並依 `runtime-lock.json` 安裝管線相依套件；Hermes 使用依鎖定版本重建的隔離環境，不能指向正式服務。每組收據保存 commit、實際系統與 Python 版本、指令及結束碼；要求已提交且無追蹤檔差異，輸出必須使用新的檔案路徑。

```bash
python -B video-loop/check_ci.py offline --out /tmp/ninax-offline.json
python -B video-loop/check_ci.py hermes-contract --hermes /srv/ninax-test/hermes-agent --out /tmp/ninax-hermes.json
```

```powershell
python -B video-loop/check_ci.py pipeline-windows --out "$env:TEMP/ninax-windows.json"
```

三組必須對應同一 commit 且全部 PASS，才可透過 GitHub 的 [commit status API](https://docs.github.com/en/rest/commits/statuses#create-a-commit-status) 回報獨立的 `ninax/local-ci` 狀態。這項狀態不宣稱 GitHub-hosted Actions 已執行；原本的帳務錯誤仍保留。此替代路徑不需要新增帳號、付費方案或常駐 runner。

`jobs/.ninax-quality.json` 自動更新來源證據狀態、審稿通過率、新證據回合數、p50／p95 延遲與審稿模型回報的 token 數。審稿通過與來源完整是分開的統計；部分摘要仍須通過事實檢核。未知用量及價格保持未知；連續三回合未通過會在本機報告列出警示，不對外發訊息。這份統計沒有聊天內容，原始審稿證據仍留在各 job 的 `evidence.json`。

音訊政策 `ninax-audio-3.0` 會重新處理舊政策的語音快取，並保留原稿；畫面取樣快取繼續沿用。補轉錄漏掉的區間保留原辨識，不以另一個模型的遺漏刪除既有內容。新增或修正片段不再改動其他證據編號，已審分段依其實際內容決定是否重用。

長片依兩分鐘與文字量分段，已核對段落可接續使用；全部分段完成後，最後審稿仍收到完整來源證據。整合指出文字矛盾時，沿用逐句修訂一次並重新審稿；修訂稿及檢核結果也會保存，整合超時可接續。單次整合上限為 60,000 字元的證據與 100 項必要重點，另受 LINE 訊息容量限制；超過時停止送出摘要，不裁掉末段。更大的影片仍需分成較短來源，尚未提供附件式長篇報告。

## 真實驗收

`check_line_video.py --case short --source-job /path/to/prepared/job` 使用真實 gateway、模型及已備妥的影片，LINE 傳送只導向 localhost。影片案例必須提供來源 job，會複製必要影音與辨識資料到新的隔離 jobs 目錄，並強制設定 `NINAX_DISABLE_METERED_FETCH=1`。缺少 `--source-job` 就退出，不能從測試自動取得計費來源；一般對話案例 `--case normal` 不需要影片。

`check_extended.py --job ... --profile ... --out ...` 驗證已備妥的長片，同樣強制禁止計費抓片後備；最多八個明確的隔離回合，保留每次缺口與進度，不向 LINE 發訊息。遇到審稿失敗就停止；只有待續分段或指定補查會接續。

`check_cross_platform.py --hooks /path/to/profile/hooks --source-job /path/to/prepared/job --job /path/to/new/fixture --candidate https://www.youtube.com/watch?v=VIDEO_ID --out /path/to/new/receipt.json` 只用公開候選與本機處理，強制禁止計費後備。來源與輸出必須隔離；一次驗證一個候選，未通過回傳 exit 1。字幕失敗或逾時且仍有預算時，繼續畫面比對；無可用語音錨點時直接比對畫面。必須有下載完成標記與相符的來源身分，不能用半成品或相似標題判定為原片。

手機驗收需將影片連結送給 NINAX，確認收到重點、片尾及來源，再送「完整一點」；長片未完成時用「繼續摘要」。以正式回合／delivery 收據配對手機回報，不能只看 health 或 HTTP 200。

計費抓片需另有明確的單次授權，沿用一份 state 收據；狀態 pending／unknown 不重送 POST。Bright Data 的餘額查詢權限與擷取權限不同，不能把餘額 HTTP 403 判成擷取故障。Apify 未配置時列為 unavailable，不假裝完成備援。

授權機制（2026-09-16 起強制）：`video_recovery.py --fetch` 在任何**新**的 provider POST 前，必須存在一份綁定該來源 identity（platform+id）、未消費、未過期的授權收據 `<state 檔同名>.authorization.json`。授予方式：

```bash
python video_recovery.py --authorize-fetch <url> --state <state.json> \
    [--ttl 300] [--authorized-by <操作者>]
```

收據在 POST **之前**先標記 `consumed`——回應遺失不得重送。缺少收據、綁定不符、已消費或過期一律 fail closed（`not_authorized`／`authorization_mismatch`／`authorization_consumed`／`authorization_expired`），不會接觸 provider。既有 pending/unknown 工作的續查不需要新收據（不重送 POST）；`NINAX_DISABLE_METERED_FETCH=1` 仍是最外層緊急停止開關。

2026-09-08 稽核發現先前兩輪隔離短片測試在單次授權尚未取得時觸發 Bright Data，兩份任務均已完成，實際費用未知。已停止新增計費驗收並修正上述測試入口；完整紀錄保留於 `evidence/goal-verification.json`。此禁止開關套用於驗收工具，不變更既有正式機器人的供應商設定。

目前的非同步擷取使用單一 URL 的 `input` 陣列物件；Bright Data 的[官方 Instagram 文件](https://brightdata.mintlify.app/products/scrapers/instagram/introduction)確認此 request body 可用於 `/trigger`。

畫面仍是取樣，自動辨識仍有誤差。原片作者、同音字或完整情節無法確認時，保留不確定；不以更長的文字冒充完整影片證據。
