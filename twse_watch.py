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

內建「財報探測器」（build_red_flags）：用應收帳款/存貨成長是否超過營收、
毛利率單期波動、現金流與淨利是否持續背離、負債比率是否快速上升等常見會計
紅旗規則，標出財報裡值得多看一眼的地方。

這個工具只做「資料整理與描述」，不會告訴你要不要買賣，判斷請自行斟酌，
也請自行核對公開資訊，不保證資料完全正確或即時。
"""

import json
import os
import datetime
import webbrowser
from pathlib import Path

import requests

# ---- 在這裡填入你要追蹤的股票代碼 ----
WATCHLIST = ["2330", "2317", "2454"]

# ---- 每次執行後是否自動用瀏覽器打開報表 ----
OPEN_IN_BROWSER = True

API = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")
TIMEOUT = 20
LOG_FILE = Path(__file__).parent / "log.jsonl"
REPORT_FILE = Path(__file__).parent / "report.html"


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

        prev_fin = fin_periods[1][1] if len(fin_periods) >= 2 else None
        if prev_fin and prev_fin.get("Revenue"):
            report["revenue_qoq"] = pct(revenue - prev_fin["Revenue"], prev_fin["Revenue"])
        if prev_fin and prev_fin.get("GrossProfit") and prev_fin.get("Revenue"):
            prev_gross_margin = pct(prev_fin["GrossProfit"], prev_fin["Revenue"])
            if report.get("gross_margin") is not None and prev_gross_margin is not None:
                report["gross_margin_delta"] = report["gross_margin"] - prev_gross_margin

        # ---- 資產負債表：抓兩年，算 ROE、負債比率，以及應收/存貨相對營收的成長差 ----
        bal_rows = safe_fetch("資產負債表", "TaiwanStockBalanceSheet", code, (today - datetime.timedelta(days=730)).isoformat())
        bal_periods = sorted(pivot_by_date(bal_rows).items(), key=lambda kv: kv[0], reverse=True)
        if bal_periods:
            _, latest_bal = bal_periods[0]
            total_assets = latest_bal.get("TotalAssets")
            liabilities = latest_bal.get("Liabilities")
            equity_parent = latest_bal.get("EquityAttributableToOwnersOfParent")
            report["debt_ratio"] = pct(liabilities, total_assets)
            if net_income is not None and equity_parent:
                report["roe"] = pct(net_income, equity_parent)

            if len(bal_periods) >= 2:
                _, prev_bal = bal_periods[1]
                prev_debt_ratio = pct(prev_bal.get("Liabilities"), prev_bal.get("TotalAssets"))
                if report.get("debt_ratio") is not None and prev_debt_ratio is not None:
                    report["debt_ratio_delta"] = report["debt_ratio"] - prev_debt_ratio

                ar, prev_ar = latest_bal.get("AccountsReceivableNet"), prev_bal.get("AccountsReceivableNet")
                inv, prev_inv = latest_bal.get("Inventories"), prev_bal.get("Inventories")
                rev_qoq = report.get("revenue_qoq")
                if ar is not None and prev_ar and rev_qoq is not None:
                    report["ar_vs_revenue_gap"] = pct(ar - prev_ar, prev_ar) - rev_qoq
                if inv is not None and prev_inv and rev_qoq is not None:
                    report["inv_vs_revenue_gap"] = pct(inv - prev_inv, prev_inv) - rev_qoq

        # ---- 現金流量表：抓兩年，算現金流/淨利比與趨勢 ----
        cf_rows = safe_fetch("現金流量表", "TaiwanStockCashFlowsStatement", code, (today - datetime.timedelta(days=730)).isoformat())
        cf_periods = sorted(pivot_by_date(cf_rows).items(), key=lambda kv: kv[0], reverse=True)
        if cf_periods and net_income:
            _, latest_cf = cf_periods[0]
            cfo = latest_cf.get("NetCashInflowFromOperatingActivities")
            if cfo is not None:
                report["cf_ratio"] = cfo / net_income
            if len(cf_periods) >= 2 and prev_fin and prev_fin.get("IncomeAfterTaxes"):
                _, prev_cf = cf_periods[1]
                prev_cfo = prev_cf.get("NetCashInflowFromOperatingActivities")
                if prev_cfo is not None:
                    report["cf_ratio_prev"] = prev_cfo / prev_fin["IncomeAfterTaxes"]

    return report


def build_red_flags(r):
    """財報探測器：常見的會計/財務紅旗檢查，只做描述，不是投資建議。"""
    flags = []
    if r.get("debt_ratio") is not None and r["debt_ratio"] > 60:
        flags.append(("warn", f"負債比率偏高（{fmt_num(r['debt_ratio'])}%）"))
    if r.get("debt_ratio_delta") is not None and r["debt_ratio_delta"] > 5:
        flags.append(("warn", f"負債比率較上一期上升 {r['debt_ratio_delta']:.1f} 個百分點，留意財務結構變化"))

    if r.get("cf_ratio") is not None and r.get("cf_ratio_prev") is not None \
            and r["cf_ratio"] < 0.8 and r["cf_ratio_prev"] < 0.8:
        flags.append(("warn", f"連續兩期現金流／淨利比皆偏低（{r['cf_ratio_prev']:.2f} → {r['cf_ratio']:.2f}），獲利品質值得留意"))
    elif r.get("cf_ratio") is not None and r["cf_ratio"] < 0.8:
        flags.append(("warn", f"現金流／淨利比偏低（{r['cf_ratio']:.2f}）"))

    if r.get("ar_vs_revenue_gap") is not None and r["ar_vs_revenue_gap"] > 10:
        flags.append(("warn", f"應收帳款成長速度比營收快 {r['ar_vs_revenue_gap']:.1f} 個百分點，留意收款狀況"))
    if r.get("inv_vs_revenue_gap") is not None and r["inv_vs_revenue_gap"] > 10:
        flags.append(("warn", f"存貨成長速度比營收快 {r['inv_vs_revenue_gap']:.1f} 個百分點，留意去化狀況"))

    if r.get("gross_margin_delta") is not None:
        if r["gross_margin_delta"] < -5:
            flags.append(("warn", f"毛利率較上一期下滑 {abs(r['gross_margin_delta']):.1f} 個百分點，留意成本或定價壓力"))
        elif r["gross_margin_delta"] > 8:
            flags.append(("neutral", f"毛利率較上一期上升 {r['gross_margin_delta']:.1f} 個百分點，波動較大，建議了解原因"))
    return flags


def build_flags(r):
    flags = []
    if r.get("yoy") is not None:
        if r["yoy"] > 20:
            flags.append(("pos", f"營收年增 {fmt_pct(r['yoy'])}，成長強勁"))
        elif r["yoy"] < 0:
            flags.append(("warn", f"營收年增 {fmt_pct(r['yoy'])}，呈現衰退"))
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

    red_flags = build_red_flags(r)
    if red_flags:
        print("  [財報探測器]")
        for tone, text in red_flags:
            print(f"  ⚠ {text}")


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def pct_class(v):
    if v is None or v == 0:
        return ""
    return "up" if v > 0 else "down"


DETAIL_FIELDS = [
    ("date", "資料日期", None), ("close", "收盤價", 2), ("volume", "成交量", 0),
    ("fin_period", "財報期間", None), ("revenue", "營業收入", 0),
    ("gross_margin", "毛利率(%)", 1), ("op_margin", "營業利益率(%)", 1),
    ("net_margin", "淨利率(%)", 1), ("eps", "EPS", 2), ("roe", "ROE(%)", 1),
    ("debt_ratio", "負債比率(%)", 1), ("cf_ratio", "現金流/淨利比", 2),
]


def render_stock_card(r):
    flags = build_flags(r)
    flags_html = "".join(
        f'<div class="flag {tone}"><span>—</span><span>{esc(text)}</span></div>'
        for tone, text in flags
    ) or '<div class="flag neutral"><span>—</span><span>尚無明顯訊號，或資料不足以判斷</span></div>'

    if r.get("date"):
        tiles_html = f'''
          <div class="tile">
            <div class="tlabel">財報｜營收年增</div>
            <div class="tnum {pct_class(r.get('yoy'))}">{fmt_pct(r.get('yoy'))}</div>
            <div class="tsub">ROE {fmt_pct(r.get('roe'))}・負債比 {fmt_pct(r.get('debt_ratio'))}</div>
          </div>
          <div class="tile">
            <div class="tlabel">量｜量比(20日)</div>
            <div class="tnum">{round(r['vol_ratio_20'], 2) if r.get('vol_ratio_20') is not None else "—"}</div>
            <div class="tsub">今日量 {fmt_num(r.get('volume'), 0)}</div>
          </div>
          <div class="tile">
            <div class="tlabel">價｜較前日</div>
            <div class="tnum {pct_class(r.get('price_change_pct'))}">{fmt_pct(r.get('price_change_pct'))}</div>
            <div class="tsub">年度區間位置 {fmt_pct(r.get('range_pos'), 0)}</div>
          </div>
        '''
        meta_html = f'<div class="meta">{esc(r.get("date"))}　收盤 {fmt_num(r.get("close"), 2)}　財報期間 {esc(r.get("fin_period") or "—")}</div>'
    else:
        tiles_html = ""
        meta_html = '<div class="meta">今日資料抓取失敗</div>'

    rows = "".join(
        f'<tr><td class="l">{esc(label)}</td><td>{esc(fmt_num(r.get(key), digits)) if digits is not None else esc(r.get(key) or "—")}</td></tr>'
        for key, label, digits in DETAIL_FIELDS
    )

    red_flags = build_red_flags(r)
    red_flags_html = ""
    if red_flags:
        items = "".join(f'<div class="flag warn"><span>⚠</span><span>{esc(text)}</span></div>' for _, text in red_flags)
        red_flags_html = f'<div class="redflag-box"><div class="redflag-title">財報探測器</div><div class="flags">{items}</div></div>'

    return f'''
    <div class="card">
      <div class="card-head">
        <span class="name">{esc(r.get("name") or "")}</span>
        <span class="code">{esc(r["code"])}</span>
      </div>
      {meta_html}
      {f'<div class="tiles">{tiles_html}</div>' if tiles_html else ""}
      <div class="flags">{flags_html}</div>
      {red_flags_html}
      <details>
        <summary>完整數據</summary>
        <table>{rows}</table>
      </details>
    </div>
    '''


def render_html(reports, generated_at):
    cards = "".join(render_stock_card(r) for r in reports)
    return f'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>台股每日追蹤報表</title>
<style>
  :root{{
    --bg:#F3F5F8; --surface:#FFFFFF; --surface-muted:#EEF1F5; --border:#DCE2EA;
    --ink:#131A26; --muted:#5B6576;
    --up:#D92B2B; --down:#1C8A4B; --warn:#B45309; --pos:#0B5FAE;
    --warn-soft:#FBF0DF; --pos-soft:#E3EEF9; --neutral-soft:#EEF1F5;
    --shadow: 0 1px 2px rgba(19,26,38,0.06), 0 10px 26px -14px rgba(19,26,38,0.20);
  }}
  @media (prefers-color-scheme: dark){{
    :root{{
      --bg:#0F1319; --surface:#171C24; --surface-muted:#1E2430; --border:#2B323E;
      --ink:#E8EBF0; --muted:#8E97A6;
      --up:#F0605F; --down:#3FCB80; --warn:#E3A650; --pos:#5CA6E8;
      --warn-soft:#3A2C16; --pos-soft:#1B2C3F; --neutral-soft:#1E2430;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 10px 26px -14px rgba(0,0,0,0.5);
    }}
  }}
  *{{box-sizing:border-box;}}
  body{{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei",sans-serif;
    padding:32px 20px 56px;
  }}
  .wrap{{max-width:800px;margin:0 auto;}}
  h1{{font-size:1.6rem;font-weight:700;margin:0 0 4px;}}
  .sub{{color:var(--muted);font-size:0.9rem;margin:0 0 18px;}}
  .disclaimer{{
    background:var(--surface-muted);border:1px solid var(--border);border-radius:12px;
    padding:12px 14px;margin-bottom:22px;font-size:0.82rem;color:var(--muted);line-height:1.6;
  }}
  .card{{
    background:var(--surface);border:1px solid var(--border);border-radius:16px;
    padding:18px;margin-bottom:16px;box-shadow:var(--shadow);
  }}
  .card-head{{display:flex;align-items:baseline;gap:8px;margin-bottom:4px;}}
  .name{{font-size:1.1rem;font-weight:700;}}
  .code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);font-size:0.85rem;}}
  .meta{{font-size:0.8rem;color:var(--muted);margin-bottom:12px;}}
  .tiles{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;}}
  .tile{{background:var(--surface-muted);border:1px solid var(--border);border-radius:12px;padding:10px 12px;}}
  .tlabel{{font-size:0.68rem;color:var(--muted);font-weight:600;margin-bottom:4px;}}
  .tnum{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:1.05rem;font-weight:700;}}
  .tsub{{font-size:0.7rem;color:var(--muted);margin-top:2px;}}
  .up{{color:var(--up);}} .down{{color:var(--down);}}
  .flags{{display:flex;flex-direction:column;gap:6px;margin-bottom:6px;}}
  .flag{{display:flex;gap:6px;font-size:0.82rem;padding:6px 10px;border-radius:9px;}}
  .flag.pos{{background:var(--pos-soft);color:var(--pos);}}
  .flag.warn{{background:var(--warn-soft);color:var(--warn);}}
  .flag.neutral{{background:var(--neutral-soft);color:var(--muted);}}
  .redflag-box{{border:1px dashed var(--warn);border-radius:12px;padding:10px 12px;margin-top:6px;margin-bottom:6px;}}
  .redflag-title{{font-size:0.74rem;font-weight:700;color:var(--warn);margin-bottom:6px;}}
  details{{margin-top:8px;font-size:0.82rem;}}
  summary{{cursor:pointer;color:var(--pos);font-weight:600;}}
  table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:0.82rem;}}
  td{{padding:4px 6px;border-bottom:1px solid var(--border);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}}
  td.l{{font-family:inherit;color:var(--muted);}}
  footer{{margin-top:12px;font-size:0.76rem;color:var(--muted);text-align:center;}}
  @media (max-width:480px){{ .tiles{{grid-template-columns:1fr;}} }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>台股每日追蹤報表</h1>
    <p class="sub">產生時間：{esc(generated_at)}</p>
    <div class="disclaimer">這份報表只做資料整理與描述，不構成投資建議，請自行核對公開資訊並謹慎判斷。</div>
    {cards}
    <footer>資料來源：FinMind API，由本機 twse_watch.py 產生。</footer>
  </div>
</body>
</html>'''


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

    now = datetime.datetime.now()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run_at": now.isoformat(), "reports": reports}, ensure_ascii=False) + "\n")
    print(f"\n本次結果已附加記錄到 {LOG_FILE}")

    REPORT_FILE.write_text(render_html(reports, now.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    print(f"HTML 報表已產生：{REPORT_FILE}")
    if OPEN_IN_BROWSER:
        webbrowser.open(REPORT_FILE.as_uri())

    print("（這個工具只整理資料，不構成投資建議，請自行核對公開資訊並謹慎判斷）")


if __name__ == "__main__":
    main()
