# Committee map for tagging congressional traders.
# Keyed by lowercase "first last" with punctuation stripped. Values are the
# committee label shown in the email. Keep names simple (no middle initials);
# the matcher also tries last-name-only as a fallback.
#
# Edit freely — this is the single source of truth for "is this person on a
# market-relevant committee." Mirror major changes into CONGRESS_COMMITTEES in
# the Apps Script too (it's only a fallback if a record arrives untagged).

COMMITTEES = {
    # ---- Senate Finance ----
    "mike crapo": "Senate Finance (Chair)",
    "ron wyden": "Senate Finance (RM)",
    "chuck grassley": "Senate Finance",
    "john cornyn": "Senate Finance",
    "lisa murkowski": "Senate Finance",
    "todd young": "Senate Finance",
    "bill cassidy": "Senate Finance",
    "mark warner": "Senate Finance / Intelligence",
    "angus king": "Senate Finance",
    # ---- Senate Banking ----
    "tim scott": "Senate Banking (Chair)",
    "elizabeth warren": "Senate Banking (RM)",
    "mike rounds": "Senate Banking",
    "thom tillis": "Senate Banking",
    "john kennedy": "Senate Banking",
    "cynthia lummis": "Senate Banking",
    "bill hagerty": "Senate Banking",
    "katie britt": "Senate Banking",
    # ---- Senate Intelligence ----
    "tom cotton": "Senate Intelligence (Chair)",
    "dan sullivan": "Senate Intelligence",
    # ---- Senate Commerce ----
    "ted cruz": "Senate Commerce (Chair)",
    "maria cantwell": "Senate Commerce (RM)",
    # ---- Senate Armed Services ----
    "roger wicker": "Senate Armed Services (Chair)",
    "tommy tuberville": "Senate Armed Services",
    "jack reed": "Senate Armed Services (RM)",
    # ---- House Financial Services ----
    "french hill": "House Financial Services (Chair)",
    "maxine waters": "House Financial Services (RM)",
    "brad sherman": "House Financial Services",
    "josh gottheimer": "House Financial Services",
    # ---- House Ways and Means ----
    "jason smith": "House Ways & Means (Chair)",
    "richard neal": "House Ways & Means (RM)",
    "vern buchanan": "House Ways & Means",
    "mike kelly": "House Ways & Means",
    # ---- House Intelligence ----
    "mike turner": "House Intelligence (Chair)",
    "jim himes": "House Intelligence (RM)",
    "dan crenshaw": "House Intelligence",
    # ---- House Energy & Commerce ----
    "bob latta": "House Energy & Commerce",
    "frank pallone": "House Energy & Commerce (RM)",
    # ---- Notable / frequently watched ----
    "nancy pelosi": "House (former Speaker)",
    "ro khanna": "House Armed Services",
}

# Last-name-only fallback (built once). Ambiguous last names are dropped so we
# never mis-tag (e.g. two different "Scott"s).
_last_counts = {}
for _k in COMMITTEES:
    _last = _k.split()[-1]
    _last_counts[_last] = _last_counts.get(_last, 0) + 1
COMMITTEES_BY_LAST = {
    _k.split()[-1]: COMMITTEES[_k]
    for _k in COMMITTEES
    if _last_counts[_k.split()[-1]] == 1
}


def normalize(name: str) -> str:
    """'Hon. Rudy C. Yakym III' / 'Crapo, Michael' -> 'rudy yakym' style key bits."""
    n = name.lower()
    for junk in ["hon.", "mr.", "mrs.", "ms.", "dr.", " iii", " ii", " jr.", " sr.", " jr", " sr"]:
        n = n.replace(junk, " ")
    n = "".join(ch for ch in n if ch.isalpha() or ch.isspace())
    return " ".join(n.split())


def tag_committee(name: str):
    key = normalize(name)
    if key in COMMITTEES:
        return COMMITTEES[key]
    parts = key.split()
    if len(parts) >= 2:
        # try "first last"
        fl = parts[0] + " " + parts[-1]
        if fl in COMMITTEES:
            return COMMITTEES[fl]
        # last-name-only fallback (only when unambiguous)
        if parts[-1] in COMMITTEES_BY_LAST:
            return COMMITTEES_BY_LAST[parts[-1]]
    return None
