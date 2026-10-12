# twse-watch

台股每日追蹤小工具：自動抓取指定股票的收盤價、成交量、財報數據，計算營收年增率、毛利率、ROE、負債比率、量比、均線偏離等指標，並給出描述性的觀察重點。

> 這個工具只做資料整理與描述，**不是投資建議**，不保證資料完全正確或即時，請自行核對公開資訊並謹慎判斷。

## 資料來源

[FinMind API](https://finmindtrade.com)（公開資料，免費額度即可使用；若要提高每小時呼叫次數上限，可以免費註冊取得 token）。

## 安裝

```bash
pip install -r requirements.txt
```

## 設定

1. 打開 `twse_watch.py`，把 `WATCHLIST` 改成你要追蹤的股票代碼：

   ```python
   WATCHLIST = ["2330", "2317", "2454"]
   ```

2.（建議，非必要）到 [FinMind 註冊頁面](https://finmindtrade.com/analysis/#/account/login) 免費註冊、驗證信箱後取得 token，提高每小時呼叫次數上限：

   ```bash
   export FINMIND_TOKEN="你的token"
   ```

   **不要把 token 寫進程式碼或提交到 git 裡。**

## 執行

```bash
python3 twse_watch.py
```

每次執行會把結果印在終端機，並附加記錄到本機的 `log.jsonl`（這個檔案已被 `.gitignore` 排除，不會被提交）。

可以用 cron（macOS/Linux）或工作排程器（Windows）設成每天自動跑一次，例如每個交易日下午 3 點：

```cron
0 15 * * 1-5 cd /path/to/twse-watch && /usr/bin/python3 twse_watch.py >> run.log 2>&1
```

## 這份報表在算什麼

- **財報面**：營收年增率、毛利率、營業利益率、淨利率、EPS、ROE、負債比率、現金流／淨利比
- **量能面**：量比（今日成交量 ÷ 5 日／20 日均量）
- **價格面**：股價相對 20 日／60 日均線的偏離、近一年高低點區間位置

每個指標後面附的文字（例如「營收年增強勁」「股價接近近一年高點」）都是**描述性的觀察**，不是買賣建議。

## 授權

沒有附加授權條款，使用前請先跟原作者確認可否使用／修改／散布。
