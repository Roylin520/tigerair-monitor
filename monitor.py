"""Tigerair round-trip price monitor (GitHub Actions edition, API only).

All search conditions live in config.toml: shared trip conditions in [trip] and one
[[route]] per destination. The booking site does not return fares to cloud runners,
so this version skips the browser entirely. For each route:
1. Read one-way daily prices (untaxed, per person) from the public daily-prices API.
2. Build every round trip of the configured lengths inside the travel window, keep the cheapest.
3. Estimate the tax-inclusive price with that route's per-person taxes.
4. Append to a per-search history CSV.
Then regenerate docs/index.html (GitHub Pages) and send one email for all routes'
combos at or under the price limit.
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


def station(code):
    return STATIONS.get(code, code)


def is_code(s):
    return len(s) == 3 and s.isalpha()


def load_config():
    """Read config.toml into one flat dict per route and validate, with messages the user can act on."""
    try:
        with open(CONFIG, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml 格式錯誤：{e}") from None
    try:
        t, a, r = raw["trip"], raw["alert"], raw["report"]
        base = {
            "origin": str(t["origin"]).strip().upper(),
            "start": t["earliest_departure"], "end": t["latest_return"],
            "trip_days": sorted({int(n) for n in t["trip_days"]}),
            "adults": int(t["adults"]), "children": int(t.get("children", 0)),
            "limit": int(a["max_price_per_person"]),
            "top_n": int(r["top_n"]),
        }
        routes = [{"dest": str(x["destination"]).strip().upper(),
                   "tax_out": int(x["tax_outbound"]), "tax_ret": int(x["tax_return"])}
                  for x in raw.get("route", [])]
    except KeyError as k:
        raise ConfigError(f"config.toml 缺少設定項目：{k}") from None
    except (TypeError, ValueError) as err:
        raise ConfigError(f"config.toml 數值格式錯誤：{err}") from None

    problems = []
    if not is_code(base["origin"]):
        problems.append(f'origin 要是 3 個英文字母的機場代碼（目前是「{base["origin"]}」）')
    if not routes:
        problems.append("至少要有一段 [[route]]")
    dests = [x["dest"] for x in routes]
    for d in dests:
        if not is_code(d):
            problems.append(f"destination 要是 3 個英文字母的機場代碼（目前是「{d}」）")
        elif d == base["origin"]:
            problems.append(f"destination {d} 和出發地相同")
    dup = sorted({d for d in dests if dests.count(d) > 1})
    if dup:
        problems.append(f'[[route]] 的 destination 重複：{"、".join(dup)}')
    if not (isinstance(base["start"], date) and isinstance(base["end"], date)):
        problems.append("日期要寫成 YYYY-MM-DD，不要加引號")
    elif base["end"] <= base["start"]:
        problems.append("latest_return 必須晚於 earliest_departure")
    if not base["trip_days"] or min(base["trip_days"]) < 2:
        problems.append("trip_days 每個值至少要 2 天")
    if base["adults"] < 1 or base["children"] < 0 or base["adults"] + base["children"] > 9:
        problems.append("adults 至少 1 人，adults + children 最多 9 人")
    if base["limit"] <= 0 or base["top_n"] <= 0:
        problems.append("max_price_per_person 與 top_n 必須大於 0")
    if problems:
        raise ConfigError("；".join(problems))

    base["pax"] = base["adults"] + base["children"]
    base["pax_text"] = f'{base["adults"]} 大' + (f' {base["children"]} 小' if base["children"] else "")
    base["days_text"] = "、".join(map(str, base["trip_days"])) + " 天"
    cfgs = []
    for x in routes:
        c = base | x
        c["search"] = f'{c["origin"]}-{c["dest"]}_{c["adults"]}A{c["children"]}C'
        c["history"] = HERE / "data" / f'history_{c["search"]}.csv'
        c["route"] = f'{station(c["origin"])} ⇄ {station(c["dest"])}'
        cfgs.append(c)
    return cfgs


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


def notify(ts, sections):
    """One email for every route's combos that newly fall to or below the limit
    (or get cheaper than last emailed). sections = [(cfg, results, error)].

    Credentials come from environment variables set by GitHub Secrets:
    SMTP_USER, SMTP_PASS, MAIL_TO (optional SMTP_HOST, SMTP_PORT; defaults to Gmail).
    TEST_EMAIL=true sends a test message with each route's current top 3 regardless of the limit.
    """
    test = os.environ.get("TEST_EMAIL") == "true"
    sent = json.loads(NOTIFIED.read_text(encoding="utf-8")) if NOTIFIED.exists() else {}
    picks, new_keys = [], {}
    for cfg, results, error in sections:
        if error:
            continue   # keep this route's notified state untouched until it can be checked again
        prefix = cfg["search"] + ":"
        hits = {prefix + f'{r["dep"]}_{r["ret"]}': r for r in results if r["total_per_adult"] <= cfg["limit"]}
        new = {k: r for k, r in hits.items() if r["total_per_adult"] < sent.get(k, 10**9)}
        # Forget this route's combos that went back above the limit, so a later dip is reported again.
        sent = {k: v for k, v in sent.items() if not k.startswith(prefix) or k in hits}
        new_keys |= {k: r["total_per_adult"] for k, r in new.items()}
        rows = list(new.values()) if not test else results[:3]
        if rows:
            picks.append((cfg, rows))
    if not new_keys and not test:
        NOTIFIED.write_text(json.dumps(sent, indent=1), encoding="utf-8")
        return

    user, pw, to = (os.environ.get(k, "") for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to):
        print("email skipped: SMTP_USER / SMTP_PASS / MAIL_TO secrets are not set")
        return

    cfg0 = sections[0][0]
    origin = station(cfg0["origin"])
    names = "、".join(station(cfg["dest"]) for cfg, _ in picks)
    subject = (f"[測試] 虎航 {origin}出發 票價通知" if test else
               f'虎航 {origin}出發：{len(new_keys)} 組每人含稅 ≤ {cfg0["limit"]:,} 元（{names}）')
    lines = [f'檢查時間：{ts}（台灣時間）｜{cfg0["pax_text"]}｜每人含稅為估算', ""]
    for cfg, rows in picks:
        lines += [f'【{cfg["route"]}】', ""]
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

    sent |= new_keys
    NOTIFIED.write_text(json.dumps(sent, indent=1), encoding="utf-8")
    print(f"email sent to MAIL_TO: {sum(len(rows) for _, rows in picks)} combos, test={test}")


def route_table(cfg, results):
    hist = read_history(cfg)
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
    return (f'<div class="wrap"><table>'
            f'<thead><tr><th>日期</th><th class="num">每人票價</th><th class="num">每人稅金</th>'
            f'<th class="num">每人含稅（估）</th><th class="num">較上次</th><th class="num">歷史最低</th>'
            f'<th class="num">{cfg["pax"]} 人合計（估）</th><th>官網</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def write_report(ts, sections, config_error=None):
    """sections = [(cfg, results, error)]; config_error = message when config.toml is unusable."""
    body = []
    if config_error:
        title, meta = "虎航票價監控", f"最後檢查：{ts}（台灣時間）"
        body.append(f'<div class="banner err">本次執行失敗：{escape(config_error)}</div>')
        note = ""
    else:
        cfg0 = sections[0][0]
        title = f'虎航 {station(cfg0["origin"])}出發 票價監控'
        meta = (f'最後檢查：{ts}（台灣時間）｜{cfg0["pax_text"]}｜去程 {cfg0["start"]:%m/%d} 起～'
                f'回程 {cfg0["end"]:%m/%d} 前｜玩 {cfg0["days_text"]}｜每 5 分鐘更新')
        limit = cfg0["limit"]
        hits = sum(1 for _, results, _ in sections for r in results if r["total_per_adult"] <= limit)
        body.append(f'<div class="banner hit">共 {hits} 組每人含稅 ≤ {limit:,} 元！</div>' if hits else
                    f'<div class="banner">目前沒有每人含稅 ≤ {limit:,} 元的組合</div>')

        summary = []
        for cfg, results, error in sections:
            name = f'<a href="#{cfg["dest"]}">{escape(cfg["route"])}</a>'
            if error:
                summary.append(f'<tr><td>{name}</td><td colspan="3" class="err">本次查詢失敗</td></tr>')
            elif not results:
                summary.append(f'<tr><td>{name}</td><td colspan="3" class="sub">區間內查不到可組成來回的票價</td></tr>')
            else:
                b = results[0]
                n = sum(1 for r in results if r["total_per_adult"] <= limit)
                summary.append(
                    f'<tr class="{"hit" if n else ""}"><td>{name}</td>'
                    f'<td>{fmt_d(b["dep"])} → {fmt_d(b["ret"])}</td>'
                    f'<td class="num strong">{b["total_per_adult"]:,}</td><td class="num">{n}</td></tr>')
        body.append('<div class="wrap"><table class="summary"><thead><tr><th>航線</th><th>最便宜日期</th>'
                    '<th class="num">每人含稅（估）</th><th class="num">低於門檻</th></tr></thead>'
                    f'<tbody>{"".join(summary)}</tbody></table></div>')

        for cfg, results, error in sections:
            body.append(f'<h2 id="{cfg["dest"]}">{escape(cfg["route"])}'
                        f'<span class="sub">稅金 去 {cfg["tax_out"]:,}・回 {cfg["tax_ret"]:,}</span></h2>')
            if error:
                body.append(f'<div class="banner err">本次查詢失敗：{escape(error)}</div>')
            elif not results:
                body.append('<div class="banner">設定的日期範圍內查不到可組成來回的票價（這條航線不是每天都飛），'
                            '可放寬 config.toml 的日期或天數。</div>')
            else:
                body.append(route_table(cfg, results))
        note = ('票價取自虎航官網每日最低價（tigerLight，不含託運行李）；稅金以 config.toml 各航線設定的每人稅金估算，'
                '與結帳金額可能略有差異，不含刷卡手續費；兒童與成人同價。'
                f'最低價座位可能不足 {cfg0["pax"]} 位，訂票前請以官網為準。')

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
h2 {{ font-size:18px; margin:28px 0 10px; scroll-margin-top:12px; }}
h2 .sub {{ margin-left:10px; font-weight:400; }}
.meta {{ color:var(--mute); font-size:13px; margin-bottom:16px; }}
.banner {{ padding:10px 14px; border-radius:8px; background:var(--card); border:1px solid var(--line); margin-bottom:12px; }}
.banner.hit {{ background:var(--hit); border-color:var(--accent); font-weight:700; }}
.banner.err, .err {{ color:var(--up); }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; min-width:760px; }}
table.summary {{ min-width:520px; }}
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
.note {{ color:var(--mute); font-size:12px; margin-top:16px; }}
</style></head><body><main>
<h1>{escape(title)}</h1>
<div class="meta">{escape(meta)}</div>
{"".join(body)}
<p class="note">{escape(note)}</p>
</main></body></html>""", encoding="utf-8")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    scheduled = os.environ.get("SCHEDULED") == "true"
    try:
        cfgs = load_config()
    except ConfigError as e:
        print(f"[{ts}] ERROR {e}")
        # A broken config is reported by the push run that introduced it; scheduled runs skip
        # quietly so GitHub doesn't email a failure every 5 minutes.
        if not scheduled or not REPORT.exists():
            write_report(ts, [], str(e))
        if not scheduled:
            sys.exit(1)
        return

    sections = []
    for cfg in cfgs:
        try:
            results = estimates(cfg)
            append_history(cfg, ts, results)
            for r in results:
                print(f'  {r["dep"]} -> {r["ret"]}  per person {r["total_per_adult"]:,}  x{cfg["pax"]} {r["total_all"]:,}')
            sections.append((cfg, results, None))
        except Exception as e:
            traceback.print_exc()
            sections.append((cfg, [], f"{type(e).__name__}: {e}"))

    failed = [cfg["search"] for cfg, _, error in sections if error]
    if len(failed) < len(sections):
        try:
            notify(ts, sections)
        except Exception:
            print("email failed (report still updated):")
            traceback.print_exc()
    # If every route failed (e.g. the API is briefly down) keep the last good report.
    if len(failed) < len(sections) or not REPORT.exists():
        write_report(ts, sections)
    total = sum(len(results) for _, results, _ in sections)
    print(f"[{ts}] {'ERROR on ' + ', '.join(failed) if failed else 'ok'}: "
          f"{len(sections)} routes, {total} estimates (API mode)")
    if failed and (len(failed) == len(sections) or not scheduled):
        sys.exit(1)


if __name__ == "__main__":
    main()
