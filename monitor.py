"""Hourly Tigerair round-trip price monitor (GitHub Actions edition, API only).

All search conditions live in config.toml. The booking site does not return fares to
cloud runners, so this version skips the browser entirely:
1. Read one-way daily prices (untaxed, per person) from the public daily-prices API.
2. Build every round trip of the configured lengths inside the travel window, keep the cheapest.
3. Estimate the tax-inclusive price with the per-person taxes from config.toml.
4. Append to a per-search history CSV, regenerate docs/index.html (GitHub Pages),
   and email combos at or under the price limit.
"""
import csv, json, os, smtplib, sys, tomllib, traceback, urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.toml"
REPORT = HERE / "docs" / "index.html"
NOTIFIED = HERE / "data" / "notified.json"   # "<search>:<dep>_<ret>" -> price already emailed
LAST_RUN = HERE / "data" / "last_run.txt"    # time slot of the last real check, for interval_minutes
REPORT_URL = "https://roylin520.github.io/tigerair-monitor/"
BOOKING = ("https://booking.tigerairtw.com/?outbound={o}-{d}&departureDate={dep}&inbound={d}-{o}"
           "&returnDate={ret}&adult={a}&children={c}&type=roundTrip&language=zh-TW&currencyCode=TWD")
API = ("https://api-cms.tigerairtw.com/api/app/book/daily-prices?origin={o}&destination={d}"
       "&userCurrency=TWD&pricingCurrency=TWD&since={s}&until={u}")
WD = "一二三四五六日"
STATIONS = {
    "TPE": "台北", "KHH": "高雄", "RMQ": "台中", "NRT": "東京成田", "HND": "東京羽田", "KIX": "大阪",
    "NGO": "名古屋", "FUK": "福岡", "CTS": "札幌", "OKA": "沖繩", "OKJ": "岡山", "KMJ": "熊本",
    "KMQ": "小松", "SDJ": "仙台", "HKD": "函館", "ISG": "石垣", "AOJ": "青森", "HNA": "花卷",
    "OIT": "大分", "IBR": "茨城", "UKB": "神戶", "GMP": "首爾金浦", "ICN": "首爾仁川", "PUS": "釜山",
    "CJU": "濟州", "DAD": "峴港",
}


class ConfigError(Exception):
    pass


def load_config():
    """Read config.toml into a flat dict and validate it, with messages the user can act on."""
    try:
        with open(CONFIG, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml 格式錯誤：{e}") from None
    try:
        t, a, e, r = raw["trip"], raw["alert"], raw["estimate"], raw["report"]
        s = raw.get("schedule", {})
        c = {
            "origin": str(t["origin"]).strip().upper(), "dest": str(t["destination"]).strip().upper(),
            "start": t["earliest_departure"], "end": t["latest_return"],
            "trip_days": sorted({int(n) for n in t["trip_days"]}),
            "adults": int(t["adults"]), "children": int(t.get("children", 0)),
            "limit": int(a["max_price_per_person"]),
            "tax_out": int(e["tax_outbound"]), "tax_ret": int(e["tax_return"]),
            "top_n": int(r["top_n"]),
            "interval": int(s.get("interval_minutes", 60)),
        }
    except KeyError as k:
        raise ConfigError(f"config.toml 缺少設定項目：{k}") from None
    except (TypeError, ValueError) as err:
        raise ConfigError(f"config.toml 數值格式錯誤：{err}") from None
    problems = []
    for key, label in (("origin", "origin"), ("dest", "destination")):
        if len(c[key]) != 3 or not c[key].isalpha():
            problems.append(f"{label} 要是 3 個英文字母的機場代碼（目前是「{c[key]}」）")
    if c["origin"] == c["dest"]:
        problems.append("出發地和目的地不能相同")
    if not (isinstance(c["start"], date) and isinstance(c["end"], date)):
        problems.append("日期要寫成 YYYY-MM-DD，不要加引號")
    elif c["end"] <= c["start"]:
        problems.append("latest_return 必須晚於 earliest_departure")
    if not c["trip_days"] or min(c["trip_days"]) < 2:
        problems.append("trip_days 每個值至少要 2 天")
    if c["adults"] < 1 or c["children"] < 0 or c["adults"] + c["children"] > 9:
        problems.append("adults 至少 1 人，adults + children 最多 9 人")
    if c["limit"] <= 0 or c["top_n"] <= 0:
        problems.append("max_price_per_person 與 top_n 必須大於 0")
    if c["interval"] < 5:
        problems.append("interval_minutes 最小是 5（GitHub 排程最短每 5 分鐘）")
    if problems:
        raise ConfigError("；".join(problems))
    c["pax"] = c["adults"] + c["children"]
    c["search"] = f'{c["origin"]}-{c["dest"]}_{c["adults"]}A{c["children"]}C'
    c["history"] = HERE / "data" / f'history_{c["search"]}.csv'
    c["route"] = f'{STATIONS.get(c["origin"], c["origin"])} ⇄ {STATIONS.get(c["dest"], c["dest"])}'
    c["pax_text"] = f'{c["adults"]} 大' + (f' {c["children"]} 小' if c["children"] else "")
    c["days_text"] = "、".join(map(str, c["trip_days"])) + " 天"
    return c


def daily_prices(cfg, o, d):
    url = API.format(o=o, d=d, s=cfg["start"], u=cfg["end"])
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)["data"]
    return {date.fromisoformat(x["date"]): x["amount"] for x in data if x["amount"]}


def estimates(cfg):
    out_p, ret_p = daily_prices(cfg, cfg["origin"], cfg["dest"]), daily_prices(cfg, cfg["dest"], cfg["origin"])
    print(f'{cfg["search"]}: {len(out_p)} outbound days, {len(ret_p)} return days with fares')
    tax = cfg["tax_out"] + cfg["tax_ret"]
    rows = []
    d = cfg["start"]
    while d <= cfg["end"]:
        for n in cfg["trip_days"]:
            r = d + timedelta(days=n - 1)
            if r <= cfg["end"] and d in out_p and r in ret_p:
                per = out_p[d] + ret_p[r] + tax
                rows.append({"dep": d.isoformat(), "ret": r.isoformat(), "days": n,
                             "out_fare": out_p[d], "ret_fare": ret_p[r],
                             "fare_per_adult": out_p[d] + ret_p[r], "tax_per_adult": tax,
                             "total_per_adult": per, "total_all": per * cfg["pax"]})
        d += timedelta(days=1)
    rows.sort(key=lambda x: (x["total_per_adult"], x["dep"]))
    return rows[:cfg["top_n"]]


def append_history(cfg, ts, results):
    path = cfg["history"]
    path.parent.mkdir(exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at", "dep", "ret", "total_per_adult", "total_all", "fare_per_adult",
                        "tax_per_adult", "out_seats", "ret_seats", "estimated"])
        for r in results:
            w.writerow([ts, r["dep"], r["ret"], r["total_per_adult"], r["total_all"],
                        r["fare_per_adult"], r["tax_per_adult"], "", "", 1])


def read_history(cfg):
    if not cfg["history"].exists():
        return []
    with open(cfg["history"], encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f) if row["total_per_adult"]]


def booking_url(cfg, r):
    return BOOKING.format(o=cfg["origin"], d=cfg["dest"], dep=r["dep"], ret=r["ret"],
                          a=cfg["adults"], c=cfg["children"])


def fmt_d(s):
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}（{WD[d.weekday()]}）"


def notify(cfg, ts, results):
    """Email combos that newly fall to or below the limit (or get cheaper than last emailed).

    Credentials come from environment variables set by GitHub Secrets:
    SMTP_USER, SMTP_PASS, MAIL_TO (optional SMTP_HOST, SMTP_PORT; defaults to Gmail).
    TEST_EMAIL=true sends a test message with the current top 3 regardless of the limit.
    """
    test = os.environ.get("TEST_EMAIL") == "true"
    sent = json.loads(NOTIFIED.read_text(encoding="utf-8")) if NOTIFIED.exists() else {}
    prefix = cfg["search"] + ":"
    hits = {prefix + f'{r["dep"]}_{r["ret"]}': r for r in results if r["total_per_adult"] <= cfg["limit"]}
    new = [r for k, r in hits.items() if r["total_per_adult"] < sent.get(k, 10**9)]
    # Forget this search's combos that went back above the limit, so a later dip is reported again.
    sent = {k: v for k, v in sent.items() if not k.startswith(prefix) or k in hits}
    if not new and not test:
        NOTIFIED.write_text(json.dumps(sent, indent=1), encoding="utf-8")
        return

    user, pw, to = (os.environ.get(k, "") for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to):
        print("email skipped: SMTP_USER / SMTP_PASS / MAIL_TO secrets are not set")
        return

    rows = new if new else results[:3]
    subject = (f'[測試] 虎航 {cfg["route"]} 票價通知' if test and not new else
               f'虎航 {cfg["route"]}：{len(new)} 組每人含稅 ≤ {cfg["limit"]:,} 元')
    lines = [f'檢查時間：{ts}（台灣時間）｜{cfg["pax_text"]}｜每人含稅為估算', ""]
    for r in rows:
        lines += [f'{fmt_d(r["dep"])} → {fmt_d(r["ret"])}（{r["days"]} 天）',
                  f'  每人含稅 {r["total_per_adult"]:,} 元｜{cfg["pax"]} 人合計 {r["total_all"]:,} 元',
                  f'  訂票：{booking_url(cfg, r)}', ""]
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
        sent[prefix + f'{r["dep"]}_{r["ret"]}'] = r["total_per_adult"]
    NOTIFIED.write_text(json.dumps(sent, indent=1), encoding="utf-8")
    print(f"email sent to MAIL_TO: {len(rows)} combos, test={test and not new}")


def write_report(cfg, ts, results, error=None):
    hist = read_history(cfg) if cfg else []
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
            f'<tr class="{"hit" if t <= cfg["limit"] else ""}">'
            f'<td>{fmt_d(r["dep"])} → {fmt_d(r["ret"])}<div class="sub">{r["days"]} 天｜'
            f'去 {r["out_fare"]:,}・回 {r["ret_fare"]:,}</div></td>'
            f'<td class="num">{r["fare_per_adult"]:,}</td><td class="num">{r["tax_per_adult"]:,}</td>'
            f'<td class="num strong">{t:,}</td><td class="num">{chg}</td>'
            f'<td class="num">{low.get(k, t):,}</td><td class="num strong">{r["total_all"]:,}</td>'
            f'<td><a href="{escape(booking_url(cfg, r))}" target="_blank" rel="noopener">訂票 ↗</a></td></tr>')

    if cfg:
        hits = [r for r in results if r["total_per_adult"] <= cfg["limit"]]
        banner = (f'<div class="banner hit">有 {len(hits)} 組每人含稅 ≤ {cfg["limit"]:,} 元！</div>' if hits else
                  f'<div class="banner">目前沒有每人含稅 ≤ {cfg["limit"]:,} 元的組合</div>')
        if not results and not error:
            banner += '<div class="banner">設定的日期範圍內查不到可組成來回的票價，請確認 config.toml 的日期與天數。</div>'
        title = f'虎航 {cfg["route"]} 票價監控'
        meta = (f'最後檢查：{ts}（台灣時間）｜{cfg["pax_text"]}｜去程 {cfg["start"]:%m/%d} 起～回程 {cfg["end"]:%m/%d} 前'
                f'｜玩 {cfg["days_text"]}｜每小時更新')
        note = (f'票價取自虎航官網每日最低價（tigerLight，不含託運行李）；稅金以 config.toml 設定的每人去程 '
                f'{cfg["tax_out"]:,}、回程 {cfg["tax_ret"]:,} 估算，與結帳金額可能略有差異，不含刷卡手續費；'
                f'兒童與成人同價。最低價座位可能不足 {cfg["pax"]} 位，訂票前請以官網為準。')
        total_hdr = f'{cfg["pax"]} 人合計（估）'
    else:
        banner, title, meta, note, total_hdr = "", "虎航票價監控", f"最後檢查：{ts}（台灣時間）", "", "合計（估）"
    if error:
        banner = f'<div class="banner err">本次執行失敗：{escape(error)}</div>' + banner

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>{escape(title)}</title>
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
<h1>{escape(title)}</h1>
<div class="meta">{escape(meta)}</div>
{banner}
<div class="wrap"><table>
<thead><tr><th>日期</th><th class="num">每人票價</th><th class="num">每人稅金</th><th class="num">每人含稅（估）</th>
<th class="num">較上次</th><th class="num">歷史最低</th><th class="num">{total_hdr}</th><th>官網</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="note">{escape(note)}</p>
</main></body></html>""", encoding="utf-8")


def time_slot(cfg, now):
    """Index of the interval_minutes-long slot containing now (slots aligned to the epoch)."""
    return int(now.timestamp() // 60) // cfg["interval"]


def is_due(cfg, now):
    """Scheduled runs fire every 5 minutes; only check prices once per configured slot."""
    try:
        last = LAST_RUN.read_text(encoding="utf-8").split()
        last_slot, last_interval = int(last[0]), int(last[1])
    except (OSError, ValueError, IndexError):
        return True
    return last_interval != cfg["interval"] or time_slot(cfg, now) != last_slot


def main():
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    forced = os.environ.get("FORCE_RUN") == "true"   # push / manual runs always check
    cfg, results, error = None, [], None
    try:
        cfg = load_config()
        if not forced and not is_due(cfg, now):
            print(f'[{ts}] not due yet (interval {cfg["interval"]} min); skipping')
            return
        results = estimates(cfg)
        LAST_RUN.write_text(f'{time_slot(cfg, now)} {cfg["interval"]}\n', encoding="utf-8")
        append_history(cfg, ts, results)
        for r in results:
            print(f'  {r["dep"]} -> {r["ret"]}  per person {r["total_per_adult"]:,}  x{cfg["pax"]} {r["total_all"]:,}')
    except Exception as e:
        error = str(e) if isinstance(e, ConfigError) else f"{type(e).__name__}: {e}"
        traceback.print_exc()
    if results:
        try:
            notify(cfg, ts, results)
        except Exception:
            print("email failed (report still updated):")
            traceback.print_exc()
    # Rewrite the report on success or on a config error (so the user sees what to fix);
    # on a temporary fetch failure keep the last good report. A broken config is reported by
    # the push run that introduced it; the 5-minute scheduled runs don't rewrite it each time.
    if results or (cfg is None and forced) or not error or not REPORT.exists():
        write_report(cfg, ts, results, error)
    print(f"[{ts}] {'ERROR ' + error if error else 'ok'}: {len(results)} estimates (API mode)")
    if error and (cfg is not None or forced):
        sys.exit(1)


if __name__ == "__main__":
    main()
