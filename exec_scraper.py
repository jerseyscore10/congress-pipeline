#!/usr/bin/env python3
"""
Executive-branch trade scraper -> exec_trades.json

The President (and other PAS officials) file OGE Form 278-T periodic transaction
reports. OGE exposes them via a JSON REST endpoint, but the PDFs are SCANNED forms
with garbled OCR text and hundreds of mostly fixed-income rows. So instead of
parsing the broken text layer, this pipeline (per design review):

  1. Polls the OGE REST endpoint for new "278 Transaction" filings by watched filers.
     Stable filing ids are tracked in seen_filings.json (committed back) so each is
     processed once.
  2. Renders each new PDF's pages to images (pdf2image) — bypassing the bad text layer.
  3. Sends the images to a vision LLM (Claude) with forced structured output to extract
     ONLY common-stock equity transactions, ignoring bonds/Treasuries/preferreds/munis.
  4. Maps each extracted asset name to a real ticker via SEC EDGAR + RapidFuzz fuzzy
     match (>= threshold); anything that doesn't map is discarded as non-equity/noise.
  5. Writes exec_trades.json, pairing every trade with the source OGE PDF link.

Runs on GitHub Actions. Requires: poppler (apt), ANTHROPIC_API_KEY (secret).
"""

import base64
import html
import io
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from pdf2image import convert_from_bytes
from rapidfuzz import process, fuzz
import anthropic

OGE_API = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
SEC_UA = {"User-Agent": "market-sentinel research prince.thissa@gmail.com"}

# Filers to watch (case-insensitive substring match against the OGE 'name' field).
WATCH = ["Trump, Donald J"]
WINDOW_DAYS = 120          # how far back to keep trades in the output
MAX_PAGES = 25             # cap pages sent to the vision model (bounds cost)
DPI = 175
FUZZ_THRESHOLD = 88        # min fuzzy score to accept a ticker mapping
MODEL = os.environ.get("EXEC_VISION_MODEL", "claude-sonnet-4-6")

SEEN_FILE = "seen_filings.json"
OUT_FILE = "exec_trades.json"


def log(*a):
    print(*a, flush=True)


def clean(s):
    return re.sub("<[^>]+>", "", html.unescape(s or "")).strip()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 1. OGE polling
# ---------------------------------------------------------------------------
def list_oge_filings():
    out, start, length = [], 0, 2000
    while True:
        r = requests.get(OGE_API, params={"start": start, "length": length},
                         headers={**UA, "Accept": "application/json",
                                  "X-Requested-With": "XMLHttpRequest"}, timeout=120)
        data = r.json()
        rows = data.get("data", [])
        out.extend(rows)
        total = data.get("recordsTotal", len(out))
        start += length
        if start >= total or not rows:
            break
    return out


def new_278t_filings(seen):
    rows = list_oge_filings()
    log(f"[oge] {len(rows)} total filings in collection")
    hits = []
    for r in rows:
        name = clean(r.get("name"))
        typ = clean(r.get("type"))
        if "278 Transaction" not in typ:
            continue
        if not any(w.lower() in name.lower() for w in WATCH):
            continue
        m = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", html.unescape(r.get("type", "")))
        if not m:
            continue
        url = m.group(1)
        uid = re.search(r"/([0-9A-F]{32})/", url)
        doc_id = uid.group(1) if uid else url
        if doc_id in seen:
            continue
        hits.append({"filer": name, "filed": (r.get("docDate") or "")[:10],
                     "url": url, "doc_id": doc_id})
    log(f"[oge] {len(hits)} new 278-T filings to process")
    return hits


# ---------------------------------------------------------------------------
# 2 + 3. Render to images, extract equities with a vision LLM
# ---------------------------------------------------------------------------
EXTRACT_TOOL = {
    "name": "record_trades",
    "description": "Record the common-stock equity transactions found in the filing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "trades": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "asset_name": {"type": "string", "description": "Company/security name as printed"},
                        "action": {"type": "string", "enum": ["BUY", "SELL"]},
                        "date": {"type": "string", "description": "Transaction date MM/DD/YYYY if visible, else ''"},
                        "amount": {"type": "string", "description": "Amount range as printed, e.g. '$1,001 - $15,000'"},
                    },
                    "required": ["asset_name", "action", "amount"],
                },
            }
        },
        "required": ["trades"],
    },
}

PROMPT = (
    "This is a U.S. OGE Form 278-T periodic transaction report (a scanned PDF). "
    "Read the transactions table across all pages. Extract ONLY common-stock equity "
    "transactions of operating companies (e.g. 'AXON ENTERPRISE INC', 'APPLE INC'). "
    "IGNORE all fixed income and non-equity holdings: anything with NOTE, BOND, BILL, "
    "TREASURY/TREAS, MUNI, municipal, a coupon like '6.875%', a maturity date like "
    "'12/31/49', preferred shares (PFD/PERP/'-XXXpl' suffixes), CDs, money-market or "
    "deposit accounts. For each equity transaction return the company name as printed, "
    "the action (BUY for purchase, SELL for sale), the transaction date, and the amount "
    "range. If there are no equity transactions, return an empty list. Be precise; do "
    "not invent rows."
)


def extract_equities(pdf_bytes, client):
    images = convert_from_bytes(pdf_bytes, dpi=DPI)
    total_pages = len(images)
    images = images[:MAX_PAGES]
    content = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(buf.getvalue()).decode()}})
    content.append({"type": "text", "text": PROMPT})

    resp = client.messages.create(
        model=MODEL, max_tokens=4096,
        tools=[EXTRACT_TOOL], tool_choice={"type": "tool", "name": "record_trades"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            log(f"[vision] {total_pages} pages ({min(total_pages, MAX_PAGES)} sent), "
                f"{len(block.input.get('trades', []))} equity rows returned")
            return block.input.get("trades", [])
    return []


# ---------------------------------------------------------------------------
# 4. Deterministic ticker mapping (SEC EDGAR + fuzzy)
# ---------------------------------------------------------------------------
_STOP = re.compile(r"\b(COMMON|STOCK|CLASS|SHARES|ORDINARY|CAPITAL|INC|CORP|"
                   r"CORPORATION|CO|LTD|LLC|PLC|HOLDINGS|HOLDING|GROUP|COMPANY|THE|"
                   r"NEW|REIT|TRUST|SA|NV|AG)\b")


def _norm(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = _STOP.sub(" ", s)
    return " ".join(s.split())


def load_sec():
    d = requests.get(SEC_TICKERS, headers=SEC_UA, timeout=60).json()
    norm_titles, tickers = [], []
    for v in d.values():
        norm_titles.append(_norm(v["title"]))
        tickers.append(v["ticker"])
    return norm_titles, tickers


# Backstop: reject obvious fixed-income / preferred / cash even if the vision model
# mistakenly surfaced it (a preferred maps to the same issuer's common ticker, so the
# fuzzy match alone can't catch it).
_FIXED_INCOME = re.compile(
    r"\b(NOTE|NT|BOND|BILL|BILLS|TREAS|TREASURY|MUNI|MUNICIPAL|PFD|PREFERRED|PERP|"
    r"DEBENTURE|MTN|FRN|CD|DEP|DEPOSIT|MONEY\s*MARKET)\b", re.I)
_COUPON = re.compile(r"\d+\.\d+\s*%|\d{1,2}/\d{1,2}/\d{2,4}|-[A-Z]{1,5}pl\b", re.I)


def looks_fixed_income(name):
    return bool(_FIXED_INCOME.search(name or "") or _COUPON.search(name or ""))


def map_ticker(name, norm_titles, tickers):
    if looks_fixed_income(name):
        return None, -1   # -1 flags "rejected as non-equity"
    q = _norm(name)
    if len(q) < 3:
        return None, 0
    match = process.extractOne(q, norm_titles, scorer=fuzz.token_set_ratio)
    if match and match[1] >= FUZZ_THRESHOLD:
        return tickers[match[2]], match[1]
    return None, (match[1] if match else 0)


# ---------------------------------------------------------------------------
def to_iso(d):
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime((d or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("[fatal] ANTHROPIC_API_KEY not set")
        sys.exit(1)
    client = anthropic.Anthropic()

    seen = set(load_json(SEEN_FILE, []))
    prior = load_json(OUT_FILE, {}).get("trades", [])

    filings = new_278t_filings(seen)
    titles, tickers = load_sec()

    new_trades = []
    for f in filings:
        log(f"[proc] {f['filer']} filed {f['filed']}  {f['url']}")
        try:
            pdf = requests.get(f["url"], headers=UA, timeout=120).content
            rows = extract_equities(pdf, client)
        except Exception as e:
            log(f"[proc] error: {e}")
            continue
        for t in rows:
            tkr, score = map_ticker(t.get("asset_name", ""), titles, tickers)
            if not tkr:
                log(f"   skip (no ticker match, best={score}): {t.get('asset_name')}")
                continue
            new_trades.append({
                "filer": f["filer"],
                "ticker": tkr,
                "asset": t.get("asset_name", "")[:60],
                "action": t.get("action", "").upper(),
                "amount_str": t.get("amount", ""),
                "tx_date": to_iso(t.get("date", "")),
                "filed_date": f["filed"],
                "source_url": f["url"],
                "match_score": score,
            })
        seen.add(f["doc_id"])

    # merge with prior, de-dupe, prune to window
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).date().isoformat()
    allt, keys = [], set()
    for t in new_trades + prior:
        if (t.get("filed_date") or "") < cutoff:
            continue
        k = (t["filer"], t["ticker"], t.get("tx_date"), t.get("amount_str"))
        if k in keys:
            continue
        keys.add(k)
        allt.append(t)
    allt.sort(key=lambda t: (t.get("filed_date", ""), t.get("tx_date", "")), reverse=True)

    with open(OUT_FILE, "w") as fh:
        json.dump({"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(allt), "trades": allt}, fh, indent=2)
    with open(SEEN_FILE, "w") as fh:
        json.dump(sorted(seen), fh, indent=2)
    log(f"[done] {len(new_trades)} new equity trades; {len(allt)} in {WINDOW_DAYS}-day window")


if __name__ == "__main__":
    main()
