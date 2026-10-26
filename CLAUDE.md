# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Tigerair Taiwan round-trip fare monitor that runs on GitHub Actions. `monitor.py` is the whole app: standard library only, Python 3.11+ (needs `tomllib`; CI uses 3.12). The repo owner edits only `config.toml`, so user-facing text (config comments, validation errors, report page, email) is in Traditional Chinese. Keep it that way.

## Commands

There are no tests, linter or build step.

```sh
python -u monitor.py                      # full run: fetch fares, append history, rewrite docs/index.html, maybe email
TEST_EMAIL=true python -u monitor.py      # send a test email with each route's top 3, whatever the limit
```

Running locally writes to `data/` and `docs/index.html`, the same files CI commits. Email is skipped (with a printed note) unless `SMTP_USER`, `SMTP_PASS` and `MAIL_TO` are set. `SMTP_HOST`/`SMTP_PORT` default to Gmail over SSL on port 465. `MAIL_TO` can hold several addresses separated by commas.

## How it runs

`.github/workflows/monitor.yml` runs `monitor.py` from an external cron-job.org trigger (`workflow_dispatch` with `scheduled=true`), on pushes that touch `config.toml` or `monitor.py`, and on demand. The built-in 5-minute `schedule` is commented out (disabled); uncomment it to bring it back. After each run it commits `docs/` and `data/` back to `main` as "Update prices …". That's why most commits are bot commits, and why you should `git pull --rebase` before pushing. `docs/index.html` is served by GitHub Pages at `REPORT_URL`.

`SCHEDULED=true` changes how failures are handled. On scheduled runs, a config error or a failure on some routes exits quietly with code 0, so GitHub doesn't send a failure email every 5 minutes. The push run that introduced the problem exits with code 1 and shows the error on the report page.

## Pipeline in `monitor.py`

1. `load_config()` turns `config.toml` (`[trip]`, `[alert]`, `[report]`, plus one `[[route]]` per destination) into one flat dict per route, after validating it. Validation problems raise `ConfigError` with messages the user can act on, in Chinese. `earliest_departure = "today"`, or a date already in the past, becomes today's date.
2. `estimates()` calls the public `daily-prices` API (`API`) twice per route, once each way. That gives the cheapest untaxed one-way fare per day per person. It then pairs every departure day with each `trip_days` length, adds the per-route `tax_outbound + tax_return` from config, and keeps the cheapest `top_n`. All prices are **estimates**: children cost the same as adults, and seat availability is unknown.
3. `append_history()` appends to `data/history_<ORIG>-<DEST>_<n>A<n>C.csv` (utf-8-sig). The file name comes from `cfg["search"]`, so changing the passenger count starts a new history file.
4. `notify()` sends one email for combos that newly fall to or below `max_price_per_person`, or drop below the price emailed last time. It deduplicates through `data/notified.json` (keys look like `"<search>:<dep>_<ret>"`). A combo that rises back above the limit is forgotten, so a later dip is reported again. A route that errored keeps its notified state unchanged.
5. `write_report()` generates the whole GitHub Pages HTML from one f-string, with CSS inside it, so CSS braces are doubled (`{{ }}`). It reads the history CSVs to show the change since the last run and the all-time low. If every route failed, the last good report is kept.

Dates after the current year get a year prefix: `fmt_d_mail` adds it to the email, and `fmt_d_html` adds a red `.yr` tag to the page.

The booking site blocks cloud runners and headless browsers, which is why the monitor uses only the daily-prices API and doesn't use a browser.
