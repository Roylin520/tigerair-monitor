# 虎航票價監控

自動追蹤台灣虎航來回機票的最低價，在 GitHub Actions 上執行。結果會發布成報表網頁，價格低於設定門檻時寄信通知。

📊 **報表網頁：** <https://roylin520.github.io/tigerair-monitor/>

## 功能

- 每條航線查詢去程、回程每天的最低未稅單程票價，再依「玩幾天」組合成來回行程。
- 加上各航線的稅金，算出**每人來回含稅估價**，並列出最便宜的前幾組。
- 報表會顯示和上次執行相比的價格變化，以及歷史最低價。
- 某組行程第一次降到門檻以下，或比上次通知時更便宜，就寄一封信。同一組行程不會重複通知。
- 價格紀錄存在 `data/` 的 CSV 檔，可以用 Excel 開啟。

> ⚠️ 所有價格都是**估算值**：兒童以成人同價計算，也無法得知是否還有座位。實際價格請以訂票頁為準（報表上每組都有「訂票 ↗」連結）。

## 修改設定

平常只需要改 [`config.toml`](config.toml)，直接在 GitHub 網頁上編輯並 Commit 即可，存檔後會自動重新執行一次。

| 區段 | 設定項目 | 說明 |
|---|---|---|
| `[trip]` | `origin` | 出發機場代碼，例如 `KHH` |
| | `earliest_departure` | 最早去程日期；填 `"today"` 代表每次執行當天 |
| | `latest_return` | 最晚回程日期，格式 `YYYY-MM-DD` |
| | `trip_days` | 玩幾天（含去回當天），可填多個，例如 `[5, 6]` |
| | `adults` / `children` | 成人、兒童人數 |
| `[[route]]` | `destination` | 目的地機場代碼，一條航線一段 |
| | `tax_outbound` / `tax_return` | 每人去程、回程稅金（台幣） |
| `[alert]` | `max_price_per_person` | 每人來回含稅門檻，低於或等於就通知 |
| `[report]` | `top_n` | 每條航線列出幾組 |

設定有誤時，報表網頁會顯示錯誤訊息和修正方式。

注意：改了旅客人數，歷史紀錄會從新的檔案重新開始。

## 執行時機

由 [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) 控制：

- 修改 `config.toml` 或 `monitor.py` 並推上 `main` 時自動執行。
- 在 GitHub 的 **Actions** 分頁手動按 **Run workflow**，可以勾選「寄一封測試信」。
- 定時排程：`schedule` 區段（每 5 分鐘），**目前已註解停用**。要恢復就把那兩行的 `#` 拿掉。
- 外部排程：cron-job.org 透過 `workflow_dispatch` 觸發，要另外到 cron-job.org 開關。

每次執行後，機器人會把 `docs/` 和 `data/` 的更新 commit 回 `main`（訊息為「Update prices …」）。所以自己推送前請先 `git pull --rebase`。

## 寄信設定

在 repo 的 **Settings → Secrets and variables → Actions** 新增：

| Secret | 說明 |
|---|---|
| `SMTP_USER` | 寄件信箱帳號（預設使用 Gmail） |
| `SMTP_PASS` | 信箱密碼；Gmail 請使用「應用程式密碼」 |
| `MAIL_TO` | 收件人，多個地址用逗號分隔 |

沒設定的話不會寄信，其他功能照常。

## 在本機執行

需要 Python 3.11 以上，不用安裝任何套件。

```sh
python -u monitor.py                   # 查價、寫入紀錄、更新報表，必要時寄信
TEST_EMAIL=true python -u monitor.py   # 寄一封測試信，內容是每條航線最便宜的 3 組
```

本機執行會改寫 `data/` 和 `docs/index.html`，和線上執行寫的是同一批檔案。

## 檔案說明

| 路徑 | 內容 |
|---|---|
| `config.toml` | 監控設定 |
| `monitor.py` | 主程式 |
| `docs/index.html` | 報表網頁（GitHub Pages） |
| `data/history_*.csv` | 每條航線的價格紀錄 |
| `data/notified.json` | 已通知過的行程，用來避免重複寄信 |
