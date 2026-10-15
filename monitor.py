"""Hourly Tigerair KHH<->KIX price monitor (GitHub Actions edition).

1. Read one-way daily prices (untaxed) from the public daily-prices API.
2. Build every 5-6 day round trip inside the travel window, keep the TOP_N cheapest.
3. Get exact tax-inclusive prices from the booking site with a headed Chrome.
   If the booking site blocks us, fall back to daily price + known taxes (marked as estimated).
4. Append to data/history.csv and regenerate docs/index.html (served by GitHub Pages).
"""
import csv, html, json, sys, traceback, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quote as q  # noqa: E402

WINDOW_START, WINDOW_END = date(2026, 11, 5), date(2026, 12, 30)
TRIP_DAYS = (5, 6)          # inclusive of departure and return day
TOP_N = 6
LIMIT_PER_PERSON = 9000     # tax-inclusive round trip; rows at or under this are highlighted
# Per-person taxes seen on the booking site (2026-10-15), used only in estimate mode.
TAX_OUT, TAX_RET = 1100, 1785
HISTORY, REPORT = HERE / "data" / "history.csv", HERE / "docs" / "index.html"
API = ("https://api-cms.tigerairtw.com/api/app/book/daily-prices?origin={o}&destination={d}"
       "&userCurrency=TWD&pricingCurrency=TWD&since={s}&until={u}")
WD = "一二三四五六日"


def daily_prices(o, d):
    url = API.format(o=o, d=d, s=WINDOW_START, u=WINDOW_END)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)["data"]
    return {date.fromisoformat(x["date"]): x["amount"] for x in data if x["amount"]}


def candidates():
    out_p, ret_p = daily_prices(q.ORIGIN, q.DEST), daily_prices(q.DEST, q.ORIGIN)
    combos = []
    d = WINDOW_START
    while d <= WINDOW_END:
        for n in TRIP_DAYS:
            r = d + timedelta(days=n - 1)
            if r <= WINDOW_END and d in out_p and r in ret_p:
                combos.append((out_p[d], ret_p[r], d, r))
        d += timedelta(days=1)
    return sorted(combos, key=lambda c: c[0] + c[1])[:TOP_N]


def estimate(out_price, ret_price, dep, ret):
    per = out_price + ret_price + TAX_OUT + TAX_RET
    seats = {"flight": "", "dep": "", "seatsLeft": "?"}
    return {"dep": dep.isoformat(), "ret": ret.isoformat(), "estimated": True,
            "outbound": seats, "return": seats,
            "fare_per_adult": out_price + ret_price, "tax_per_adult": TAX_OUT + TAX_RET,
            "total_per_adult": per, "total_per_child": per,
            "total_all": per * (q.ADULTS + q.CHILDREN)}


def fetch_quotes(cands):
    results, blocked = [], False
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=False)
        page = b.new_context(locale="zh-TW", viewport={"width": 1366, "height": 900}).new_page()
        page.goto("https://www.tigerairtw.com/zh-TW/index", wait_until="networkidle", timeout=60000)
        for out_price, ret_price, dep, ret in cands:
            if not blocked:
                try:
                    r = q.quote(page, dep.isoformat(), ret.isoformat())
                    results.append(r if "error" not in r else estimate(out_price, ret_price, dep, ret) | {"note": r["error"]})
                    page.wait_for_timeout(3000)
                    continue
                except q.Blocked:
                    blocked = True
                    print("booking site blocked this runner; switching to estimate mode")
            results.append(estimate(out_price, ret_price, dep, ret))
        b.close()
    return results, blocked


def append_history(ts, results):
    HISTORY.parent.mkdir(exist_ok=True)
    new = not HISTORY.exists()
    with open(HISTORY, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at", "dep", "ret", "total_per_adult", "total_all", "fare_per_adult",
                        "tax_per_adult", "out_seats", "ret_seats", "estimated"])
        for r in results:
            w.writerow([ts, r["dep"], r["ret"], r["total_per_adult"], r["total_all"], r["fare_per_adult"],
                        r["tax_per_adult"], r["outbound"]["seatsLeft"], r["return"]["seatsLeft"],
                        int(r["estimated"])])


def read_history():
    if not HISTORY.exists():
        return []
    with open(HISTORY, encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f) if row["total_per_adult"]]


def fmt_d(s):
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}（{WD[d.weekday()]}）"


def write_report(ts, results, blocked, error=None):
    hist = read_history()
    runs = sorted({h["checked_at"] for h in hist})
    prev_run = runs[-2] if len(runs) >= 2 else None
    prev = {(h["dep"], h["ret"]): int(h["total_per_adult"]) for h in hist if h["checked_at"] == prev_run}
    low = {}
    for h in hist:
        k, v = (h["dep"], h["ret"]), int(h["total_per_adult"])
        low[k] = min(low.get(k, v), v)

    rows = []
    for r in sorted(results, key=lambda r: r["total_per_adult"]):
        k = (r["dep"], r["ret"])
        days = (date.fromisoformat(r["ret"]) - date.fromisoformat(r["dep"])).days + 1
        t = r["total_per_adult"]
        if k in prev and prev[k] != t:
            diff = t - prev[k]
            chg = f'<span class="{"up" if diff > 0 else "down"}">{"▲" if diff > 0 else "▼"}{abs(diff):,}</span>'
        else:
            chg = '<span class="same">—</span>'
        o, rt = r["outbound"], r["return"]
        sub = f'{days} 天｜{o["flight"]} {o["dep"][11:16]}・{rt["flight"]} {rt["dep"][11:16]}' if o["flight"] else f"{days} 天"
        tag = '<span class="tag">估算</span>' if r["estimated"] else ""
        rows.append(
            f'<tr class="{"hit" if t <= LIMIT_PER_PERSON else ""}">'
            f'<td>{fmt_d(r["dep"])} → {fmt_d(r["ret"])}<div class="sub">{sub}</div></td>'
            f'<td class="num">{r["fare_per_adult"]:,}</td><td class="num">{r["tax_per_adult"]:,}</td>'
            f'<td class="num strong">{t:,}{tag}</td><td class="num">{chg}</td>'
            f'<td class="num">{low.get(k, t):,}</td><td class="num strong">{r["total_all"]:,}</td>'
            f'<td class="num">{o["seatsLeft"]} / {rt["seatsLeft"]}</td></tr>')

    hits = [r for r in results if r["total_per_adult"] <= LIMIT_PER_PERSON]
    banner = (f'<div class="banner hit">有 {len(hits)} 組每人含稅 ≤ {LIMIT_PER_PERSON:,} 元！</div>' if hits else
              f'<div class="banner">目前沒有每人含稅 ≤ {LIMIT_PER_PERSON:,} 元的組合</div>')
    if blocked:
        banner += ('<div class="banner warn">訂票頁擋掉了雲端主機，本次價格為「每日最低價 + 已知稅金」估算，'
                   '可能與實際結帳金額有些微差異。</div>')
    if error:
        banner = f'<div class="banner err">本次執行失敗：{html.escape(error)}</div>' + banner

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>虎航票價監控</title>
<style>
:root {{ --bg:#f6f5f2; --card:#fff; --ink:#1f1f1f; --mute:#6b6b6b; --line:#e4e1da; --accent:#f5a623;
        --up:#c0392b; --down:#1e8449; --hit:#fff4dc; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17171a; --card:#212126; --ink:#ececec; --mute:#9a9aa0;
        --line:#33333a; --hit:#3a2f17; --up:#ff7a6b; --down:#5fd38d; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Noto Sans TC","Microsoft JhengHei",sans-serif; }}
main {{ max-width:1000px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.meta {{ color:var(--mute); font-size:13px; margin-bottom:16px; }}
.banner {{ padding:10px 14px; border-radius:8px; background:var(--card); border:1px solid var(--line); margin-bottom:12px; }}
.banner.hit {{ background:var(--hit); border-color:var(--accent); font-weight:700; }}
.banner.warn {{ border-color:var(--accent); font-size:13px; }}
.banner.err {{ color:var(--up); }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; min-width:760px; }}
th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--mute); font-weight:600; }}
tr:last-child td {{ border-bottom:none; }}
tr.hit td {{ background:var(--hit); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.strong {{ font-weight:700; }}
.sub {{ color:var(--mute); font-size:12px; }}
.tag {{ font-size:11px; font-weight:400; color:var(--mute); border:1px solid var(--line); border-radius:4px; padding:0 4px; margin-left:4px; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .same {{ color:var(--mute); }}
.note {{ color:var(--mute); font-size:12px; margin-top:12px; }}
</style></head><body><main>
<h1>虎航 高雄 ⇄ 大阪 票價監控</h1>
<div class="meta">最後檢查：{ts}（台灣時間）｜2 大 1 小｜去程 {WINDOW_START:%m/%d}～回程 {WINDOW_END:%m/%d}｜5～6 天｜每小時更新</div>
{banner}
<div class="wrap"><table>
<thead><tr><th>日期 / 航班</th><th class="num">每人票價</th><th class="num">每人稅金</th><th class="num">每人含稅</th>
<th class="num">較上次</th><th class="num">歷史最低</th><th class="num">3 人合計</th><th class="num">剩餘座位 去/回</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="note">tigerLight 最低票價，不含託運行李與刷卡手續費；兒童與成人同價。候選組合依官網每日最低價挑前 {TOP_N} 名，再到訂票頁查含稅實價。</p>
</main></body></html>""", encoding="utf-8")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    results, blocked, error = [], False, None
    try:
        results, blocked = fetch_quotes(candidates())
        append_history(ts, results)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    write_report(ts, results, blocked, error)
    print(f"[{ts}] {'ERROR ' + error if error else 'ok'}: {len(results)} quotes, blocked={blocked}")
    if error:
        sys.exit(1)


if __name__ == "__main__":
    main()
