"""Hourly Tigerair KHH<->KIX price monitor (GitHub Actions edition, API only).

The booking site does not return fares to cloud runners, so this version skips the
browser entirely:
1. Read one-way daily prices (untaxed, per person) from the public daily-prices API.
2. Build every 5-6 day round trip inside the travel window, keep the TOP_N cheapest.
3. Estimate the tax-inclusive price with the per-person taxes seen on the booking site.
4. Append to data/history.csv and regenerate docs/index.html (served by GitHub Pages).
"""
import csv, json, os, smtplib, sys, traceback, urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent

ORIGIN, DEST, ADULTS, CHILDREN = "KHH", "KIX", 2, 1
WINDOW_START, WINDOW_END = date(2026, 11, 5), date(2026, 12, 30)
TRIP_DAYS = (5, 6)          # inclusive of departure and return day
TOP_N = 10
LIMIT_PER_PERSON = 9000     # tax-inclusive round trip; rows at or under this are highlighted
# Per-person taxes seen on the booking site (2026-10-15): outbound 1,100; return 1,685-1,785.
# The higher return figure is used so the estimate errs on the expensive side.
TAX_OUT, TAX_RET = 1100, 1785
HISTORY, REPORT = HERE / "data" / "history.csv", HERE / "docs" / "index.html"
NOTIFIED = HERE / "data" / "notified.json"   # combo -> price already emailed, to avoid repeats
REPORT_URL = "https://roylin520.github.io/tigerair-monitor/"
BOOKING = ("https://booking.tigerairtw.com/?outbound={o}-{d}&departureDate={dep}&inbound={d}-{o}"
           "&returnDate={ret}&adult={a}&children={c}&type=roundTrip&language=zh-TW&currencyCode=TWD")
API = ("https://api-cms.tigerairtw.com/api/app/book/daily-prices?origin={o}&destination={d}"
       "&userCurrency=TWD&pricingCurrency=TWD&since={s}&until={u}")
WD = "一二三四五六日"


def daily_prices(o, d):
    url = API.format(o=o, d=d, s=WINDOW_START, u=WINDOW_END)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)["data"]
    return {date.fromisoformat(x["date"]): x["amount"] for x in data if x["amount"]}


def estimates():
    out_p, ret_p = daily_prices(ORIGIN, DEST), daily_prices(DEST, ORIGIN)
    print(f"daily prices: {len(out_p)} outbound days, {len(ret_p)} return days")
    rows = []
    d = WINDOW_START
    while d <= WINDOW_END:
        for n in TRIP_DAYS:
            r = d + timedelta(days=n - 1)
            if r <= WINDOW_END and d in out_p and r in ret_p:
                per = out_p[d] + ret_p[r] + TAX_OUT + TAX_RET
                rows.append({"dep": d.isoformat(), "ret": r.isoformat(), "days": n,
                             "out_fare": out_p[d], "ret_fare": ret_p[r],
                             "fare_per_adult": out_p[d] + ret_p[r], "tax_per_adult": TAX_OUT + TAX_RET,
                             "total_per_adult": per, "total_all": per * (ADULTS + CHILDREN)})
        d += timedelta(days=1)
    rows.sort(key=lambda x: (x["total_per_adult"], x["dep"]))
    return rows[:TOP_N]


def append_history(ts, results):
    HISTORY.parent.mkdir(exist_ok=True)
    new = not HISTORY.exists()
    with open(HISTORY, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at", "dep", "ret", "total_per_adult", "total_all", "fare_per_adult",
                        "tax_per_adult", "out_seats", "ret_seats", "estimated"])
        for r in results:
            w.writerow([ts, r["dep"], r["ret"], r["total_per_adult"], r["total_all"],
                        r["fare_per_adult"], r["tax_per_adult"], "", "", 1])


def read_history():
    if not HISTORY.exists():
        return []
    with open(HISTORY, encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f) if row["total_per_adult"]]


def booking_url(r):
    return BOOKING.format(o=ORIGIN, d=DEST, dep=r["dep"], ret=r["ret"], a=ADULTS, c=CHILDREN)


def fmt_d(s):
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}（{WD[d.weekday()]}）"


def write_report(ts, results, error=None):
    hist = read_history()
    runs = sorted({h["checked_at"] for h in hist})
    prev_run = runs[-2] if len(runs) >= 2 else None
    prev = {(h["dep"], h["ret"]): int(h["total_per_adult"]) for h in hist if h["checked_at"] == prev_run}
    low = {}
    for h in hist:
        k, v = (h["dep"], h["ret"]), int(h["total_per_adult"])
        low[k] = min(low.get(k, v), v)

    rows = []
    for r in results:
        k = (r["dep"], r["ret"])
        t = r["total_per_adult"]
        if k in prev and prev[k] != t:
            diff = t - prev[k]
            chg = f'<span class="{"up" if diff > 0 else "down"}">{"▲" if diff > 0 else "▼"}{abs(diff):,}</span>'
        elif k in prev:
            chg = '<span class="same">—</span>'
        else:
            chg = '<span class="same">新</span>'
        rows.append(
            f'<tr class="{"hit" if t <= LIMIT_PER_PERSON else ""}">'
            f'<td>{fmt_d(r["dep"])} → {fmt_d(r["ret"])}<div class="sub">{r["days"]} 天｜'
            f'去 {r["out_fare"]:,}・回 {r["ret_fare"]:,}</div></td>'
            f'<td class="num">{r["fare_per_adult"]:,}</td><td class="num">{r["tax_per_adult"]:,}</td>'
            f'<td class="num strong">{t:,}</td><td class="num">{chg}</td>'
            f'<td class="num">{low.get(k, t):,}</td><td class="num strong">{r["total_all"]:,}</td>'
            f'<td><a href="{escape(booking_url(r))}" target="_blank" rel="noopener">訂票 ↗</a></td></tr>')

    hits = [r for r in results if r["total_per_adult"] <= LIMIT_PER_PERSON]
    banner = (f'<div class="banner hit">有 {len(hits)} 組每人含稅 ≤ {LIMIT_PER_PERSON:,} 元！</div>' if hits else
              f'<div class="banner">目前沒有每人含稅 ≤ {LIMIT_PER_PERSON:,} 元的組合</div>')
    if error:
        banner = f'<div class="banner err">本次執行失敗（以下為上次結果）：{escape(error)}</div>' + banner

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>虎航票價監控</title>
<style>
:root {{ --bg:#f6f5f2; --card:#fff; --ink:#1f1f1f; --mute:#6b6b6b; --line:#e4e1da; --accent:#f5a623;
        --up:#c0392b; --down:#1e8449; --hit:#fff4dc; --link:#b26a00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17171a; --card:#212126; --ink:#ececec; --mute:#9a9aa0;
        --line:#33333a; --hit:#3a2f17; --up:#ff7a6b; --down:#5fd38d; --link:#f5b54a; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Noto Sans TC","Microsoft JhengHei",sans-serif; }}
main {{ max-width:1000px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.meta {{ color:var(--mute); font-size:13px; margin-bottom:16px; }}
.banner {{ padding:10px 14px; border-radius:8px; background:var(--card); border:1px solid var(--line); margin-bottom:12px; }}
.banner.hit {{ background:var(--hit); border-color:var(--accent); font-weight:700; }}
.banner.err {{ color:var(--up); }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; min-width:760px; }}
a {{ color:var(--link); text-decoration:none; font-weight:600; white-space:nowrap; }}
a:hover {{ text-decoration:underline; }}
th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ font-size:12px; color:var(--mute); font-weight:600; }}
tr:last-child td {{ border-bottom:none; }}
tr.hit td {{ background:var(--hit); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.strong {{ font-weight:700; }}
.sub {{ color:var(--mute); font-size:12px; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .same {{ color:var(--mute); }}
.note {{ color:var(--mute); font-size:12px; margin-top:12px; }}
</style></head><body><main>
<h1>虎航 高雄 ⇄ 大阪 票價監控</h1>
<div class="meta">最後檢查：{ts}（台灣時間）｜2 大 1 小｜去程 {WINDOW_START:%m/%d}～回程 {WINDOW_END:%m/%d}｜5～6 天｜每小時更新</div>
{banner}
<div class="wrap"><table>
<thead><tr><th>日期</th><th class="num">每人票價</th><th class="num">每人稅金</th><th class="num">每人含稅（估）</th>
<th class="num">較上次</th><th class="num">歷史最低</th><th class="num">3 人合計（估）</th><th>官網</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="note">票價取自虎航官網每日最低價（tigerLight，不含託運行李）；稅金以實際查到的每人去程 {TAX_OUT:,}、回程 {TAX_RET:,} 估算，
與結帳金額約差 0～100 元，不含刷卡手續費；兒童與成人同價。最低價座位可能不足 3 位，訂票前請以官網為準。</p>
</main></body></html>""", encoding="utf-8")


def notify(ts, results):
    """Email combos that newly fall to or below the limit (or get cheaper than last emailed).

    Credentials come from environment variables set by GitHub Secrets:
    SMTP_USER, SMTP_PASS, MAIL_TO (optional SMTP_HOST, SMTP_PORT; defaults to Gmail).
    TEST_EMAIL=true sends a test message with the current top 3 regardless of the limit.
    """
    test = os.environ.get("TEST_EMAIL") == "true"
    sent = json.loads(NOTIFIED.read_text(encoding="utf-8")) if NOTIFIED.exists() else {}
    hits = {f'{r["dep"]}_{r["ret"]}': r for r in results if r["total_per_adult"] <= LIMIT_PER_PERSON}
    new = [r for k, r in hits.items() if r["total_per_adult"] < sent.get(k, 10**9)]
    # Forget combos that went back above the limit, so a later dip is reported again.
    sent = {k: v for k, v in sent.items() if k in hits}
    if not new and not test:
        NOTIFIED.write_text(json.dumps(sent, indent=1), encoding="utf-8")
        return

    user, pw, to = (os.environ.get(k, "") for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to):
        print("email skipped: SMTP_USER / SMTP_PASS / MAIL_TO secrets are not set")
        return

    rows = new if new else results[:3]
    subject = (f"[測試] 虎航 高雄⇄大阪 票價通知" if test and not new else
               f"虎航 高雄⇄大阪：{len(new)} 組每人含稅 ≤ {LIMIT_PER_PERSON:,} 元")
    lines = [f"檢查時間：{ts}（台灣時間）｜2 大 1 小｜每人含稅為估算", ""]
    for r in rows:
        lines += [f'{fmt_d(r["dep"])} → {fmt_d(r["ret"])}（{r["days"]} 天）',
                  f'  每人含稅 {r["total_per_adult"]:,} 元｜3 人合計 {r["total_all"]:,} 元',
                  f'  訂票：{booking_url(r)}', ""]
    lines.append(f"完整報表：{REPORT_URL}")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content("\n".join(lines))
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)

    for r in new:
        sent[f'{r["dep"]}_{r["ret"]}'] = r["total_per_adult"]
    NOTIFIED.write_text(json.dumps(sent, indent=1), encoding="utf-8")
    print(f"email sent to MAIL_TO: {len(rows)} combos, test={test and not new}")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    results, error = [], None
    try:
        results = estimates()
        append_history(ts, results)
        for r in results:
            print(f'  {r["dep"]} -> {r["ret"]}  per person {r["total_per_adult"]:,}  x3 {r["total_all"]:,}')
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    if results:
        try:
            notify(ts, results)
        except Exception:
            print("email failed (report still updated):")
            traceback.print_exc()
    if results or not REPORT.exists():
        write_report(ts, results, error)
    print(f"[{ts}] {'ERROR ' + error if error else 'ok'}: {len(results)} estimates (API mode)")
    if error:
        sys.exit(1)


if __name__ == "__main__":
    main()
