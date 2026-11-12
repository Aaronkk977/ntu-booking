# 臺大場地預約（工作站多人版）

架在一台 Linux 主機（例如資工系工作站）上，你和幾個朋友用手機開網址、用自己的計中帳號登入，搭配臺大體育室場地租借系統（rent.pe.ntu.edu.tw）使用：

- **可租時段搜尋**：今天到 7 天後每個時段還有幾面空場（含「僅限現場租借」），可複選場地。結果大家共用。
- **機器人**：每個人設定自己的「場地＋時段」濾網。伺服器每幾分鐘**只掃一次**所有人濾網用到的場地，符合的線上空位加入各自的清單，並用 **Discord 私訊**通知。
- **準備預訂 → 手機確認**：伺服器在背景把表單填到學校的確認頁（濾網開「找到就先準備」時會自動做），私訊你連結；你在手機看過確認頁截圖，按「確認送出」才真的下單，10 分鐘沒按就放棄、什麼都不送。**繳費仍要自己到學校網站。**
- **訂單管理**：每 30 分鐘讀一次自己的「常租紀錄」；繳費期限剩 1 天時私訊提醒。
- **歷史熱門度**：過去每個星期幾、每個時段被訂光的比例（管理者更新，大家都看得到）。

不會繞過驗證碼；只支援計中帳號（`b12345678`，學校網站的 `b12345678@ntu.edu.tw`）透過計中 SSO 登入。

## 先知道的風險

- **工作站規範**：資工系《工作站帳號使用規範》寫明帳號「限作教學及研究用途」，濫用網路資源初犯停權 1 個月；管理團隊也可能結束長時間執行的程式。架在工作站上是你自己承擔這個風險。
- **密碼保存**：為了在學校登入過期時自動重新登入、幫大家準備預訂，伺服器會把每個人的**計中密碼加密保存**（`data/`）。能讀到 `data/secret.key` 和 `data/` 的人（例如工作站 root）就能解開。計中帳號也是信箱和選課的帳號，請讓朋友知情同意，只給信任的人用。任何人都可以在「設定」刪除自己在伺服器上的所有資料。
- **租借規定**：一年內借了沒用、也沒取消達 3 次，一年內不得再借綜合體育館球場。送出前看清楚、訂了就要去或取消。
- **單一裝置登入**：學校網站可能只允許一個裝置登入，伺服器替你登入時，你手機瀏覽器上的學校網站登入可能會被登出。
- 服務條款禁止干擾系統運作：掃描間隔最短 3 分鐘，掃描只用一個帳號，人數多也不會多打學校網站。

## 架設（Linux，不需要 root）

```bash
git clone <this repo> ntu-booking && cd ntu-booking
deploy/setup.sh                                   # uv、Python 3.12、headless Chromium、cloudflared、加密金鑰
.venv/bin/python manage.py set-admin b12345678    # 你自己：管理者，也是共用掃描用的帳號
.venv/bin/python manage.py allow r13922000 b11000000   # 朋友們
.venv/bin/python manage.py set-discord-token      # 貼上 Discord bot token（不會顯示）
deploy/run.sh                                     # 啟動網站與 tunnel，印出網址
deploy/run.sh --cron                              # 印出兩行，貼進 crontab -e，掛掉或重開機會自己起來
```

- `setup.sh` 最後會試著開一次 headless Chromium。開不起來通常是工作站少了系統函式庫，錯誤訊息會寫是哪個；沒有 root 只能請管理團隊裝，或換一台。
- 家目錄有配額，Chromium 約 100 MB；可在 `deploy/env` 設 `PLAYWRIGHT_BROWSERS_PATH` 放到 `/tmp2`（會被定期清掉，被清了重跑 `setup.sh`）。
- 工作站之間共用家目錄時，服務只會在第一次啟動它的那台跑（`data/*.pid` 記著主機名稱）；crontab 也只設在那一台。
- 停止：`deploy/run.sh --stop`。紀錄在 `data/logs/`。

### 對外網址（Cloudflare Tunnel）

伺服器只聽 `127.0.0.1`，由 `cloudflared` 把 HTTPS 網址接進來，工作站不用開 port。

- **不設定**（預設）：quick tunnel，網址像 `https://xxx.trycloudflare.com`，**每次重啟都會變**；`run.sh` 會把新網址存進設定並私訊給每個設好 Discord 的人。網址一變，大家要重新登入一次。
- **固定網址**：要有一個放在 Cloudflare 的網域。Cloudflare Zero Trust → Networks → Tunnels 建一個 tunnel，把你的子網域導到 `http://127.0.0.1:<PORT>`，把 token 和網址填進 `deploy/env` 的 `TUNNEL_TOKEN`、`PUBLIC_URL`。

### Discord bot

1. 到 <https://discord.com/developers/applications> 建一個 application → Bot → Reset Token，複製 token，執行 `manage.py set-discord-token`。
2. OAuth2 → URL Generator 勾 `bot`，用產生的連結把 bot 邀進一個大家都在的 Discord 伺服器（bot 只能私訊和它在同一個伺服器的人）。
3. 每個人在網站「設定」填自己的 Discord 使用者 ID（Discord 設定 → 進階 → 開發者模式，然後在自己頭像右鍵／長按 →「複製使用者 ID」），按「傳測試訊息」確認收得到。

bot 只用 Discord 的 REST API 發私訊，不需要一直連線，也不會讀任何訊息。

## 使用

1. 手機打開網址，用計中帳號＋密碼登入（伺服器會真的去學校網站登入一次來確認；失敗 5 次會鎖 15 分鐘）。可以「加入主畫面」當 App 用。
2. 「設定」：填 Discord 使用者 ID、預訂人數（羽球等場地至少 2 人）。
3. 「機器人」分頁：新增濾網、選場地、在週表格點時段，打開「啟用」。要伺服器找到就先幫你填好，就把「找到就先準備」開著。
4. 收到 Discord 私訊「已填到學校確認頁」→ 點「打開確認頁」→ 看截圖 →「確認送出」或「取消」。
5. 「訂單」分頁 →「線上付款」會開學校的付款頁（第一次要在手機上登入學校網站）。

同一個人同時只會有一筆預訂在處理；學校規定一張訂單一面場地，要訂多面時會一面確認完再準備下一面。

## 管理

```bash
.venv/bin/python manage.py show                 # 目前設定與使用者
.venv/bin/python manage.py allow <帳號>          # 也可以在網站「設定」裡加
.venv/bin/python manage.py deny <帳號>           # 移出名單並登出
.venv/bin/python manage.py set-scan-account <帳號>  # 換一個帳號負責共用掃描
```

只有管理者能改掃描間隔、手動掃描、更新歷史熱門度、管理名單。

## 資料（都在 `data/`，權限 700，不進 git）

| 位置 | 內容 |
|---|---|
| `config.json` | 允許名單、管理者、掃描帳號、Discord bot token、公開網址、掃描間隔 |
| `secret.key` | 加密密碼用的金鑰（也可改用環境變數 `NTU_BOOKING_KEY`） |
| `web_sessions.json` | 這個網站的登入（只存 token 的雜湊） |
| `users/<帳號>/` | `credentials.json`（加密的密碼）、`session.json`（學校網站的 cookie）、`robot.json`（濾網、人數、Discord ID）、`hits.json`、`orders.json` |
| `search_cache.json`、`history_stats.json` | 共用的時段快取與歷史統計 |
| `snapshots/` | 抓到的學校網頁，除錯用（含確認頁） |
| `logs/` | `run.sh` 的伺服器與 tunnel 紀錄 |

## 本機開發測試（Windows）

雙擊 `start.bat`（或 `.venv\Scripts\python.exe server.py`），先 `manage.py set-admin <你的帳號>`，再開 <http://127.0.0.1:8765>。本機沒裝 Playwright 的 Chromium 時會改用 Edge（一樣是 headless）。

原本的單機桌面版保留在 git tag `desktop-v1`。

## 檔案結構

| 檔案 | 用途 |
|---|---|
| `server.py` | 網頁伺服器與 API：登入、每人的資料、機器人與預訂的串接 |
| `config.py` | `data/` 的位置與 `config.json` |
| `session.py` | 每個人的學校網站登入（`Account`：計中 SSO、cookie、每帳號一把鎖、自動重新登入） |
| `credentials.py` | 密碼加密儲存（Fernet） |
| `scanner.py` | 讀取網站時段表、解析、快取；搜尋與機器人共用 |
| `robot.py` | 共用的背景掃描、每人的濾網與清單 |
| `booker.py` | 準備預訂：headless 填到確認頁、等手機確認、送出並核對訂單 |
| `notify.py` | Discord 私訊 |
| `orders.py` | 讀取「常租紀錄」的訂單與繳費狀態 |
| `history.py` | 歷史熱門度統計（也可單獨執行：`python history.py <帳號> A:2 2026-09-07`） |
| `manage.py` | 命令列管理 |
| `deploy/` | `setup.sh`、`run.sh`（看門狗）、`env.example` |
| `static/index.html` | 操作介面（手機優先） |
