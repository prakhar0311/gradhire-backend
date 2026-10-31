import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from app.services.ai_optimizer import get_openai_client, resume_to_text
from app.services.skills import SKILLS, extract_skills


# =====================================================
# RESUME ANALYSIS (Optimize screen)
# =====================================================
# Everything shown on the Optimize screen comes from THIS resume and THIS
# job description:
#
#   1. The resume's own structure is read word for word (AI, checked against
#      the resume text, so nothing can be added).
#   2. The job's keywords come from the job description: known skills found
#      by code, plus requirement phrases the AI copies from the posting
#      (each must appear in the posting verbatim).
#   3. Matched vs missing is decided by code: a keyword is matched only if
#      it (or a known spelling of it) is in the resume.
#   4. The AI rewrites the user's own bullets. Every rewrite is checked: no
#      new numbers, no skills the resume doesn't have, no filler. A broken
#      or generic response is retried once, then reported as an error.
#   5. One scoring function scores the original and the optimized resume.
#      Rule-based parts plus a fixed AI rubric at temperature 0, cached by
#      content, so the same resume and job always get the same score.
#
# Nothing here falls back to sample or placeholder content.

ANALYSIS_VERSION = "6"
MODEL = "gpt-4o-mini"
# Rewriting follows strict rules; this model is set separately so it can
# be stronger than the one used for extraction and the rubric
REWRITE_MODEL = "gpt-4o"
SEED = 7

MAX_RESUME_CHARS = 6000
MAX_JOB_CHARS = 5000
MAX_KEYWORDS = 20
MAX_REWRITE_PASSES = 2

# Score weights (out of 100)
W_KEYWORDS = 40
W_REQUIRED = 20
W_SECTIONS = 10
W_BULLETS = 15
W_RUBRIC = 15


class AnalysisError(Exception):
    """The analysis couldn't be produced reliably; shown as an error with Retry."""

    def __init__(self, message: str):
        self.message = message


# =====================================================
# CACHE (in memory; signed-in results are also stored in Supabase)
# =====================================================

_cache: Dict[str, Tuple[float, object]] = {}
_CACHE_MAX = 500


def _cache_get(key: str):
    entry = _cache.get(key)
    if not entry or time.time() >= entry[0]:
        return None
    return entry[1]


def _cache_set(key: str, value, ttl: int):
    if len(_cache) >= _CACHE_MAX:
        now = time.time()
        for k in [k for k, (expires, _) in _cache.items() if expires <= now]:
            _cache.pop(k, None)
        while len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
    _cache[key] = (time.time() + ttl, value)


def content_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


# =====================================================
# TEXT HELPERS
# =====================================================

def _squash(text: str) -> str:
    """Letters and digits only, lowercase: survives PDF line breaks,
    bullet glyphs and hyphenation."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _clean_line(text: str) -> str:
    text = re.sub(r"^[\s•●▪◦\-–—*·]+", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,14}\]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set:
    """Numbers outside [placeholders]."""
    return set(_NUMBER.findall(_PLACEHOLDER.sub(" ", text or "")))


def _sentence_containing(text: str, term: str) -> str:
    lowered = text.lower()
    index = lowered.find(term.lower())
    if index < 0:
        return ""
    start = max(lowered.rfind(".", 0, index), lowered.rfind("\n", 0, index)) + 1
    ends = [i for i in (lowered.find(".", index), lowered.find("\n", index)) if i >= 0]
    end = min(ends) if ends else len(text)
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    return quote[:200]


# =====================================================
# AI CALLS (temperature 0, fixed seed, JSON only)
# =====================================================

def _chat_json(system: str, user: str, max_tokens: int, model: str = MODEL) -> dict:
    client = get_openai_client()
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        seed=SEED,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("not an object")
    return parsed


# =====================================================
# 1. RESUME STRUCTURE (verbatim)
# =====================================================

STRUCTURE_PROMPT = """You split a resume into sections. COPY TEXT EXACTLY as written in the resume.
Never rephrase, fix, summarize, translate or add anything. If something isn't in the resume, use "" or [].

Return JSON:
{
  "name": string,
  "phone": string, "email": string, "linkedin": string, "github": string, "portfolio": string, "city": string,
  "summary": string,
  "skills": [string],
  "experience": [{"title": string, "company": string, "duration": string, "location": string, "bullets": [string]}],
  "projects": [{"title": string, "tech": string, "link": string, "date": string, "bullets": [string]}],
  "education": [{"degree": string, "school": string, "duration": string, "location": string, "gpa": string, "coursework": string}],
  "certifications": [string],
  "achievements": [string]
}
"linkedin", "github", "portfolio" are the links exactly as written (e.g. "linkedin.com/in/name").
"tech" is the project's tech stack as written (e.g. "Swift, Firebase"), "link" its URL, "date" its date.
"gpa" is the CGPA/GPA/percentage as written (e.g. "CGPA 8.4"); "coursework" the relevant coursework line, without its label.
"bullets" are the lines describing what the person did, copied exactly, without the bullet symbol.
"skills" are the items listed in the resume's skills section, one per entry."""

CONTACT_FIELDS = ("phone", "email", "linkedin", "github", "portfolio", "city")


def _validate_structure(parsed: dict, resume_text: str) -> Tuple[dict, int]:
    """Keeps only text that is really in the resume. Returns (structure,
    number of bullets dropped)."""

    source = _squash(resume_text)
    dropped = 0

    def real(value) -> str:
        text = _clean_line(value) if isinstance(value, str) else ""
        return text if text and _squash(text) in source else ""

    def bullets(values) -> List[str]:
        nonlocal dropped
        kept = []
        for value in values if isinstance(values, list) else []:
            text = real(value)
            if text and len(_squash(text)) >= 8:
                kept.append(text)
            elif isinstance(value, str) and value.strip():
                dropped += 1
        return kept

    def lines(values, limit) -> List[str]:
        return [t for t in (real(v) for v in (values if isinstance(values, list) else [])) if t][:limit]

    structure = {
        "name": real(parsed.get("name")),
        "summary": real(parsed.get("summary")),
        "skills": [s for s in (real(v) for v in (parsed.get("skills") or [])) if s][:40],
        "experience": [],
        "projects": [],
        "education": [],
        "certifications": lines(parsed.get("certifications"), 12),
        "achievements": lines(parsed.get("achievements"), 12),
    }
    for field in CONTACT_FIELDS:
        structure[field] = real(parsed.get(field))
    structure["contact"] = " | ".join(structure[f] for f in CONTACT_FIELDS if structure[f])

    for item in (parsed.get("experience") or [])[:10]:
        if isinstance(item, dict):
            structure["experience"].append({
                "title": real(item.get("title")),
                "company": real(item.get("company")),
                "duration": real(item.get("duration")),
                "location": real(item.get("location")),
                "bullets": bullets(item.get("bullets")),
            })

    for item in (parsed.get("projects") or [])[:12]:
        if isinstance(item, dict):
            structure["projects"].append({
                "title": real(item.get("title")),
                "tech": real(item.get("tech")),
                "link": real(item.get("link")),
                "date": real(item.get("date")),
                "bullets": bullets(item.get("bullets")),
            })

    for item in (parsed.get("education") or [])[:5]:
        if isinstance(item, dict):
            structure["education"].append({
                "degree": real(item.get("degree")),
                "school": real(item.get("school")),
                "duration": real(item.get("duration")),
                "location": real(item.get("location")),
                "gpa": real(item.get("gpa")),
                "coursework": real(item.get("coursework")),
            })

    return structure, dropped


def all_bullets(structure: dict) -> List[dict]:
    """Every bullet with where it lives, in a stable order."""
    found = []
    for section in ("experience", "projects"):
        for item_index, item in enumerate(structure.get(section, [])):
            for bullet_index, text in enumerate(item.get("bullets", [])):
                found.append({
                    "section": section,
                    "item": item_index,
                    "bullet": bullet_index,
                    "heading": item.get("title") or item.get("company") or "",
                    "text": text,
                })
    return found


def read_structure(resume_text: str) -> dict:
    key = "structure:" + content_hash(ANALYSIS_VERSION, resume_text)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    last_error = "unreadable"
    for _ in range(2):
        try:
            parsed = _chat_json(STRUCTURE_PROMPT, f"Resume:\n{resume_text[:MAX_RESUME_CHARS]}", 1800)
            structure, _dropped = _validate_structure(parsed, resume_text)
            if all_bullets(structure) or structure["skills"]:
                _cache_set(key, structure, 3600)
                return structure
            last_error = "no content"
        except Exception as e:
            last_error = type(e).__name__

    logging.warning(f"Resume structure failed: {last_error}")
    raise AnalysisError("We couldn't read your resume's sections. Please try again.")


# =====================================================
# 2. JOB KEYWORDS (from the job description)
# =====================================================

KEYWORDS_PROMPT = """You list what a job posting asks for. Use ONLY the job description given.
List concrete skills, tools, technologies, methods and qualifications (not soft-skill clichés, not the job title, not benefits).
Copy each term EXACTLY as it is written in the job description, 1 to 4 words.
Mark required=false for anything under "nice to have", "preferred", "bonus", "plus" or similar; otherwise true.

Return JSON: {"keywords": [{"term": string, "required": boolean}]}"""

_NICE_TO_HAVE = re.compile(r"nice[\s-]to[\s-]have|preferred|bonus|good to have|is a plus|would be a plus|desirable", re.I)
_SKILL_ORDER = list(SKILLS.keys())


def _is_nice_to_have(job_text: str, term: str) -> bool:
    lowered = job_text.lower()
    index = lowered.find(term.lower())
    marker = _NICE_TO_HAVE.search(job_text)
    return bool(marker and index >= marker.start())


def _canonical(term: str) -> str:
    skills = extract_skills(term)
    # Only when the term IS a skill ("Postgres" → PostgreSQL), not a phrase
    # that merely contains one
    if len(skills) == 1 and len(term.split()) <= 3:
        return next(iter(skills))
    return term.strip()


def read_job_keywords(job_text: str) -> List[dict]:
    key = "keywords:" + content_hash(ANALYSIS_VERSION, job_text)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    keywords: Dict[str, dict] = {}

    # Known skills, found by code
    for skill in sorted(extract_skills(job_text), key=_SKILL_ORDER.index):
        aliases = SKILLS[skill]
        alias = next((a for a in aliases if a in job_text.lower()), skill)
        keywords[skill.lower()] = {
            "name": skill,
            "required": not _is_nice_to_have(job_text, alias),
            "quote": _sentence_containing(job_text, alias),
        }

    # Other requirement phrases, copied from the posting by the AI
    try:
        parsed = _chat_json(KEYWORDS_PROMPT, f"Job description:\n{job_text[:MAX_JOB_CHARS]}", 700)
        for entry in parsed.get("keywords") or []:
            if not isinstance(entry, dict):
                continue
            term = re.sub(r"\s+", " ", str(entry.get("term") or "")).strip(" .,;:")
            if not (2 <= len(term) <= 40) or len(term.split()) > 4:
                continue
            if term.count("(") != term.count(")") or re.search(r"\b(or|and)\b|[,/;]", term, re.I):
                continue  # a list or a broken fragment, not one requirement
            if term.lower() not in job_text.lower():
                continue  # must be in the posting, verbatim
            name = _canonical(term)
            if name.lower() in keywords:
                continue
            if name not in SKILLS and extract_skills(term):
                continue  # a phrase around a skill that's already listed
            keywords[name.lower()] = {
                "name": name,
                "required": bool(entry.get("required", True)) and not _is_nice_to_have(job_text, term),
                "quote": _sentence_containing(job_text, term),
                "phrase": term,
            }
    except Exception as e:
        # Code-found skills alone are still genuine; only log the failure type
        logging.warning(f"Job keyword extraction failed: {type(e).__name__}")

    result = sorted(keywords.values(), key=lambda k: (not k["required"],))[:MAX_KEYWORDS]
    if not result:
        raise AnalysisError("We couldn't find specific requirements in this job description.")

    _cache_set(key, result, 6 * 3600)
    return result


# =====================================================
# 3. MATCHING (code, not AI)
# =====================================================

def _phrase_pattern(term: str) -> re.Pattern:
    words = [re.escape(w) for w in re.split(r"[\s\-/]+", term.lower()) if w]
    body = r"[\s\-/]*".join(words)
    return re.compile(rf"(?<![a-z0-9]){body}s?(?![a-z0-9])")


def _mentions(keyword: dict, text: str) -> bool:
    if not text:
        return False
    if keyword["name"] in SKILLS:
        return keyword["name"] in extract_skills(text)
    return bool(_phrase_pattern(keyword.get("phrase") or keyword["name"]).search(text.lower()))


def keyword_presence(keyword: dict, structure: dict, resume_text: str) -> Optional[str]:
    """"bullet" (shown in use), "listed" (skills/summary/elsewhere) or None."""
    for entry in all_bullets(structure):
        if _mentions(keyword, f"{entry['heading']} {entry['text']}"):
            return "bullet"
    if _mentions(keyword, resume_text) or _mentions(keyword, flatten(structure)):
        return "listed"
    return None


def flatten(structure: dict) -> str:
    return resume_to_text(structure)


# =====================================================
# 4. SCORING (same function for before and after)
# =====================================================

ACTION_VERBS = {
    "achieved", "added", "analysed", "analyzed", "architected", "automated", "built", "championed",
    "cleaned", "coded", "collaborated", "configured", "conducted", "connected", "created", "debugged",
    "decreased", "defined", "delivered", "deployed", "designed", "developed", "drove", "enabled",
    "engineered", "enhanced", "established", "evaluated", "executed", "expanded", "fixed", "built",
    "generated", "guided", "identified", "implemented", "improved", "increased", "integrated",
    "introduced", "launched", "led", "maintained", "managed", "mentored", "migrated", "modeled",
    "modelled", "monitored", "optimized", "optimised", "organized", "owned", "performed", "planned",
    "presented", "produced", "programmed", "prototyped", "published", "reduced", "refactored",
    "researched", "resolved", "restructured", "reviewed", "scaled", "secured", "shipped",
    "simplified", "solved", "spearheaded", "streamlined", "supported", "tested", "trained",
    "transformed", "tuned", "upgraded", "validated", "visualized", "visualised", "wrote",
    "authored", "benchmarked", "calculated", "compiled", "coordinated", "curated", "forecasted",
    "investigated", "measured", "predicted", "queried", "reported", "segmented", "surveyed",
}

RUBRIC_PROMPT = """You rate how relevant a candidate's experience is to a job, using this fixed rubric:
0 = unrelated field
1 = related field, but none of the job's core work
2 = some of the job's core work, mostly indirect
3 = clearly does part of the job's core work
4 = does most of the job's core work, in projects or internships
5 = does nearly all of the job's core work with clear evidence
Judge only the evidence written in the resume. Wording style does not change the rating.
Return JSON: {"rating": integer 0-5}"""


def _rubric(resume_flat: str, job_text: str) -> int:
    key = "rubric:" + content_hash(ANALYSIS_VERSION, resume_flat, job_text)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    for _ in range(2):
        try:
            parsed = _chat_json(
                RUBRIC_PROMPT,
                f"Job description:\n{job_text[:MAX_JOB_CHARS]}\n\nResume:\n{resume_flat[:MAX_RESUME_CHARS]}",
                20,
            )
            rating = int(parsed.get("rating"))
            if 0 <= rating <= 5:
                _cache_set(key, rating, 24 * 3600)
                return rating
        except Exception:
            continue
    raise AnalysisError("We couldn't score your resume right now. Please try again.")


def _without_placeholders(text: str) -> str:
    """Drops each clause that holds an unfilled [placeholder]."""
    lines = []
    for line in text.splitlines():
        clauses = [c for c in re.split(r"(?<=,)", line) if not _PLACEHOLDER.search(c)]
        lines.append("".join(clauses).rstrip(", "))
    return "\n".join(lines)


def _bullet_quality(text: str) -> float:
    words = re.findall(r"[A-Za-z][A-Za-z'/+#.-]*", text)
    if not words:
        return 0.0
    score = 0.0
    if words[0].lower() in ACTION_VERBS:
        score += 0.4
    if _numbers(text):
        score += 0.4  # a real number; [X%] placeholders don't count
    if 8 <= len(text.split()) <= 35:
        score += 0.2
    return score


def _strength_label(value: float) -> str:
    if value >= 0.7:
        return "Strong"
    if value >= 0.4:
        return "Medium"
    return "Weak"


def score_resume(structure: dict, keywords: List[dict], job_text: str, resume_text: str) -> dict:
    """The one scoring function for both versions. Returns the total and
    the breakdown that explains it."""

    flat = flatten(structure)
    source = resume_text + "\n" + flat

    scored = keywords if any(k["required"] for k in keywords) else [dict(k, required=True) for k in keywords]
    required = [k for k in scored if k["required"]]

    presence = {k["name"]: keyword_presence(k, structure, source) for k in keywords}

    evidence = sum(1.0 if presence[k["name"]] == "bullet" else 0.5 if presence[k["name"]] else 0.0 for k in required)
    evidence_ratio = evidence / len(required) if required else 0.0

    required_present = sum(1 for k in required if presence[k["name"]])
    required_ratio = required_present / len(required) if required else 0.0

    sections = [
        bool(structure.get("contact")),
        bool(structure.get("education")),
        bool(structure.get("skills")),
        bool(all_bullets(structure)),
    ]
    sections_ratio = sum(sections) / len(sections)

    bullets = all_bullets(structure)
    bullet_ratio = sum(_bullet_quality(b["text"]) for b in bullets) / len(bullets) if bullets else 0.0

    # Placeholders the user hasn't filled in are not evidence
    rating = _rubric(_without_placeholders(flat), job_text)

    total = (
        evidence_ratio * W_KEYWORDS
        + required_ratio * W_REQUIRED
        + sections_ratio * W_SECTIONS
        + bullet_ratio * W_BULLETS
        + rating / 5 * W_RUBRIC
    )

    matched_count = sum(1 for k in keywords if presence[k["name"]])

    return {
        "total": int(round(total)),
        "breakdown": {
            "keyword_evidence": int(round(evidence_ratio * 100)),
            "keywords_matched": matched_count,
            "keywords_total": len(keywords),
            "required_present": required_present,
            "required_total": len(required),
            "sections_present": sum(sections),
            "sections_total": len(sections),
            "bullet_strength": _strength_label(bullet_ratio),
            "bullet_strength_value": round(bullet_ratio, 2),
            "relevance": rating,
        },
        "presence": presence,
    }


# =====================================================
# 5. REWRITING THE USER'S OWN BULLETS
# =====================================================

REWRITE_PROMPT = """You improve the bullets of THIS person's resume for THIS job. Strict rules:
- Rewrite only the numbered bullets given. Keep the same action and the same thing it was done to: "Cleaned data" stays about cleaning data. Keep the person's plain wording style; it must read like their own resume, not like AI.
- You may add ONLY: a stronger, accurate action verb; the name of a tool or skill the resume shows they used for that work; context stated elsewhere in the resume; a result placeholder.
- Never add skills, tools, technologies, companies, projects or experience that are not in the resume text given.
- Never add numbers, results or purposes the resume doesn't state. Where a measurable result would strengthen the bullet, add it ONLY as a placeholder the person will fill in, e.g. ", cutting load time by [X%]", ", used by [N] students", ", saving [X] hours a week".
- Start with an action verb. One sentence, at most 30 words.
- Where it is true from the resume, name the job's keywords the person already has (listed below) in the bullet where they used them.
- No buzzwords ("results-driven", "passionate", "synergy", "dynamic", "proven track record", "cutting-edge").
- Leave out bullets you can't improve honestly.
Also list "skills_to_list": job keywords from the given list that the person demonstrably has but that are missing from their skills section.

Return JSON: {"bullets": [{"index": integer, "text": string}], "skills_to_list": [string]}"""

_FILLER = re.compile(
    r"results[\s-]driven|passionate|synergy|dynamic|proven track record|cutting[\s-]edge|go[\s-]getter|"
    r"detail[\s-]oriented|team player|hard[\s-]working|self[\s-]motivated|leverag(e|ed|ing) synerg",
    re.I,
)


# An outcome or purpose ("to improve...", ", reducing...") is a claim; a
# rewrite may only add one with a [placeholder] the user fills in.
_OUTCOME = re.compile(
    r"(?:,\s*|\s(?:and|to|by|for|while)\s+)(?:improv|reduc|increas|optimi[sz]|enhanc|boost|driv|"
    r"visuali[sz]|support|aid|help|enabl|ensur|streamlin|maximi[sz]|minimi[sz]|accelerat|"
    r"result|lead|leading|achiev|deliver|sav|automat|simplif|facilitat|empower|scal|transform|"
    r"increas|elevat|strengthen|guarantee)\w*",
    re.I,
)


_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "onto", "that", "this", "those", "these", "their", "them",
    "using", "used", "use", "via", "across", "over", "under", "within", "while", "when", "where", "which",
    "were", "was", "are", "has", "have", "had", "been", "being", "each", "every", "other", "more", "most",
    "also", "such", "both", "than", "then", "through", "about", "after", "before", "between", "during",
    "including", "based", "team", "teams", "work", "worked", "working",
}


def _content_words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z][a-z'-]{3,}", text.lower()) if w not in _STOPWORDS]


def _new_details(text: str, original: str, resume_text: str) -> List[str]:
    """Content words the rewrite adds that appear nowhere in the resume,
    ignoring its first verb and any clause holding a [placeholder]."""
    clauses = re.split(r"[,;]", text)
    kept = " ".join(c for c in clauses if not _PLACEHOLDER.search(c))
    words = _content_words(kept)
    if words and text.split() and words[0] == text.split()[0].lower():
        words = words[1:]
    known = resume_text.lower() + " " + original.lower()
    return [w for w in words if w[:5] not in known and w.rstrip("s") not in known]


def _adds_unstated_outcome(text: str, original: str) -> bool:
    new_outcomes = len(_OUTCOME.findall(text)) > len(_OUTCOME.findall(original))
    return new_outcomes and not _PLACEHOLDER.search(text)


def _check_new_text(text: str, original: str, resume_text: str, missing: List[dict]) -> Optional[str]:
    """Why a rewritten line isn't acceptable, or None."""

    if not text or len(text) > 320:
        return "empty or too long"
    if _squash(text) == _squash(original):
        return "unchanged"
    new_numbers = _numbers(text) - _numbers(original)
    if new_numbers:
        return f"added numbers {sorted(new_numbers)}"
    resume_skills = extract_skills(resume_text)
    invented = extract_skills(text) - resume_skills
    if invented:
        return f"added skills {sorted(invented)}"
    for keyword in missing:
        if _mentions(keyword, text):
            return f"added {keyword['name']}, which the resume doesn't have"
    if _FILLER.search(text):
        return "filler wording"
    if original and _adds_unstated_outcome(text, original):
        return "added a result or purpose the resume doesn't state (use a [placeholder] instead)"
    added = _new_details(text, original, resume_text)
    if len(added) > 1:
        return f"added details the resume doesn't state ({', '.join(added[:4])}); keep to what it says"
    return None


def _rewrite_once(structure: dict, resume_text: str, keywords: List[dict], presence: Dict[str, Optional[str]],
                  focus: str, feedback: str) -> dict:
    bullets = all_bullets(structure)
    have = [k["name"] for k in keywords if presence.get(k["name"])]
    missing = [k for k in keywords if not presence.get(k["name"])]

    numbered = "\n".join(f"{i}. {b['text']}" for i, b in enumerate(bullets))
    user = (
        f"Job keywords the person already has: {', '.join(have) or 'none'}\n"
        f"Job keywords the person does NOT have (never mention): {', '.join(k['name'] for k in missing) or 'none'}\n"
        f"Focus: {focus}\n"
        + (f"Your previous answer was rejected: {feedback}\n" if feedback else "")
        + f"\nFull resume (the only source of facts):\n{resume_text[:MAX_RESUME_CHARS]}\n\nBullets:\n{numbered}"
    )

    parsed = _chat_json(REWRITE_PROMPT, user, 1800, REWRITE_MODEL)
    if not isinstance(parsed.get("bullets"), list):
        raise ValueError("no bullets")

    accepted: Dict[int, str] = {}
    rejected: List[str] = []
    for entry in parsed["bullets"]:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= index < len(bullets)) or index in accepted:
            continue
        text = _clean_line(entry.get("text"))
        problem = _check_new_text(text, bullets[index]["text"], resume_text, missing)
        if problem:
            rejected.append(f"bullet {index}: {problem}")
        else:
            accepted[index] = text

    summary = _clean_line(parsed.get("summary"))
    first_line = next((l.strip() for l in resume_text.splitlines() if l.strip()), "")
    if summary and (
        len(summary.split()) > 60
        or _check_new_text(summary, "", resume_text, missing)
        or len(_new_details(summary, "", resume_text)) > 2
        or re.search(r"\b(he|she|his|her|him|they|their|i|my)\b", summary, re.I)
        or (first_line and first_line.lower() in summary.lower())
    ):
        summary = ""

    skills_to_list = []
    for term in parsed.get("skills_to_list") or []:
        keyword = next((k for k in keywords if k["name"].lower() == str(term).strip().lower()), None)
        if keyword and presence.get(keyword["name"]):
            skills_to_list.append(keyword["name"])

    return {"accepted": accepted, "rejected": rejected, "summary": summary,
            "skills_to_list": skills_to_list, "submitted": len(parsed["bullets"])}


def _rewrite(structure: dict, resume_text: str, keywords: List[dict], presence: Dict[str, Optional[str]],
             focus: str) -> dict:
    """One rewrite, retried once if the answer is malformed or mostly rejected."""

    feedback = ""
    last = None
    for _ in range(2):
        try:
            result = _rewrite_once(structure, resume_text, keywords, presence, focus, feedback)
        except Exception as e:
            feedback = "the answer was not the requested JSON"
            logging.warning(f"Rewrite attempt failed: {type(e).__name__}")
            continue
        last = result
        if result["accepted"] and len(result["accepted"]) >= len(result["rejected"]):
            return result
        feedback = "; ".join(result["rejected"][:6]) or "no bullet was improved"

    if last and last["accepted"]:
        return last
    raise AnalysisError("We couldn't rewrite your bullets reliably. Please try again.")


def _apply(structure: dict, rewrite: dict) -> dict:
    optimized = json.loads(json.dumps(structure))
    for index, entry in enumerate(all_bullets(structure)):
        if index in rewrite["accepted"]:
            optimized[entry["section"]][entry["item"]]["bullets"][entry["bullet"]] = rewrite["accepted"][index]
    listed = {s.lower() for s in optimized.get("skills", [])}
    listed_text = " ".join(optimized.get("skills", []))
    for skill in rewrite["skills_to_list"]:
        if skill.lower() not in listed and skill not in extract_skills(listed_text):
            optimized.setdefault("skills", []).append(skill)
    return optimized


def _weakest_focus(score: dict) -> str:
    b = score["breakdown"]
    gaps = {
        "keywords": 1 - b["keyword_evidence"] / 100,
        "bullets": 1 - b["bullet_strength_value"],
    }
    if max(gaps, key=gaps.get) == "keywords":
        return ("Make sure each job keyword the person already has is named in the bullet where they "
                "actually used it.")
    return ("Make every bullet start with a strong action verb and add a [X] placeholder where a result "
            "could be measured.")


# =====================================================
# PUBLIC: ANALYSIS (fast part) AND OPTIMIZATION
# =====================================================

def _keyword_view(keywords: List[dict], presence: Dict[str, Optional[str]]) -> List[dict]:
    return [
        {"name": k["name"], "required": k["required"], "matched": bool(presence.get(k["name"])),
         "where": presence.get(k["name"]), "quote": k.get("quote", "")}
        for k in keywords
    ]


def analyze(resume_text: str, job_text: str) -> dict:
    """Job keywords, matched vs missing, and the original resume's score."""

    resume_text = (resume_text or "").strip()
    job_text = (job_text or "").strip()
    if len(resume_text) < 100:
        raise AnalysisError("Your resume text is too short to analyse. Upload it again.")
    if len(job_text) < 60:
        raise AnalysisError("This job's description is too short to tailor your resume to.")

    key = "analysis:" + content_hash(ANALYSIS_VERSION, resume_text, job_text)  # both already stripped
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with ThreadPoolExecutor(max_workers=2) as pool:
        structure_future = pool.submit(read_structure, resume_text)
        keywords_future = pool.submit(read_job_keywords, job_text)
        structure = structure_future.result()
        keywords = keywords_future.result()

    before = score_resume(structure, keywords, job_text, resume_text)
    presence = before.pop("presence")

    result = {
        "version": ANALYSIS_VERSION,
        "keywords": _keyword_view(keywords, presence),
        "missing_skills": [
            {"name": k["name"], "required": k["required"], "quote": k.get("quote", "")}
            for k in keywords if not presence.get(k["name"])
        ],
        "score_before": before,
        "_structure": structure,
        "_keywords": keywords,
        "_presence": presence,
    }
    _cache_set(key, result, 3600)
    return result


def optimize(resume_text: str, job_text: str) -> dict:
    """The full result: before/after score, rewritten bullets and the
    optimized resume (for the PDF). At most two rewrite passes."""

    key = "optimized:" + content_hash(ANALYSIS_VERSION, resume_text.strip(), job_text.strip())
    cached = _cache_get(key)
    if cached is not None:
        return cached

    analysis = analyze(resume_text, job_text)
    structure = analysis["_structure"]
    keywords = analysis["_keywords"]
    presence = analysis["_presence"]
    before = analysis["score_before"]

    if not all_bullets(structure):
        raise AnalysisError("We couldn't find bullet points in your resume to improve.")

    best_resume, best_score, best_rewrite = structure, before, None
    best_presence = presence
    focus = "Strengthen each bullet and name the job keywords the person already has where they used them."
    passes = 0

    for _ in range(MAX_REWRITE_PASSES):
        passes += 1
        rewrite = _rewrite(structure, resume_text, keywords, presence, focus)
        candidate = _apply(structure, rewrite)
        candidate_score = score_resume(candidate, keywords, job_text, resume_text)
        candidate_presence = candidate_score.pop("presence")
        # A rewrite that doesn't lower the score is still worth showing; a
        # higher one ends the search
        if best_rewrite is None and candidate_score["total"] >= before["total"] \
                or candidate_score["total"] > best_score["total"]:
            best_resume, best_score, best_rewrite = candidate, candidate_score, rewrite
            best_presence = candidate_presence
        if best_score["total"] > before["total"]:
            break
        focus = _weakest_focus(candidate_score)

    improved = best_score["total"] > before["total"]
    missing_required = [k["name"] for k in keywords if k["required"] and not presence.get(k["name"])]

    if improved:
        message = ""
    elif before["total"] >= 80 or not missing_required:
        message = "Your resume already covers this job well."
    else:
        message = f"To improve further, you'd need: {', '.join(missing_required[:4])} (not in your resume)."
    if improved and missing_required:
        message = f"To go further, you'd need: {', '.join(missing_required[:4])} (not in your resume)."

    bullets_view = []
    if best_rewrite:
        for index, entry in enumerate(all_bullets(structure)):
            if index in best_rewrite["accepted"]:
                improved_text = best_rewrite["accepted"][index]
                bullets_view.append({
                    "section": entry["section"],
                    "heading": entry["heading"],
                    "original": entry["text"],
                    "improved": improved_text,
                    "has_placeholder": bool(_PLACEHOLDER.search(improved_text)),
                })

    # The full structure: the PDF renderer lays it out (resume_pdf.py)
    optimized_resume = json.loads(json.dumps(best_resume))

    # What the rewrite changed, for the "What changed" summary
    listed_before = {s.lower() for s in structure.get("skills", [])}
    changes = {
        "keywords_in_bullets": [
            k["name"] for k in keywords
            if presence.get(k["name"]) != "bullet" and best_presence.get(k["name"]) == "bullet"
        ],
        "skills_listed": [
            s for s in best_resume.get("skills", []) if s.lower() not in listed_before
        ],
        "placeholders": sum(1 for b in bullets_view if b["has_placeholder"]),
    }

    result = {
        "version": ANALYSIS_VERSION,
        "keywords": analysis["keywords"],
        "missing_skills": analysis["missing_skills"],
        "changes": changes,
        "score_before": before,
        "score_after": best_score,
        "improved": improved,
        "passes": passes,
        "message": message,
        "bullets": bullets_view,
        "optimized_resume": optimized_resume,
        "optimized_resume_text": flatten(best_resume),
    }
    _cache_set(key, result, 3600)
    return result


def is_cached(kind: str, resume_text: str, job_text: str) -> bool:
    """kind: "analysis" or "optimized"; True when no AI call is needed."""
    key = f"{kind}:" + content_hash(ANALYSIS_VERSION, resume_text.strip(), job_text.strip())
    return _cache_get(key) is not None


def public_analysis(analysis: dict) -> dict:
    """The analysis without its internal fields."""
    return {k: v for k, v in analysis.items() if not k.startswith("_")}
