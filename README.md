# congress-pipeline

Scrapes congressional stock trades from the **official** government portals and
publishes a normalized `congress_trades.json` that the Market Sentinel Apps
Script reads. Runs daily on GitHub Actions.

## Why this exists

Apps Script (Google's servers) can't do the Senate eFD CSRF/agreement handshake
or parse PDFs, and the old community APIs (House/Senate Stock Watcher) had their
data buckets taken offline. GitHub Actions runs real Python, and
`raw.githubusercontent.com` is never blocked from Apps Script — so the scraping
happens here and the email just reads the result.

## Sources

| Chamber | Source | Notes |
|---|---|---|
| House  | `disclosures-clerk.house.gov` (XML index + PTR PDFs) | Electronic filings parse cleanly; handwritten/scanned ones are skipped. |
| Senate | `efdsearch.senate.gov` (electronic PTR HTML tables) | Paper filings (PDF) are skipped. |

Best-effort by design: everything machine-readable is captured; image-only
filings can't be parsed by anyone without OCR and are left out.

## Setup (one time)

1. Create a new GitHub repo (e.g. `congress-pipeline`) — can be public or private.
2. Add these files: `scraper.py`, `committees.py`, `requirements.txt`,
   `.github/workflows/update-congress-data.yml`.
3. Push. Go to the repo's **Actions** tab and enable workflows if prompted.
4. Click **Update congress trades → Run workflow** to do a first run now.
5. Confirm `congress_trades.json` appears in the repo.
6. Copy its **raw** URL (open the file → "Raw" button), e.g.:
   `https://raw.githubusercontent.com/<you>/congress-pipeline/main/congress_trades.json`
7. Paste it into the Apps Script `CONFIG.CONGRESS_JSON_URL`, save, and run
   `testCongressEmail` to verify.

> If the repo is **private**, raw URLs require a token. Easiest path: make the
> repo public (the data is already public record), or switch to a tokenized
> fetch in Apps Script. Public is recommended.

## Tuning

- **Committees:** edit `committees.py`. That's the single source of truth for
  who counts as a "committee member" (highlighted in the email).
- **Window:** `WINDOW_DAYS` in `scraper.py` (default 40; the email itself shows
  the last 30).
- **Schedule:** the `cron` in the workflow (default 09:00 UTC, before the 7am ET
  email reads it).

## Debugging

Each run logs to the Actions output: how many House PTR filings were found, how
many parsed, Senate counts, and the final write. If a run shows `0` trades,
open the log — it'll show which source failed (the official sites occasionally
change markup, which is exactly why this is isolated from your email).
