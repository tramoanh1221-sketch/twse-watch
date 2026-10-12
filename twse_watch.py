"""
台股每日追蹤小工具（資料來源：FinMind API, https://finmindtrade.com）

用法：
  1. pip install requests
  2. （建議但非必要）到 https://finmindtrade.com/analysis/#/account/login 免費註冊、
     驗證信箱後取得一個 token，用來提高每小時呼叫次數上限。拿到後執行：
        export FINMIND_TOKEN="你的token"
     （寫進 shell 設定檔或 .env 都可以，重點是不要把 token 直接寫在程式碼或貼到對話裡）
  3. 把下面的 WATCHLIST 改成你要追蹤的股票代碼
  4. 每天執行一次： python3 twse_watch.py
     可以用 cron / 排程器設成每天自動跑一次，每次執行的結果會附加記錄到 log.jsonl

這個工具只做「資料整理與描述」，不會告訴你要不要買賣，判斷請自行斟酌，
也請自行核對公開資訊，不保證資料完全正確或即時。
"""

import json
import os
import datetime
from pathlib import Path

import requests

# ---- 在這裡填入你要追蹤的股票代碼 ----
WATCHLIST = ["2330", "2317", "2454"]

API = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")
TIMEOUT = 20
LOG_FILE = Path(__file__).parent / "log.jsonl"


def fetch(dataset, data_id, start_date, end_date=None):
    params = {"dataset": dataset, "data_id": data_id, "start_date": start_date}
    if end_date:
        params["end_date"] = end_date
    if TOKEN:
        params["token"] = TOKEN
    resp = requests.get(API, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") not in (200, "200", None) and payload.get("msg") not in ("success", None):
        raise RuntimeError(f"{dataset} 回應異常：{payload.get('msg')}")
    return payload.get("data", [])


def pivot_by_date(rows):
    """把 FinMind 的長格式 [{date, type, value}, ...] 轉成 {date: {type: value}}"""
    out = {}
    for r in rows:
        out.setdefault(r["date"], {})[r["type"]] = r["value"]
    return out


def parse_date(s):
    return datetime.date.fromisoformat(s)


def find_year_ago(periods_desc, latest_date):
    """periods_desc: [(date_str, dict), ...] 由新到舊排序，找出跟 latest 差約一年的那期"""
    target = latest_date.replace(year=latest_date.year - 1)
    best = None
    best_gap = None
    for date_str, vals in periods_desc:
        d = parse_date(date_str)
        gap = abs((d - target).days)
        if gap <= 20 and (best_gap is None or gap < best_gap):
            best, best_gap = vals, gap
    return best


def fmt_pct(v, digits=1):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{digits}f}%"


def fmt_num(v, digits=1):
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def pct(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator * 100


def safe_fetch(label, dataset, code, start_date):
    try:
        return fetch(dataset, code, start_date)
    except Exception as e:
        print(f"  ! {label} 抓取失敗：{e}")
        return []


def fetch_name(code):
    try:
        rows = fetch("TaiwanStockInfo", code, "2020-01-01")
        return rows[0]["stock_name"] if rows else None
    except Exception:
        return None


def analyze_stock(code, today):
    report = {"code": code, "name": fetch_name(code)}

    # ---- 量與價：抓約 14 個月，可算 5/20 日均量、20/60 日均線、年度高低點 ----
    price_rows = safe_fetch("股價", "TaiwanStockPrice", code, (today - datetime.timedelta(days=420)).isoformat())
    price_rows = sorted(price_rows, key=lambda r: r["date"])
    if price_rows:
        latest = price_rows[-1]
        close = latest.get("close")
        volume = latest.get("Trading_Volume")
        report["date"] = latest["date"]
        report["close"] = close
        report["volume"] = volume

        closes = [r["close"] for r in price_rows if r.get("close") is not None]
        vols = [r["Trading_Volume"] for r in price_rows if r.get("Trading_Volume") is not None]
        year_rows = [r for r in price_rows if parse_date(r["date"]) >= today - datetime.timedelta(days=365)]
        year_closes = [r["close"] for r in year_rows if r.get("close") is not None]

        if len(price_rows) >= 2:
            prev_close = price_rows[-2].get("close")
            report["price_change_pct"] = pct(close - prev_close, prev_close) if prev_close else None
        if len(vols) >= 5:
            report["vol_ratio_5"] = volume / (sum(vols[-5:]) / 5) if volume else None
        if len(vols) >= 20:
            report["vol_ratio_20"] = volume / (sum(vols[-20:]) / 20) if volume else None
        if len(closes) >= 20:
            report["dev_ma20"] = pct(close - sum(closes[-20:]) / 20, sum(closes[-20:]) / 20)
        if len(closes) >= 60:
            report["dev_ma60"] = pct(close - sum(closes[-60:]) / 60, sum(closes[-60:]) / 60)
        if year_closes:
            y_high, y_low = max(year_closes), min(year_closes)
            if y_high > y_low:
                report["range_pos"] = pct(close - y_low, y_high - y_low)

    # ---- 財報：抓兩年，找去年同期算年增率 ----
    fin_rows = safe_fetch("綜合損益表", "TaiwanStockFinancialStatements", code, (today - datetime.timedelta(days=730)).isoformat())
    fin_periods = sorted(pivot_by_date(fin_rows).items(), key=lambda kv: kv[0], reverse=True)
    if fin_periods:
        latest_date_str, latest_fin = fin_periods[0]
        report["fin_period"] = latest_date_str
        revenue = latest_fin.get("Revenue")
        gross = latest_fin.get("GrossProfit")
        op_income = latest_fin.get("OperatingIncome")
        net_income = latest_fin.get("IncomeAfterTaxes")
        eps = latest_fin.get("EPS")
        report["revenue"] = revenue
        report["gross_margin"] = pct(gross, revenue)
        report["op_margin"] = pct(op_income, revenue)
        report["net_margin"] = pct(net_income, revenue)
        report["eps"] = eps

        prev_year_fin = find_year_ago(fin_periods, parse_date(latest_date_str))
        if prev_year_fin and prev_year_fin.get("Revenue"):
            report["yoy"] = pct(revenue - prev_year_fin["Revenue"], prev_year_fin["Revenue"])

        # ---- 資產負債表：抓最近一筆算 ROE、負債比率 ----
        bal_rows = safe_fetch("資產負債表", "TaiwanStockBalanceSheet", code, (today - datetime.timedelta(days=200)).isoformat())
        bal_periods = sorted(pivot_by_date(bal_rows).items(), key=lambda kv: kv[0], reverse=True)
        if bal_periods:
            _, latest_bal = bal_periods[0]
            total_assets = latest_bal.get("TotalAssets")
            liabilities = latest_bal.get("Liabilities")
            equity_parent = latest_bal.get("EquityAttributableToOwnersOfParent")
            report["debt_ratio"] = pct(liabilities, total_assets)
            if net_income is not None and equity_parent:
                report["roe"] = pct(net_income, equity_parent)

        # ---- 現金流量表：算現金流/淨利比 ----
        cf_rows = safe_fetch("現金流量表", "TaiwanStockCashFlowsStatement", code, (today - datetime.timedelta(days=200)).isoformat())
        cf_periods = sorted(pivot_by_date(cf_rows).items(), key=lambda kv: kv[0], reverse=True)
        if cf_periods and net_income:
            _, latest_cf = cf_periods[0]
            cfo = latest_cf.get("NetCashInflowFromOperatingActivities")
            if cfo is not None:
                report["cf_ratio"] = cfo / net_income

    return report


def build_flags(r):
    flags = []
    if r.get("yoy") is not None:
        if r["yoy"] > 20:
            flags.append(("pos", f"營收年增 {fmt_pct(r['yoy'])}，成長強勁"))
        elif r["yoy"] < 0:
            flags.append(("warn", f"營收年增 {fmt_pct(r['yoy'])}，呈現衰退"))
    if r.get("debt_ratio") is not None and r["debt_ratio"] > 60:
        flags.append(("warn", f"負債比率偏高（{fmt_num(r['debt_ratio'])}%）"))
    if r.get("cf_ratio") is not None and r["cf_ratio"] < 0.8:
        flags.append(("warn", f"現金流／淨利比偏低（{r['cf_ratio']:.2f}）"))
    if r.get("roe") is not None and r["roe"] >= 15:
        flags.append(("pos", f"ROE {fmt_num(r['roe'])}%，股東權益報酬率不錯"))
    vr = r.get("vol_ratio_20") or r.get("vol_ratio_5")
    pc = r.get("price_change_pct")
    if vr is not None and pc is not None:
        vol_up, vol_down = vr >= 1.2, vr <= 0.8
        price_up, price_down = pc > 0.3, pc < -0.3
        if vol_up and price_up:
            flags.append(("pos", "量增價漲，買盤較積極"))
        elif vol_up and price_down:
            flags.append(("warn", "量增價跌，賣壓偏重"))
        elif vol_down and price_down:
            flags.append(("neutral", "量縮價跌，賣壓減緩但動能不足"))
        elif vol_down and price_up:
            flags.append(("neutral", "量縮價漲，追價意願不高、支撐仍在"))
    if r.get("range_pos") is not None:
        if r["range_pos"] >= 85:
            flags.append(("warn", "股價接近近一年高點，留意追高風險"))
        elif r["range_pos"] <= 15:
            flags.append(("neutral", "股價接近近一年低點"))
    if r.get("dev_ma20") is not None:
        if r["dev_ma20"] < -5:
            flags.append(("warn", f"跌破 20 日均線 {fmt_pct(r['dev_ma20'])}，短線偏弱"))
        elif r["dev_ma20"] > 5:
            flags.append(("pos", f"站上 20 日均線 {fmt_pct(r['dev_ma20'])}，短線偏強"))
    return flags


def print_report(r):
    print("\n" + "=" * 46)
    print(f"{r['code']}  {r.get('name') or ''}")
    if r.get("date"):
        print(f"  {r['date']} 收盤 {fmt_num(r.get('close'), 2)}　成交量 {fmt_num(r.get('volume'), 0)}　較前日 {fmt_pct(r.get('price_change_pct'))}")
        print(f"  量比(5日) {r.get('vol_ratio_5') and round(r['vol_ratio_5'], 2)}　量比(20日) {r.get('vol_ratio_20') and round(r['vol_ratio_20'], 2)}")
        print(f"  相對20日均線 {fmt_pct(r.get('dev_ma20'))}　相對60日均線 {fmt_pct(r.get('dev_ma60'))}　年度區間位置 {fmt_pct(r.get('range_pos'), 0)}")
    else:
        print("  今日股價資料抓取失敗")
    if r.get("fin_period"):
        print(f"  財報期間 {r['fin_period']}　營收年增 {fmt_pct(r.get('yoy'))}")
        print(f"  毛利率 {fmt_pct(r.get('gross_margin'))}　營業利益率 {fmt_pct(r.get('op_margin'))}　淨利率 {fmt_pct(r.get('net_margin'))}")
        print(f"  EPS {fmt_num(r.get('eps'), 2)}　ROE {fmt_pct(r.get('roe'))}　負債比率 {fmt_pct(r.get('debt_ratio'))}")
    else:
        print("  財報資料抓取失敗或查無資料")
    flags = build_flags(r)
    if flags:
        for tone, text in flags:
            print(f"  · {text}")
    else:
        print("  · 尚無明顯訊號，或資料不足以判斷")


def main():
    today = datetime.date.today()
    print(f"=== 台股每日追蹤｜{today.isoformat()} ===")
    if not TOKEN:
        print("（目前未設定 FINMIND_TOKEN，使用免費額度，股票多或跑太頻繁可能會被限流）")

    reports = []
    for code in WATCHLIST:
        r = analyze_stock(code, today)
        reports.append(r)
        print_report(r)

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run_at": datetime.datetime.now().isoformat(), "reports": reports}, ensure_ascii=False) + "\n")
    print(f"\n本次結果已附加記錄到 {LOG_FILE}")
    print("（這個工具只整理資料，不構成投資建議，請自行核對公開資訊並謹慎判斷）")


if __name__ == "__main__":
    main()
