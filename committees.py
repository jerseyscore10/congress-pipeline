# Committee tagging.
#
# Primary source: the @unitedstates/congress-legislators open dataset (YAML),
# which lists EVERY current member and committee assignment with roles, updated
# as Congress reshuffles. We pull it live each run and tag any trader who sits
# on a market-relevant committee — no hand-maintained roster to drift.
#
# If that fetch fails, we fall back to the small static map at the bottom.
# A tiny WATCHLIST also flags notable non-committee traders (e.g. Pelosi).

import re
import requests
import yaml

RAW = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/"

# Which committees count as "market-relevant" (keyword in the official name -> short label).
_MARKET = [
    ("committee on finance", "Finance"),
    ("banking, housing", "Banking"),
    ("financial services", "Financial Services"),
    ("ways and means", "Ways & Means"),
    ("energy and commerce", "Energy & Commerce"),
    ("commerce, science", "Commerce"),
    ("armed services", "Armed Services"),
    ("intelligence", "Intelligence"),
    ("appropriations", "Appropriations"),
    ("budget", "Budget"),
]

# Notable traders to flag even if not on a market committee (normalized "first last").
WATCHLIST = {
    "nancy pelosi": "House (notable trader)",
    "ro khanna": "House (notable trader)",
}


def normalize(name: str) -> str:
    """'Hon. Rudy C. Yakym III' -> 'rudy c yakym' (lowercased, no honorifics/suffixes)."""
    n = (name or "").lower()
    for junk in ["hon.", "mr.", "mrs.", "ms.", "dr.", " iii", " ii", " jr.", " sr.", " jr", " sr"]:
        n = n.replace(junk, " ")
    n = "".join(ch for ch in n if ch.isalpha() or ch.isspace())
    return " ".join(n.split())


def _market_label(name, ctype):
    n = (name or "").lower()
    chamber = "Senate" if ctype == "senate" else "House" if ctype == "house" else "Joint"
    for key, short in _MARKET:
        if key in n:
            return chamber + " " + short
    return None


def _role_suffix(title):
    t = (title or "").lower()
    if "chair" in t and "vice" not in t and "ranking" not in t:
        return "(Chair)"
    if "ranking" in t:
        return "(RM)"
    if "vice chair" in t:
        return "(Vice Chair)"
    return ""


_ROLE_RANK = {"(Chair)": 0, "(RM)": 1, "(Vice Chair)": 2, "": 3}

# Cached candidate index: list of (firsts:set, last:str, label:str)
_INDEX = None


def _build_index():
    def fetch(name):
        return yaml.safe_load(requests.get(RAW + name, timeout=90).text)

    committees = fetch("committees-current.yaml")
    membership = fetch("committee-membership-current.yaml")
    legislators = fetch("legislators-current.yaml")

    # thomas_id -> market label (top-level committees only)
    code_label = {}
    for c in committees:
        lab = _market_label(c.get("name", ""), c.get("type", ""))
        if lab and c.get("thomas_id"):
            code_label[c["thomas_id"]] = lab

    # bioguide -> best market label (highest role wins)
    bio_best = {}
    for code, members in membership.items():
        lab = code_label.get(code)          # subcommittee codes won't be here -> skipped
        if not lab:
            continue
        for m in members:
            bio = m.get("bioguide")
            if not bio:
                continue
            suffix = _role_suffix(m.get("title"))
            full = (lab + " " + suffix).strip()
            rank = _ROLE_RANK[suffix]
            if bio not in bio_best or rank < bio_best[bio][1]:
                bio_best[bio] = (full, rank)

    # Map those bioguides to name parts for matching against trade names
    cands = []
    for L in legislators:
        bio = (L.get("id") or {}).get("bioguide")
        if bio not in bio_best:
            continue
        nm = L.get("name") or {}
        firsts = set()
        for k in ("first", "nickname", "official_full"):
            v = nm.get(k)
            if v:
                tok = normalize(v).split()
                if tok:
                    firsts.add(tok[0])
        last = normalize(nm.get("last", ""))
        if last:
            cands.append((firsts, last, bio_best[bio][0]))
    return cands


def _index():
    global _INDEX
    if _INDEX is None:
        try:
            _INDEX = _build_index()
            print(f"[committees] loaded {len(_INDEX)} market-committee members from live dataset", flush=True)
        except Exception as e:
            _INDEX = []
            print(f"[committees] live dataset unavailable ({e}); using static fallback", flush=True)
    return _INDEX


def _match(name, candidates):
    t = normalize(name)
    if not t:
        return None
    head = t.split()[0]
    best, best_rank = None, 99
    for firsts, last, label in candidates:
        if t.endswith(last) and any(head == f or head.startswith(f) or f.startswith(head) for f in firsts):
            rank = 0 if "(Chair)" in label else 1 if "(RM)" in label else 2 if "(Vice" in label else 3
            if rank < best_rank:
                best, best_rank = label, rank
    return best


def tag_committee(name: str):
    # 1) live committee membership
    idx = _index()
    if idx:
        hit = _match(name, idx)
        if hit:
            return hit
    else:
        # dataset unreachable -> static fallback map
        hit = _static_tag(name)
        if hit:
            return hit
    # 2) notable-trader watchlist (applies in both cases)
    wl = [({k.split()[0]}, k.split()[-1], v) for k, v in WATCHLIST.items()]
    return _match(name, wl)


# ---------------------------------------------------------------------------
# Static fallback (only used if the live dataset can't be fetched).
# ---------------------------------------------------------------------------
COMMITTEES = {
    "mike crapo": "Senate Finance (Chair)", "ron wyden": "Senate Finance (RM)",
    "tim scott": "Senate Banking (Chair)", "elizabeth warren": "Senate Banking (RM)",
    "tom cotton": "Senate Intelligence (Chair)", "mark warner": "Senate Intelligence (RM)",
    "ted cruz": "Senate Commerce (Chair)", "maria cantwell": "Senate Commerce (RM)",
    "roger wicker": "Senate Armed Services (Chair)", "jack reed": "Senate Armed Services (RM)",
    "french hill": "House Financial Services (Chair)", "maxine waters": "House Financial Services (RM)",
    "jason smith": "House Ways & Means (Chair)", "richard neal": "House Ways & Means (RM)",
    "rick crawford": "House Intelligence (Chair)", "jim himes": "House Intelligence (RM)",
    "brett guthrie": "House Energy & Commerce (Chair)", "frank pallone": "House Energy & Commerce (RM)",
}
_FALLBACK = [({k.split()[0]}, k.split()[-1], v) for k, v in COMMITTEES.items()]


def _static_tag(name):
    return _match(name, _FALLBACK)
