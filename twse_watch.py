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
      <p class="more-link"><a href="analysis_{esc(r["code"])}.html">歷年財報深度分析 →</a></p>
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
  .more-link{{margin:10px 0 0;font-size:0.82rem;}}
  .more-link a{{color:var(--pos);text-decoration:none;font-weight:600;}}
  .more-link a:hover{{text-decoration:underline;}}
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


QUARTER_DAYS = 91  # 簡化：用 91 天代表一季，不是精確日曆天數


def fetch_history_periods(code, today, years=8):
    """抓多年財報＋資產負債表＋現金流量表，逐季組成時間序列，
    用來畫趨勢圖、杜邦ROE拆解，以及健診用的現金流/應收/存貨數字。"""
    start = (today - datetime.timedelta(days=years * 365)).isoformat()
    fin_rows = safe_fetch("歷年損益表", "TaiwanStockFinancialStatements", code, start)
    bal_rows = safe_fetch("歷年資產負債表", "TaiwanStockBalanceSheet", code, start)
    cf_rows = safe_fetch("歷年現金流量表", "TaiwanStockCashFlowsStatement", code, start)
    fin_by_date = pivot_by_date(fin_rows)
    bal_by_date = pivot_by_date(bal_rows)
    cf_by_date = pivot_by_date(cf_rows)

    periods = []
    for date_str in sorted(fin_by_date.keys()):
        f = fin_by_date[date_str]
        b = bal_by_date.get(date_str, {})
        c = cf_by_date.get(date_str, {})
        revenue = f.get("Revenue")
        cogs = f.get("CostOfGoodsSold")
        gross_profit = f.get("GrossProfit")
        op_income = f.get("OperatingIncome")
        pretax_income = f.get("PreTaxIncome")
        net_income = f.get("IncomeAfterTaxes")
        total_assets = b.get("TotalAssets")
        equity_parent = b.get("EquityAttributableToOwnersOfParent")
        ar = b.get("AccountsReceivableNet")
        inventory = b.get("Inventories")
        cfo = c.get("NetCashInflowFromOperatingActivities")
        capex = c.get("PropertyAndPlantAndEquipment")  # 投資活動的資本支出，本身已是負值

        p = {
            "date": date_str,
            "revenue": revenue,
            "gross_profit": gross_profit,
            "op_income": op_income,
            "pretax_income": pretax_income,
            "net_income": net_income,
            "gross_margin": pct(gross_profit, revenue),
            "op_margin": pct(op_income, revenue),
            "net_margin": pct(net_income, revenue),
            "eps": f.get("EPS"),
            "ar": ar, "inventory": inventory,
            "cfo": cfo, "capex": capex,
            "fcf": (cfo + capex) if (cfo is not None and capex is not None) else None,
        }
        if ar is not None and revenue:
            p["ar_days"] = ar / revenue * QUARTER_DAYS
        if inventory is not None and cogs:
            p["inv_days"] = inventory / cogs * QUARTER_DAYS
        if total_assets and equity_parent and revenue is not None and net_income is not None:
            p["asset_turnover"] = revenue / total_assets
            p["equity_multiplier"] = total_assets / equity_parent
            p["roe"] = pct(net_income, equity_parent)
        periods.append(p)
    return periods


def complete_years(periods):
    """只保留剛好有 4 季資料的完整年度，用於健診的『近5年』統計。"""
    years = {}
    for p in periods:
        y = p["date"][:4]
        yd = years.setdefault(y, {"revenue": 0, "net_income": 0, "cfo": 0, "capex": 0, "quarters": 0, "ok": True})
        for key in ("revenue", "net_income", "cfo", "capex"):
            v = p.get(key)
            if v is None:
                yd["ok"] = False
            else:
                yd[key] += v
        yd["quarters"] += 1

    out = []
    for y in sorted(years.keys()):
        d = years[y]
        if d["quarters"] == 4 and d["ok"]:
            out.append({
                "year": y, "revenue": d["revenue"], "net_income": d["net_income"],
                "cfo": d["cfo"], "fcf": d["cfo"] + d["capex"],
                "cfo_ratio": (d["cfo"] / d["net_income"]) if d["net_income"] else None,
            })
    return out


def red_flag_health_check(periods):
    """地雷股健診：參考財報狗公開的 6 項檢查邏輯，只做描述，不是投資建議。"""
    items = []
    years = complete_years(periods)[-5:]
    if years:
        fcf_vals = [y["fcf"] for y in years if y["fcf"] is not None]
        fcf_pos = sum(1 for v in fcf_vals if v > 0)
        items.append(("自由現金流近5年至少3年為正", fcf_pos >= 3, f"{fcf_pos}/{len(years)} 年為正"))
        if fcf_vals:
            avg_fcf = sum(fcf_vals) / len(fcf_vals)
            items.append(("自由現金流5年平均為正", avg_fcf > 0, fmt_num(avg_fcf, 0)))

        ratios = [y["cfo_ratio"] for y in years if y["cfo_ratio"] is not None]
        ratio_ok = sum(1 for v in ratios if v > 1.0)
        items.append(("營業現金流/淨利近5年至少3年>100%", ratio_ok >= 3, f"{ratio_ok}/{len(years)} 年>100%"))
        if ratios:
            avg_ratio = sum(ratios) / len(ratios)
            items.append(("營業現金流/淨利5年平均>100%", avg_ratio > 1.0, fmt_pct(avg_ratio * 100)))
    else:
        items.append(("自由現金流 / 現金流對淨利比（近5年）", None, "完整年度資料不足"))

    if periods:
        latest = periods[-1]
        prev_year = find_year_ago([(p["date"], p) for p in reversed(periods)], parse_date(latest["date"]))
        if prev_year and latest.get("ar_days") is not None and prev_year.get("ar_days") is not None:
            ok = latest["ar_days"] <= prev_year["ar_days"]
            items.append(("應收帳款週轉天數 ≤ 去年同期", ok, f"{latest['ar_days']:.1f} 天 vs 去年 {prev_year['ar_days']:.1f} 天"))
        else:
            items.append(("應收帳款週轉天數 ≤ 去年同期", None, "資料不足"))
        if prev_year and latest.get("inv_days") is not None and prev_year.get("inv_days") is not None:
            ok = latest["inv_days"] <= prev_year["inv_days"]
            items.append(("存貨週轉天數 ≤ 去年同期", ok, f"{latest['inv_days']:.1f} 天 vs 去年 {prev_year['inv_days']:.1f} 天"))
        else:
            items.append(("存貨週轉天數 ≤ 去年同期", None, "資料不足"))
    return items


def growth_health_check(code, periods, today):
    """成長股健診：參考財報狗公開的 5 項檢查邏輯，只做描述，不是投資建議。"""
    items = []
    rows = safe_fetch("月營收", "TaiwanStockMonthRevenue", code, (today - datetime.timedelta(days=800)).isoformat())
    monthly = sorted(rows, key=lambda r: (r["revenue_year"], r["revenue_month"]))
    by_ym = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in monthly}
    yoy_list = []
    for r in monthly:
        y, m = r["revenue_year"], r["revenue_month"]
        prev = by_ym.get((y - 1, m))
        if prev:
            yoy_list.append((f"{y}/{m:02d}", pct(r["revenue"] - prev, prev)))
    last3 = yoy_list[-3:]
    if len(last3) == 3:
        ok = all(v is not None and v > 0 for _, v in last3)
        detail = "、".join(f"{label} {fmt_pct(v)}" for label, v in last3)
        items.append(("月營收年增率連續3個月>0", ok, detail))
    else:
        items.append(("月營收年增率連續3個月>0", None, "資料不足"))

    if periods:
        latest = periods[-1]
        prev_year = find_year_ago([(p["date"], p) for p in reversed(periods)], parse_date(latest["date"]))
        checks = [
            ("gross_profit", "毛利年增率>0（本期 vs 去年同期）"),
            ("op_income", "營業利益年增率>0（本期 vs 去年同期）"),
            ("pretax_income", "稅前淨利年增率>0（本期 vs 去年同期）"),
            ("net_income", "稅後淨利年增率>0（本期 vs 去年同期）"),
        ]
        for key, label in checks:
            cur, prv = latest.get(key), (prev_year.get(key) if prev_year else None)
            if cur is not None and prv:
                yoy = pct(cur - prv, prv)
                items.append((label, yoy > 0, fmt_pct(yoy)))
            else:
                items.append((label, None, "資料不足"))
    return items


def annual_aggregate(periods):
    """把逐季資料依年度加總，算年度營收/淨利與年增率（簡化：直接加總各年已公布的季度）。"""
    years = {}
    for p in periods:
        y = p["date"][:4]
        yd = years.setdefault(y, {"revenue": 0, "net_income": 0, "quarters": 0, "has_rev": False, "has_ni": False})
        if p.get("revenue") is not None:
            yd["revenue"] += p["revenue"]
            yd["has_rev"] = True
        if p.get("net_income") is not None:
            yd["net_income"] += p["net_income"]
            yd["has_ni"] = True
        yd["quarters"] += 1

    sorted_keys = sorted(years.keys())
    # 最早的一年如果季數不足 4 季，通常是抓取區間邊界造成的不完整年度，直接捨棄避免誤導
    if len(sorted_keys) > 1 and years[sorted_keys[0]]["quarters"] < 4:
        sorted_keys = sorted_keys[1:]

    out = []
    prev_rev = None
    for y in sorted_keys:
        d = years[y]
        rev = d["revenue"] if d["has_rev"] else None
        yoy = pct(rev - prev_rev, prev_rev) if (prev_rev and rev is not None) else None
        out.append({
            "year": y, "revenue": rev,
            "net_income": d["net_income"] if d["has_ni"] else None,
            "quarters": d["quarters"], "yoy": yoy,
        })
        if rev is not None:
            prev_rev = rev
    return out


def render_health_panel(title, items):
    if not items:
        return ""
    passed = sum(1 for _, ok, _ in items if ok is True)
    determinable = sum(1 for _, ok, _ in items if ok is not None)
    rows = "".join(
        f'<tr><td class="l">{esc(label)}</td>'
        f'<td class="hc-mark {"ok" if ok is True else ("no" if ok is False else "unk")}">'
        f'{"✓" if ok is True else ("✗" if ok is False else "—")}</td>'
        f'<td class="hc-detail">{esc(detail)}</td></tr>'
        for label, ok, detail in items
    )
    return f'''
    <div class="hc-card">
      <div class="hc-head"><span class="hc-title">{esc(title)}</span>
        <span class="hc-score">{passed} / {determinable or len(items)} 項通過</span></div>
      <table class="hc-table">{rows}</table>
    </div>
    '''


def render_analysis_page(code, name, periods, annual, red_flag_items, growth_items):
    labels_q = [p["date"] for p in periods]
    gross = [p.get("gross_margin") for p in periods]
    op = [p.get("op_margin") for p in periods]
    net = [p.get("net_margin") for p in periods]
    roe = [p.get("roe") for p in periods]
    asset_turnover = [p.get("asset_turnover") for p in periods]
    equity_multiplier = [p.get("equity_multiplier") for p in periods]
    net_margin_only = [p.get("net_margin") for p in periods]

    labels_y = [a["year"] for a in annual]
    rev_y = [a["revenue"] for a in annual]
    ni_y = [a["net_income"] for a in annual]
    yoy_y = [a["yoy"] for a in annual]

    comp_rows = ""
    if len(periods) >= 1:
        latest = periods[-1]
        prev_year = find_year_ago(
            [(p["date"], p) for p in reversed(periods)], parse_date(latest["date"])
        )
        comp_fields = [
            ("revenue", "營業收入", 0), ("gross_margin", "毛利率(%)", 1),
            ("op_margin", "營業利益率(%)", 1), ("net_margin", "淨利率(%)", 1),
            ("eps", "EPS", 2), ("roe", "ROE(%)", 1),
        ]
        for key, label, digits in comp_fields:
            cur_v = latest.get(key)
            prev_v = prev_year.get(key) if prev_year else None
            delta = (cur_v - prev_v) if (cur_v is not None and prev_v is not None) else None
            comp_rows += (
                f'<tr><td class="l">{esc(label)}</td>'
                f'<td>{esc(fmt_num(cur_v, digits))}</td>'
                f'<td>{esc(fmt_num(prev_v, digits))}</td>'
                f'<td class="{pct_class(delta)}">{esc(fmt_num(delta, digits)) if delta is not None else "—"}</td></tr>'
            )

    return f'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(name or code)} 財報深度分析</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root{{
    --bg:#F3F5F8; --surface:#FFFFFF; --surface-muted:#EEF1F5; --border:#DCE2EA;
    --ink:#131A26; --muted:#5B6576; --up:#D92B2B; --down:#1C8A4B; --pos:#0B5FAE;
    --good:#1C8A4B; --bad:#D92B2B;
    --shadow: 0 1px 2px rgba(19,26,38,0.06), 0 10px 26px -14px rgba(19,26,38,0.20);
  }}
  @media (prefers-color-scheme: dark){{
    :root{{
      --bg:#0F1319; --surface:#171C24; --surface-muted:#1E2430; --border:#2B323E;
      --ink:#E8EBF0; --muted:#8E97A6; --up:#F0605F; --down:#3FCB80; --pos:#5CA6E8;
      --good:#3FCB80; --bad:#F0605F;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 10px 26px -14px rgba(0,0,0,0.5);
    }}
  }}
  *{{box-sizing:border-box;}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei",sans-serif;padding:32px 20px 56px;}}
  .wrap{{max-width:900px;margin:0 auto;}}
  a.back{{color:var(--pos);text-decoration:none;font-size:0.85rem;font-weight:600;}}
  h1{{font-size:1.5rem;font-weight:700;margin:10px 0 4px;}}
  .sub{{color:var(--muted);font-size:0.85rem;margin:0 0 18px;}}
  .panel{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:18px;margin-bottom:18px;box-shadow:var(--shadow);}}
  .panel h2{{font-size:1.02rem;font-weight:700;margin:0 0 4px;}}
  .panel .note{{font-size:0.76rem;color:var(--muted);margin:0 0 12px;}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
  canvas{{max-width:100%;}}
  table{{width:100%;border-collapse:collapse;font-size:0.85rem;}}
  td,th{{padding:6px 8px;border-bottom:1px solid var(--border);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;text-align:right;}}
  th{{color:var(--muted);font-size:0.76rem;font-weight:600;}}
  td.l,th.l{{font-family:inherit;text-align:left;color:var(--muted);}}
  .up{{color:var(--up);}} .down{{color:var(--down);}}
  .hc-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
  .hc-card{{background:var(--surface-muted);border:1px solid var(--border);border-radius:12px;padding:12px 14px;}}
  .hc-head{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;}}
  .hc-title{{font-size:0.88rem;font-weight:700;}}
  .hc-score{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.8rem;color:var(--muted);}}
  .hc-table{{font-size:0.76rem;}}
  .hc-table td{{padding:5px 4px;}}
  .hc-table td.l{{text-align:left;width:52%;}}
  .hc-mark{{text-align:center;font-weight:700;width:10%;font-family:inherit;}}
  .hc-mark.ok{{color:var(--good);}} .hc-mark.no{{color:var(--bad);}} .hc-mark.unk{{color:var(--muted);}}
  .hc-detail{{text-align:left;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}}
  footer{{margin-top:12px;font-size:0.76rem;color:var(--muted);text-align:center;}}
  @media (max-width:640px){{ .grid2{{grid-template-columns:1fr;}} .hc-grid{{grid-template-columns:1fr;}} }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="report.html">← 回到每日追蹤報表</a>
  <h1>{esc(name or "")} <span style="color:var(--muted);font-weight:400;">{esc(code)}</span></h1>
  <p class="sub">歷年財報深度分析・資料來源 FinMind・僅供資料整理與描述，不構成投資建議</p>

  <div class="panel">
    <h2>個股健診</h2>
    <p class="note">參考財報狗公開說明的健診邏輯重新計算，評分制、只做條件檢查，<strong>不是投資建議</strong>，各項門檻為常見經驗法則，不是絕對標準。</p>
    <div class="hc-grid">
      {render_health_panel("地雷股健診（避雷）", red_flag_items)}
      {render_health_panel("成長股健診", growth_items)}
    </div>
  </div>

  <div class="panel">
    <h2>年度營收與淨利</h2>
    <p class="note">長條為各年度營收／淨利加總（取已公布季度加總，非完整年度的用虛線標示季數不足）；折線為營收年增率。</p>
    <canvas id="chartAnnual" height="110"></canvas>
  </div>

  <div class="panel">
    <h2>三率趨勢（毛利率／營業利益率／淨利率）</h2>
    <p class="note">近 {len(periods)} 季，單季數字。</p>
    <canvas id="chartMargins" height="100"></canvas>
  </div>

  <div class="panel">
    <h2>杜邦 ROE 拆解</h2>
    <p class="note">ROE = 淨利率 × 總資產週轉率 × 權益乘數（簡化版，用期末資產/權益，非平均值）。</p>
    <div class="grid2">
      <div><canvas id="chartRoe" height="140"></canvas></div>
      <div><canvas id="chartTurnover" height="140"></canvas></div>
    </div>
  </div>

  <div class="panel">
    <h2>本期 vs 去年同期</h2>
    <table>
      <tr><th class="l">項目</th><th>本期</th><th>去年同期</th><th>差異</th></tr>
      {comp_rows}
    </table>
  </div>

  <footer>資料來源：FinMind API，由本機 twse_watch.py 產生，不構成投資建議。</footer>
</div>
<script>
  var labelsQ = {json.dumps(labels_q)};
  var gross = {json.dumps(gross)};
  var op = {json.dumps(op)};
  var net = {json.dumps(net)};
  var roe = {json.dumps(roe)};
  var assetTurnover = {json.dumps(asset_turnover)};
  var equityMultiplier = {json.dumps(equity_multiplier)};
  var labelsY = {json.dumps(labels_y)};
  var revY = {json.dumps(rev_y)};
  var niY = {json.dumps(ni_y)};
  var yoyY = {json.dumps(yoy_y)};

  function baseOpts(extra){{
    return Object.assign({{responsive:true, interaction:{{mode:'index',intersect:false}},
      plugins:{{legend:{{labels:{{boxWidth:12}}}}}}}}, extra || {{}});
  }}

  new Chart(document.getElementById('chartAnnual'), {{
    type: 'bar',
    data: {{ labels: labelsY, datasets: [
      {{ label:'年度營收', data: revY, backgroundColor:'#0B5FAE', yAxisID:'y' }},
      {{ label:'年度淨利', data: niY, backgroundColor:'#8FB7DD', yAxisID:'y' }},
      {{ label:'營收年增率(%)', data: yoyY, type:'line', borderColor:'#D92B2B', yAxisID:'y1', tension:0.25 }}
    ]}},
    options: baseOpts({{ scales:{{ y:{{position:'left'}}, y1:{{position:'right',grid:{{drawOnChartArea:false}}}} }} }})
  }});

  new Chart(document.getElementById('chartMargins'), {{
    type: 'line',
    data: {{ labels: labelsQ, datasets: [
      {{ label:'毛利率%', data: gross, borderColor:'#0B5FAE', tension:0.2 }},
      {{ label:'營業利益率%', data: op, borderColor:'#B45309', tension:0.2 }},
      {{ label:'淨利率%', data: net, borderColor:'#1C8A4B', tension:0.2 }}
    ]}},
    options: baseOpts()
  }});

  new Chart(document.getElementById('chartRoe'), {{
    type: 'line',
    data: {{ labels: labelsQ, datasets: [
      {{ label:'ROE%', data: roe, borderColor:'#0B5FAE', tension:0.2 }},
      {{ label:'淨利率%', data: net, borderColor:'#8E97A6', tension:0.2, borderDash:[4,3] }}
    ]}},
    options: baseOpts()
  }});

  new Chart(document.getElementById('chartTurnover'), {{
    type: 'line',
    data: {{ labels: labelsQ, datasets: [
      {{ label:'總資產週轉率(次)', data: assetTurnover, borderColor:'#0B5FAE', yAxisID:'y' }},
      {{ label:'權益乘數(倍)', data: equityMultiplier, borderColor:'#B45309', yAxisID:'y1' }}
    ]}},
    options: baseOpts({{ scales:{{ y:{{position:'left'}}, y1:{{position:'right',grid:{{drawOnChartArea:false}}}} }} }})
  }});
</script>
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

        print("  抓取歷年財報資料，產生深度分析頁面...")
        periods = fetch_history_periods(code, today)
        if periods:
            annual = annual_aggregate(periods)
            red_flag_items = red_flag_health_check(periods)
            growth_items = growth_health_check(code, periods, today)
            analysis_path = Path(__file__).parent / f"analysis_{code}.html"
            analysis_path.write_text(
                render_analysis_page(code, r.get("name"), periods, annual, red_flag_items, growth_items),
                encoding="utf-8",
            )
            print(f"  → {analysis_path}")

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
