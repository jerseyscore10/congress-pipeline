#!/usr/bin/env python3
"""
Congressional trade scraper -> congress_trades.json

Runs on a GitHub Action (real Python env), so it can do what Apps Script can't:
the Senate EFD CSRF/agreement handshake and PDF text extraction. Output is a
single normalized JSON file the Apps Script reads from raw.githubusercontent.com
(never blocked).

Sources are the OFFICIAL government portals:
  - House:  https://disclosures-clerk.house.gov  (XML filing index + PTR PDFs)
  - Senate: https://efdsearch.senate.gov          (electronic PTR HTML tables)

Best-effort: handwritten/paper filings (image PDFs) can't be parsed and are
skipped. Everything machine-readable is captured. Logs go to stdout for the
Actions run log so you can see exactly what parsed.

Output schema:
{
  "generated_utc": "2026-06-25T09:00:00Z",
  "source": "house+senate official",
  "trades": [
    {"member","chamber","ticker","asset","type","amount_str",
     "amount_mid","tx_date","committee"}
  ]
}
"""

import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None

from committees import tag_committee

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# Pull a slightly wider window than the email shows (email filters to 30 days),
# so late-disclosed trades still land in the file.
WINDOW_DAYS = 40
CUTOFF = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).date()

AMOUNT_RE = re.compile(r"\$[\d,]+(?:\s*-\s*\$[\d,]+)?")
TICKER_RE = re.compile(r"\(([A-Z]{1,5}(?:\.[A-Z])?)\)")


def log(*a):
    print(*a, flush=True)


def parse_amount_mid(s: str):
    nums = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", s or "")]
    if not nums:
        return 0
    return sum(nums) / len(nums)


def to_iso(d: str):
    """Accept M/D/YYYY or MM/DD/YYYY or YYYY-MM-DD -> YYYY-MM-DD (or None)."""
    d = (d or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(d, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _shift(iso: str, days: int):
    """Return an ISO date shifted by N days (for sanity-checking parsed dates)."""
    try:
        return (datetime.fromisoformat(iso).date() + timedelta(days=days)).isoformat()
    except Exception:
        return iso


# ---------------------------------------------------------------------------
# HOUSE
# ---------------------------------------------------------------------------
def parse_house(year: int):
    out = []
    zip_url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    log(f"[house] fetching index {zip_url}")
    try:
        r = requests.get(zip_url, headers=UA, timeout=60)
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = [n for n in zf.namelist() if n.endswith(".xml")][0]
        xml = zf.read(xml_name).decode("utf-8", "ignore")
    except Exception as e:
        log(f"[house] index fetch failed: {e}")
        return out

    # crude XML walk (stdlib ElementTree)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    ptrs = []
    for m in root:
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        iso = to_iso(m.findtext("FilingDate") or "")
        if not iso or datetime.fromisoformat(iso).date() < CUTOFF:
            continue
        first = (m.findtext("First") or "").strip()
        last = (m.findtext("Last") or "").strip()
        doc = (m.findtext("DocID") or "").strip()
        ptrs.append({"name": f"{first} {last}".strip(), "doc": doc, "filed": iso})

    log(f"[house] {len(ptrs)} PTR filings in window")
    if pdfplumber is None:
        log("[house] pdfplumber unavailable — skipping PDF parse")
        return out

    for p in ptrs:
        pdf_url = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{p['doc']}.pdf"
        try:
            pr = requests.get(pdf_url, headers=UA, timeout=60)
            if pr.status_code != 200 or not pr.content:
                continue
            with pdfplumber.open(io.BytesIO(pr.content)) as pdf:
                text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        except Exception as e:
            log(f"[house] pdf {p['doc']} parse error: {e}")
            continue

        if not text.strip():
            continue  # scanned/handwritten image PDF -> not machine-readable

        found = _extract_house_rows(text, p)
        out.extend(found)

    log(f"[house] extracted {len(out)} ticker-level trades")
    return out


def _extract_house_rows(text: str, p: dict):
    """Pull (ticker, type, amount, date) rows from an electronic House PTR PDF."""
    rows = []
    for line in text.splitlines():
        m = TICKER_RE.search(line)
        if not m:
            continue
        ticker = m.group(1)
        # transaction type: a standalone P / S / S (partial) token
        tmatch = re.search(r"\b(P|S)\b(?:\s*\(partial\))?", line)
        if not tmatch:
            continue
        ttype = "BUY" if tmatch.group(1) == "P" else "SELL"
        amount = AMOUNT_RE.search(line)
        amount_str = amount.group(0) if amount else "undisclosed"
        # a date on the line (transaction date is the first one)
        dmatch = re.search(r"\d{1,2}/\d{1,2}/\d{4}", line)
        tx_date = to_iso(dmatch.group(0)) if dmatch else p["filed"]
        # Guard against misparsed dates: a transaction can't sensibly be >120 days
        # before its filing. If it is, the line's date was likely the wrong column.
        if tx_date and tx_date < _shift(p["filed"], -120):
            tx_date = p["filed"]
        asset = line[:m.start()].strip(" .-")[:60] or ticker
        rows.append({
            "member": p["name"],
            "chamber": "House",
            "ticker": ticker,
            "asset": asset,
            "type": ttype,
            "amount_str": amount_str,
            "amount_mid": parse_amount_mid(amount_str),
            "tx_date": tx_date,
            "filed_date": p["filed"],
            "committee": tag_committee(p["name"]),
        })
    return rows


# ---------------------------------------------------------------------------
# SENATE  (eFD electronic PTRs)
# ---------------------------------------------------------------------------
def senate_session():
    s = requests.Session()
    s.headers.update(UA)
    home = s.get("https://efdsearch.senate.gov/search/home/", timeout=60)
    tok = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', home.text)
    if not tok:
        raise RuntimeError("no csrf token on EFD home")
    token = tok.group(1)
    s.post("https://efdsearch.senate.gov/search/home/",
           data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
           headers={"Referer": "https://efdsearch.senate.gov/search/home/"}, timeout=60)
    return s


def parse_senate(year: int):
    out = []
    try:
        s = senate_session()
        csrf = s.cookies.get("csrftoken")
        start = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%m/%d/%Y")
        payload = {
            "start": "0", "length": "100",
            "report_types": "[11]",            # 11 = Periodic Transaction Report
            "filer_types": "[]",
            "submitted_start_date": f"{start} 00:00:00",
            "submitted_end_date": "",
            "candidate_state": "", "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
            "csrftoken": csrf, "csrfmiddlewaretoken": csrf,
        }
        r = s.post("https://efdsearch.senate.gov/search/report/data/", data=payload,
                   headers={"Referer": "https://efdsearch.senate.gov/search/",
                            "X-Requested-With": "XMLHttpRequest",
                            "X-CSRFToken": csrf}, timeout=60)
        data = r.json().get("data", [])
    except Exception as e:
        log(f"[senate] report list failed: {e}")
        return out

    log(f"[senate] {len(data)} filings returned")
    for row in data:
        try:
            first, last = row[0].strip(), row[1].strip()
            link_html = row[3]
            filed = to_iso(row[4].split()[0]) if len(row) > 4 and row[4] else None
            href = BeautifulSoup(link_html, "html.parser").a["href"]
            if "/ptr/" not in href:   # paper filing -> PDF, skip (not machine-readable)
                continue
            url = "https://efdsearch.senate.gov" + href
            out.extend(_parse_senate_ptr(s, url, f"{first} {last}".strip(), filed))
        except Exception as e:
            log(f"[senate] row error: {e}")
            continue

    log(f"[senate] extracted {len(out)} ticker-level trades")
    return out


def _parse_senate_ptr(session, url, member, filed_date):
    rows = []
    try:
        pg = session.get(url, timeout=60)
        soup = BeautifulSoup(pg.text, "html.parser")
        table = soup.find("table")
        if not table:
            return rows
        for tr in table.select("tbody tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) < 8:
                continue
            # columns: #, Tx Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, ...
            tx_date = to_iso(tds[1])
            ticker = tds[3].upper()
            asset = tds[4]
            ttype_raw = tds[6].lower()
            amount_str = tds[7]
            if ticker in ("", "--"):
                continue
            if "purchase" in ttype_raw:
                ttype = "BUY"
            elif "sale" in ttype_raw or "sell" in ttype_raw:
                ttype = "SELL"
            else:
                continue
            # Window is driven by FILING date (handled upstream by the report query),
            # so we keep late-disclosed older trades. tx_date is just detail here.
            rows.append({
                "member": member,
                "chamber": "Senate",
                "ticker": ticker,
                "asset": asset[:60],
                "type": ttype,
                "amount_str": amount_str or "undisclosed",
                "amount_mid": parse_amount_mid(amount_str),
                "tx_date": tx_date or filed_date,
                "filed_date": filed_date,
                "committee": tag_committee(member),
            })
    except Exception as e:
        log(f"[senate] ptr parse error {url}: {e}")
    return rows


# ---------------------------------------------------------------------------
def main():
    year = datetime.now(timezone.utc).year
    trades = []
    trades += parse_house(year)
    trades += parse_senate(year)

    # de-dupe + sort (committee first, then most recent, then biggest)
    seen, deduped = set(), []
    for t in trades:
        k = (t["member"], t["ticker"], t["tx_date"], t["amount_str"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(t)

    # committee members first, then most recently DISCLOSED, then biggest amount
    deduped.sort(key=lambda t: (0 if t["committee"] else 1,
                                _neg_date(t.get("filed_date") or t["tx_date"]),
                                -t["amount_mid"]))

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "house+senate official",
        "window_days": WINDOW_DAYS,
        "count": len(deduped),
        "trades": deduped,
    }
    with open("congress_trades.json", "w") as f:
        json.dump(out, f, indent=2)
    log(f"[done] wrote congress_trades.json with {len(deduped)} trades")


def _neg_date(iso: str):
    """Sort key that puts the most recent date first."""
    try:
        return -datetime.fromisoformat(iso).toordinal()
    except Exception:
        return 0


if __name__ == "__main__":
    main()
