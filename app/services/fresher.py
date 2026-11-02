import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set


# =====================================================
# FRESHER RULES
# =====================================================
# Decides from a job's title and description (Adzuna gives us only the
# first ~500 characters) whether it suits someone with 0-1 years of
# experience:
#   "exclude"  senior title, or a stated minimum above MAX_MIN_YEARS
#   "fresher"  a stated minimum of 0-1 years, or clear fresher wording
#   "unclear"  nothing either way; the AI check decides (fresher_ai.py)

MAX_MIN_YEARS = 1

# Values above this are company history ("for nearly 100 years"), not a
# requirement
MAX_PLAUSIBLE_YEARS = 15


# -----------------------------------------------------
# Titles
# -----------------------------------------------------

SENIOR_TITLE_WORDS = {
    "senior", "sr", "snr",
    "lead", "leader", "principal", "staff",
    "manager", "mgr", "architect",
    "director", "dir", "head", "chief", "vp", "avp", "vice", "cto",
    "ii", "iii", "iv",
    "experienced", "expert", "mid", "middle",
    # Programs for people returning after a career break, or lateral hires
    "returnee", "returnship", "lateral",
}

# Whole words only, so "leadership" or "ahead" don't trip it. Letter
# boundaries (not \b) so "IN_Sr Associate" or "Lead/Manager" still match.
SENIOR_TITLE_PATTERN = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(SENIOR_TITLE_WORDS)) + r")(?![a-z])"
    # SDE 2, SDE-II, Engineer 3, L4-L9, mid-level, advanced associate
    r"|(?<![a-z])sde[\s-]*(?:[2-9]|ii|iii)(?![a-z0-9])"
    r"|(?<![a-z])(?:engineer|developer|analyst)[\s-]+(?:[2-9]|ii|iii)(?![a-z0-9])"
    r"|(?<![a-z0-9])l[4-9](?![a-z0-9])"
    r"|(?<![a-z])advanced\s+associate",
    re.IGNORECASE
)

# At these banks "Associate" is a mid-level grade (after Analyst)
ASSOCIATE_IS_MID_LEVEL = ("goldman sachs", "morgan stanley", "jpmorgan", "jp morgan", "j.p. morgan", "barclays")


def is_senior_title(title: str, company: str = "") -> bool:

    title = title or ""
    if SENIOR_TITLE_PATTERN.search(title):
        return True

    company = (company or "").lower()
    return bool(
        re.search(r"(?<![a-z])associate(?![a-z])", title, re.IGNORECASE)
        and any(bank in company for bank in ASSOCIATE_IS_MID_LEVEL)
    )


# -----------------------------------------------------
# Years of experience
# -----------------------------------------------------

_NUM = r"(\d{1,2}(?:\.\d{1,2})?)"
_UNIT = r"(years?|yrs?|months?|mos?)(?![a-z])"
_DASH = r"\s*(?:-|–|—|to)\s*"

# Most specific first; each match is consumed so it isn't read twice
_EXPERIENCE_PATTERNS = [
    # "2-4 years", "1.5 -3 Years", "3.00-8.00 Years", "6 months - 1 year"
    ("range", re.compile(_NUM + r"\s*" + r"(?:" + _UNIT + r")?" + _DASH + _NUM + r"\s*\+?\s*" + _UNIT, re.I)),
    # "6 Months 1 Year" (both units, no dash)
    ("range", re.compile(_NUM + r"\s*" + _UNIT + r"\s+" + _NUM + r"\s*\+?\s*" + _UNIT, re.I)),
    # "up to 2 yrs", "less than 1 year": no minimum
    ("upto", re.compile(r"(?:up\s*to|upto|max(?:imum)?\.?(?:\s+of)?|less\s+than|under|below|within)\s*" + _NUM + r"\s*\+?\s*" + _UNIT, re.I)),
    # "minimum 2 years", "at least 3 yrs", "over 3 years", "2+ years"
    ("atleast", re.compile(r"(?:min(?:imum)?\.?(?:\s+of)?|at\s*least|atleast|over|more\s+than|above)\s*" + _NUM + r"\s*\+?\s*" + _UNIT, re.I)),
    ("single", re.compile(_NUM + r"\s*\+?\s*" + _UNIT, re.I)),
    # No unit after the numbers: "Years of Experience: 1 to 3", "Exp: 2-5"
    ("labeled", re.compile(r"(?:(?:min(?:imum)?\.?\s+)?(?:experience|exp)\.?\s*(?:required|level)?\s*[:\-]|min(?:imum)?\.?\s+(?:experience|exp)\.?\s*(?:of)?)\s*" + _NUM
                           + r"(?:" + _DASH + r"\d{1,2}(?:\.\d{1,2})?)?\s*\+?(?![\d.%])", re.I)),
]

# Numbers next to these words aren't an experience requirement: a service
# bond, a course length, company history
_NOT_EXPERIENCE_AFTER = re.compile(
    r"^[^;\n]{0,30}?(?<![a-z])(?:bond|agreement|contract|commitment|service\s+(?:bond|agreement|period|commitment)|"
    r"notice\s+period|probation|warranty|stipend|degree|course|program(?:me)?|curriculum|of\s+age|old|"
    r"in\s+business|of\s+(?:history|legacy|excellence|operations))(?![a-z])",
    re.I
)
_NOT_EXPERIENCE_BEFORE = re.compile(
    r"(?:founded|since|history|legacy|for\s+(?:nearly|over|almost|more\s+than)|over\s+the\s+(?:past|last)|"
    r"anniversary|trusted|bond|agreement|tenure|duration|period|aged?)[^.;\n]{0,25}$",
    re.I
)

_CONTEXT = 40


def _to_years(value: str, unit: str) -> float:
    number = float(value)
    return number / 12 if unit.lower().startswith("mo") else number


def _is_requirement(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - _CONTEXT):start]
    after = text[end:end + _CONTEXT]
    return not (_NOT_EXPERIENCE_BEFORE.search(before) or _NOT_EXPERIENCE_AFTER.search(after))


def min_years_required(text: str) -> Optional[float]:
    """The minimum experience a posting asks for, or None if it doesn't say.

    Ranges count from their low end ("2-4 years" is 2). When a posting
    states several requirements, the highest minimum wins ("3 to 5 years,
    with at least 2 in cloud" is 3)."""

    text = text or ""
    remaining = text
    minimums: List[float] = []

    for kind, pattern in _EXPERIENCE_PATTERNS:
        for match in pattern.finditer(remaining):
            if not _is_requirement(remaining, match.start(), match.end()):
                continue
            groups = match.groups()
            if kind == "range":
                low, low_unit, high, high_unit = groups
                low_years = _to_years(low, low_unit or high_unit)
                if low_years > MAX_PLAUSIBLE_YEARS or _to_years(high, high_unit) < low_years:
                    continue
                minimums.append(low_years)
            elif kind == "upto":
                minimums.append(0.0)
            elif kind == "labeled":
                years = float(groups[0])
                if years > MAX_PLAUSIBLE_YEARS:
                    continue
                minimums.append(years)
            else:
                value, unit = groups
                years = _to_years(value, unit)
                if years > MAX_PLAUSIBLE_YEARS:
                    continue
                minimums.append(years)
        # Blank out what this pattern read so the next one can't reread it
        remaining = pattern.sub(lambda m: " " * len(m.group(0)), remaining)

    return max(minimums) if minimums else None


# -----------------------------------------------------
# Fresher wording and graduation batches
# -----------------------------------------------------

FRESHER_PATTERN = re.compile(
    r"(?<![a-z])(?:freshers?|fresh\s+graduates?|new\s+grad(?:uate)?s?|recent\s+graduates?|"
    r"entry[\s-]+level|graduate\s+(?:engineer\s+)?trainees?|trainees?|interns?|internship|"
    r"campus|no\s+(?:prior\s+)?experience\s+(?:is\s+)?required|apprentice(?:ship)?)(?![a-z])",
    re.I
)

JUNIOR_TITLE = re.compile(r"(?<![a-z])(?:junior|jr|graduate|associate|apprentice)(?![a-z])", re.I)

_YEAR = r"(20[2-3]\d)"
_BATCH_PATTERNS = [
    # "Batch: 2025 & 2026", "batches 2024/2025", "2026 batch"
    re.compile(r"batch(?:es)?\s*(?:of)?\s*[:\-]?\s*" + _YEAR + r"((?:\s*(?:,|/|&|and|-|or)\s*20[2-3]\d)*)", re.I),
    re.compile(_YEAR + r"((?:\s*(?:,|/|&|and|-|or)\s*20[2-3]\d)*)\s*(?:batch(?:es)?|pass[\s-]*outs?|passed\s+out|graduates?|grads?)", re.I),
]


def graduation_batches(text: str) -> List[int]:
    found: Set[int] = set()
    for pattern in _BATCH_PATTERNS:
        for match in pattern.finditer(text or ""):
            found.add(int(match.group(1)))
            found.update(int(y) for y in re.findall(r"20[2-3]\d", match.group(2) or ""))
    return sorted(found)


# -----------------------------------------------------
# Description wording that suggests seniority
# -----------------------------------------------------
# Only a hint: "under the guidance of a Senior Developer" is a junior job,
# so these never exclude on their own; they send the job to the AI check.

SENIORITY_HINT = re.compile(
    r"(?<![a-z])(?:seasoned|extensive\s+experience|proven\s+(?:track\s+record|experience)|"
    r"(?:an?\s+)?experienced\s+[a-z./+#-]*\s*(?:developer|engineer|professional|candidate)|"
    r"senior\s+[a-z./+#-]*\s*(?:developer|engineer)|sde\s*(?:-|\s)?(?:ii|2|iii|3)|"
    r"(?:engineer|developer)\s+(?:ii|iii)(?![a-z])|technical\s+lead|tech\s+lead|team\s+lead|"
    r"architect|mentor(?:ing)?\s+(?:junior|other))",
    re.I
)


@dataclass
class Assessment:
    status: str                         # "exclude", "fresher" or "unclear"
    min_years: Optional[float] = None
    batches: List[int] = field(default_factory=list)
    reason: str = ""


def assess(title: str, description: str, company: str = "") -> Assessment:

    title = title or ""
    description = description or ""
    text = f"{title}. {description}"

    if is_senior_title(title, company):
        return Assessment("exclude", reason="senior title")

    years = min_years_required(text)
    batches = graduation_batches(text)

    if years is not None and years > MAX_MIN_YEARS:
        return Assessment("exclude", years, batches, f"needs {years:g}+ years")

    if years is not None:
        return Assessment("fresher", years, batches, f"needs {years:g} years")

    if batches:
        return Assessment("fresher", None, batches, "graduation batch")

    if SENIORITY_HINT.search(text):
        return Assessment("unclear", reason="seniority wording")

    if FRESHER_PATTERN.search(text):
        return Assessment("fresher", reason="fresher wording")

    return Assessment("unclear", reason="no signal")


# =====================================================
# BROKEN LISTINGS
# =====================================================

MIN_DESCRIPTION_LENGTH = 30

# "Backdoor" placement ads charge freshers for jobs; never a real opening
SPAM_PATTERN = re.compile(r"back\s*door", re.I)


def is_broken(job: dict) -> bool:
    """A served job (build_job shape) with no usable link or content, or a
    known scam pattern."""

    url = (job.get("applyURL") or "").strip().lower()
    title = (job.get("title") or "").strip()
    description = (job.get("description") or "").strip()

    if not url.startswith("https://") or not title or len(description) < MIN_DESCRIPTION_LENGTH:
        return True
    return bool(SPAM_PATTERN.search(f"{title} {description}"))


# =====================================================
# DUPLICATES
# =====================================================
# The same opening is often posted several times: same company and place,
# title differing only in punctuation, prefixes or word order. Or the same
# company posting the identical text under a reworded title or for several
# cities.

DUPLICATE_SIMILARITY = 0.85
SAME_TEXT_TITLE_SIMILARITY = 0.65
SAME_TEXT_CHARS = 300

_TITLE_NOISE = re.compile(
    r"^(?:[a-z]{2,6}[_-]\d+[_-]|ind?[-_ ]+|in_)"   # "Cisco_110_", "IND-", "IN_"
    r"|\b(?:urgent(?:ly)?|hiring|immediate|opening|for|and|the|of|a|an|with)\b"
    r"|[^a-z0-9+#]+",
    re.I
)


def title_words(title: str) -> Set[str]:
    cleaned = _TITLE_NOISE.sub(" ", (title or "").lower().replace("&", " "))
    return {w for w in cleaned.split() if w}


def _same_place(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _opening_text(job: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "", (job.get("description") or "").lower())[:SAME_TEXT_CHARS]


def is_duplicate(a: dict, b: dict) -> bool:
    if _same_place(a.get("company")) != _same_place(b.get("company")):
        return False
    words_a, words_b = title_words(a.get("title")), title_words(b.get("title"))
    if not words_a or not words_b:
        return False
    similarity = len(words_a & words_b) / len(words_a | words_b)

    if similarity >= DUPLICATE_SIMILARITY and _same_place(a.get("location")) == _same_place(b.get("location")):
        return True
    text_a = _opening_text(a)
    return similarity >= SAME_TEXT_TITLE_SIMILARITY and len(text_a) >= 100 and text_a == _opening_text(b)


def dedupe(jobs: Iterable[dict], seen: Optional[List[dict]] = None) -> List[dict]:
    """Keeps the first of each duplicate group, in order. `seen` holds jobs
    already served (e.g. earlier search pages) and is extended in place."""

    kept: List[dict] = []
    seen = seen if seen is not None else []
    by_company: dict = {}
    for job in seen:
        by_company.setdefault(_same_place(job.get("company")), []).append(job)

    for job in jobs:
        group = by_company.setdefault(_same_place(job.get("company")), [])
        if any(job.get("id") == other.get("id") or is_duplicate(job, other) for other in group):
            continue
        group.append(job)
        seen.append(job)
        kept.append(job)

    return kept

