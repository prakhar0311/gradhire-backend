import copy
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.services.skills import extract_skills


# =====================================================
# ONE-PAGE ATS RESUME (download and in-app preview)
# =====================================================
# A single-column, text-only resume drawn from the structured resume data:
#
#   Header → Education → Skills → Experience → Projects → Certifications
#   → Achievements   (Experience first for 12+ months of full-time work)
#
# Always exactly one page. The layout is measured before drawing; if it
# doesn't fit, these steps run in order until it does:
#   1. tighter spacing and margins (never under 0.5")
#   2. a smaller body font (never under 10pt)
#   3. leave out Achievements, then Certifications, then coursework
#   4. at most 3 bullets per job and 2 per project (the most relevant)
#   5. at most 3 projects (the most relevant)
#   6. only for very long resumes: older jobs, then fewer bullets on
#      older items (never the header, education, skills or latest job)
# Everything left out is reported, so the app can tell the user.
#
# Text is real and selectable, drawn top to bottom, in Arimo (an open,
# Arial-compatible font, SIL OFL) embedded in the file.

FONT = "Arimo"
FONT_BOLD = "Arimo-Bold"
_FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"

pdfmetrics.registerFont(TTFont(FONT, str(_FONT_DIR / "Arimo-Regular.ttf")))
pdfmetrics.registerFont(TTFont(FONT_BOLD, str(_FONT_DIR / "Arimo-Bold.ttf")))

PAGE_WIDTH, PAGE_HEIGHT = A4
INCH = 72


@dataclass(frozen=True)
class Level:
    margin: float   # inches
    body: float     # pt
    spacing: float  # gap multiplier


# Loosest first: steps 1 and 2 of the fitting order
LEVELS = [
    Level(0.75, 11.0, 1.00),
    Level(0.65, 11.0, 0.90),
    Level(0.55, 11.0, 0.80),
    Level(0.50, 11.0, 0.72),
    Level(0.50, 10.5, 0.72),
    Level(0.50, 10.0, 0.72),
]

MAX_JOB_BULLETS = 3
MAX_PROJECT_BULLETS = 2
MAX_PROJECTS = 3


class RenderError(Exception):
    def __init__(self, message: str, overflow_lines: int = 0):
        self.message = message
        self.overflow_lines = overflow_lines


# =====================================================
# TEXT HELPERS
# =====================================================

_MONTHS = {
    "january": "Jan", "february": "Feb", "march": "Mar", "april": "Apr", "may": "May", "june": "Jun",
    "july": "Jul", "august": "Aug", "september": "Sep", "sept": "Sep", "october": "Oct",
    "november": "Nov", "december": "Dec",
}


def format_dates(text: str) -> str:
    """Same dates, one style: "Jan 2025 – Jun 2025", "2021 – 2025", "Present"."""
    if not text:
        return ""
    out = re.sub(
        r"\b(january|february|march|april|may|june|july|august|september|sept|october|november|december)\b\.?",
        lambda m: _MONTHS[m.group(1).lower()], text, flags=re.I,
    )
    out = re.sub(r"\b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b\.?",
                 lambda m: m.group(1).capitalize(), out, flags=re.I)
    out = re.sub(r"\b(currently|current|till date|to date|ongoing|now|today|present)\b", "Present", out, flags=re.I)
    out = re.sub(r"\s*(?:–|—|-|\bto\b)\s*", " – ", out)
    return re.sub(r"\s+", " ", out).strip(" –")


def _link_url(text: str) -> Optional[str]:
    value = (text or "").strip()
    if not value:
        return None
    if "@" in value and " " not in value and not value.lower().startswith("http"):
        return f"mailto:{value}"
    if re.match(r"^https?://", value, re.I):
        return value
    if re.match(r"^(www\.)?[\w-]+(\.[\w-]+)+(/\S*)?$", value):
        return "https://" + value
    return None


_SKILL_GROUPS = {
    "Languages": {
        "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "C", "Go", "Rust", "Kotlin", "Swift",
        "Dart", "PHP", "Ruby", "Scala", "R", "MATLAB", "Bash", "SQL", "HTML", "CSS", "Objective-C",
    },
    "Frameworks": {
        "React", "React Native", "Angular", "Vue", "Next.js", "Node.js", "Express", "Django", "Flask",
        "FastAPI", "Spring Boot", ".NET", "Flutter", "SwiftUI", "UIKit", "Combine", "Core Data",
        "Jetpack Compose", "Redux", "Tailwind CSS", "Bootstrap", "jQuery", "Hibernate", "TensorFlow",
        "PyTorch", "scikit-learn", "Pandas", "NumPy", "Spark", "Matplotlib", "Seaborn", "XCTest",
    },
    "Databases": {
        "PostgreSQL", "MySQL", "MongoDB", "Redis", "Oracle", "SQL Server", "Firebase", "Supabase",
        "Elasticsearch", "Snowflake", "BigQuery",
    },
}
_GROUP_ORDER = ["Languages", "Frameworks", "Tools", "Databases"]


def group_skills(skills: List[str]) -> List[Tuple[str, List[str]]]:
    """The user's own skill words, grouped. Unrecognised items go under Tools."""
    groups: Dict[str, List[str]] = {name: [] for name in _GROUP_ORDER}
    seen = set()
    for raw in skills:
        skill = (raw or "").strip()
        if not skill or skill.lower() in seen:
            continue
        seen.add(skill.lower())
        canonical = extract_skills(skill)
        # A plain canonical name ("Express") counts even where the skill
        # list only accepts an unambiguous spelling
        canonical |= {name for names in _SKILL_GROUPS.values() for name in names if name.lower() == skill.lower()}
        group = "Tools"
        # Most specific first: "SwiftUI" also matches the Swift language
        for name in ("Frameworks", "Databases", "Languages"):
            if canonical & _SKILL_GROUPS[name]:
                group = name
                break
        groups[group].append(skill)
    return [(name, groups[name]) for name in _GROUP_ORDER if groups[name]]


_MONTH_NUM = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _months_between(duration: str) -> int:
    """Rough length of a role in months (0 if unknown)."""
    text = format_dates(duration).lower()
    points = []
    for month, year in re.findall(r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+)?((?:19|20)\d{2})", text):
        points.append((int(year), _MONTH_NUM.get(month[:3], 1) if month else 1))
    if "present" in text:
        today = date.today()
        points.append((today.year, today.month))
    if len(points) < 2:
        return 0
    (y1, m1), (y2, m2) = points[0], points[-1]
    return max(0, (y2 - y1) * 12 + (m2 - m1))


def experience_first(data: dict) -> bool:
    """Substantial full-time experience: 12+ months in a non-intern role."""
    for job in data.get("experience", []):
        title = (job.get("title") or "").lower()
        if re.search(r"intern|trainee|apprentice|student|fellow", title):
            continue
        if _months_between(job.get("duration") or "") >= 12:
            return True
    return False


# =====================================================
# RELEVANCE (which bullets and projects to keep)
# =====================================================

def _relevance(text: str, keywords: List[str]) -> int:
    if not text:
        return 0
    lowered = text.lower()
    found = extract_skills(text)
    score = 0
    for keyword in keywords:
        if keyword in found or keyword.lower() in lowered:
            score += 2
    # Stronger bullets win ties: an action verb or a real number
    if re.match(r"^[A-Z][a-z]+ed\b|^(Built|Led|Wrote|Ran|Made|Won|Grew|Cut)\b", text):
        score += 1
    if re.search(r"\d", re.sub(r"\[[^\]]*\]", "", text)):
        score += 1
    return score


def _keep_best(bullets: List[str], limit: int, keywords: List[str]) -> Tuple[List[str], int]:
    """The `limit` most relevant bullets, in their original order."""
    if len(bullets) <= limit:
        return bullets, 0
    ranked = sorted(range(len(bullets)), key=lambda i: (-_relevance(bullets[i], keywords), i))[:limit]
    return [bullets[i] for i in sorted(ranked)], len(bullets) - limit


# =====================================================
# LAYOUT (measure and draw with the same code)
# =====================================================

class _Writer:

    def __init__(self, level: Level, pdf: Optional[canvas.Canvas]):
        self.level = level
        self.pdf = pdf
        self.margin = level.margin * INCH
        self.left = self.margin
        self.right = PAGE_WIDTH - self.margin
        self.width = self.right - self.left
        self.y = PAGE_HEIGHT - self.margin
        self.body = level.body
        self.leading = level.body * (1.18 + 0.1 * level.spacing)

    # Runs are (text, font, size, url)

    @staticmethod
    def _width(runs) -> float:
        return sum(pdfmetrics.stringWidth(t, f, s) for t, f, s, _ in runs)

    def _wrap(self, runs, first_width: float, rest_width: float) -> List[list]:
        """Splits runs into lines at spaces; a very long word is split too."""
        lines, line, used = [], [], 0.0
        limit = first_width

        def push():
            nonlocal line, used, limit
            lines.append(line)
            line, used, limit = [], 0.0, rest_width

        for text, font, size, url in runs:
            for index, word in enumerate(re.split(r"(\s+)", text)):
                if not word:
                    continue
                if word.isspace():
                    if line:
                        piece = (" ", font, size, None)
                        line.append(piece)
                        used += pdfmetrics.stringWidth(" ", font, size)
                    continue
                w = pdfmetrics.stringWidth(word, font, size)
                if used + w > limit and line:
                    while line and line[-1][0] == " ":
                        used -= pdfmetrics.stringWidth(" ", line[-1][1], line[-1][2])
                        line.pop()
                    push()
                while w > limit:  # a URL longer than the line
                    cut = len(word)
                    while cut > 1 and pdfmetrics.stringWidth(word[:cut], font, size) > limit:
                        cut -= 1
                    line.append((word[:cut], font, size, url))
                    push()
                    word = word[cut:]
                    w = pdfmetrics.stringWidth(word, font, size)
                line.append((word, font, size, url))
                used += w
        if line:
            lines.append(line)
        return lines

    def _draw_runs(self, runs, x: float, y: float):
        if not self.pdf:
            return
        for text, font, size, url in runs:
            self.pdf.setFont(font, size)
            self.pdf.drawString(x, y, text)
            w = pdfmetrics.stringWidth(text, font, size)
            if url and text.strip():
                self.pdf.linkURL(url, (x, y - 2, x + w, y + size), relative=0, thickness=0)
            x += w

    def gap(self, points: float):
        self.y -= points * self.level.spacing

    def centered(self, runs, size: float):
        for line in self._wrap(runs, self.width, self.width):
            self.y -= size * 1.15
            x = self.left + (self.width - self._width(line)) / 2
            self._draw_runs(line, x, self.y)

    def heading(self, title: str):
        self.gap(9)
        size = self.body + 1.5
        self.y -= size
        self._draw_runs([(title, FONT_BOLD, size, None)], self.left, self.y)
        self.y -= 3
        if self.pdf:
            self.pdf.setLineWidth(0.6)
            self.pdf.line(self.left, self.y, self.right, self.y)
        self.gap(4)

    def row(self, left_runs, right_runs=()):
        """Left text (wrapping if long) with right-aligned text on its first line."""
        right_w = self._width(right_runs)
        first = self.width - (right_w + 10 if right_runs else 0)
        lines = self._wrap(left_runs, max(first, self.width * 0.45), self.width)
        for index, line in enumerate(lines):
            self.y -= self.leading
            self._draw_runs(line, self.left, self.y)
            if index == 0 and right_runs:
                self._draw_runs(right_runs, self.right - right_w, self.y)
        if not lines and right_runs:
            self.y -= self.leading
            self._draw_runs(right_runs, self.right - right_w, self.y)

    def bullet(self, text: str):
        # One punctuation style: no full stop at the end of a bullet
        text = re.sub(r"(?<![.][.])\.\s*$", "", text.strip())
        indent = 12
        lines = self._wrap([(text, FONT, self.body, None)], self.width - indent, self.width - indent)
        for index, line in enumerate(lines):
            self.y -= self.leading
            if index == 0:
                self._draw_runs([("•", FONT, self.body, None)], self.left + 2, self.y)
            self._draw_runs(line, self.left + indent, self.y)

    def paragraph(self, runs):
        for line in self._wrap(runs, self.width, self.width):
            self.y -= self.leading
            self._draw_runs(line, self.left, self.y)

    @property
    def fits(self) -> bool:
        return self.y >= self.margin - 0.01

    @property
    def overflow(self) -> float:
        return max(0.0, self.margin - self.y)


def _layout(data: dict, level: Level, pdf: Optional[canvas.Canvas] = None) -> _Writer:
    w = _Writer(level, pdf)
    body = level.body
    small = max(10.0, body - 0.5)

    # Header: name, then one contact line of plain text and links
    if data.get("name"):
        w.centered([(data["name"], FONT_BOLD, 19 if level.spacing >= 0.8 else 18, None)], 19)
    contact_runs = []
    for field in ("phone", "email", "linkedin", "github", "portfolio", "city"):
        value = (data.get(field) or "").strip()
        if not value:
            continue
        if contact_runs:
            contact_runs.append((" | ", FONT, small, None))
        url = _link_url(value) if field in ("email", "linkedin", "github", "portfolio") else None
        contact_runs.append((value, FONT, small, url))
    if not contact_runs and data.get("contact"):
        contact_runs = [(data["contact"], FONT, small, None)]
    if contact_runs:
        w.gap(3)
        w.centered(contact_runs, small)

    def education():
        items = [e for e in data.get("education", []) if e.get("degree") or e.get("school")]
        if not items:
            return
        w.heading("Education")
        for e in items:
            w.row([(e.get("degree") or e.get("school"), FONT_BOLD, body, None)],
                  [(format_dates(e.get("duration") or ""), FONT, body, None)] if e.get("duration") else [])
            second = ", ".join(p for p in [e.get("school") if e.get("degree") else "", e.get("location")] if p)
            if second or e.get("gpa"):
                w.row([(second, FONT, body, None)], [(e["gpa"], FONT, body, None)] if e.get("gpa") else [])
            if e.get("coursework"):
                w.paragraph([("Relevant coursework: ", FONT_BOLD, body, None), (e["coursework"], FONT, body, None)])
            w.gap(3)

    def skills():
        groups = group_skills(data.get("skills", []))
        if not groups:
            return
        w.heading("Skills")
        for name, items in groups:
            w.paragraph([(f"{name}: ", FONT_BOLD, body, None), (", ".join(items), FONT, body, None)])

    def experience():
        items = [j for j in data.get("experience", []) if j.get("title") or j.get("company")]
        if not items:
            return
        w.heading("Experience")
        for j in items:
            left = [(j.get("title") or j.get("company"), FONT_BOLD, body, None)]
            if j.get("title") and j.get("company"):
                left.append((", " + j["company"], FONT, body, None))
            right_text = " | ".join(p for p in [j.get("location"), format_dates(j.get("duration") or "")] if p)
            w.row(left, [(right_text, FONT, body, None)] if right_text else [])
            for b in j.get("bullets", []):
                w.bullet(b)
            w.gap(4)

    def projects():
        items = [p for p in data.get("projects", []) if p.get("title")]
        if not items:
            return
        w.heading("Projects")
        for p in items:
            left = [(p["title"], FONT_BOLD, body, None)]
            if p.get("tech"):
                left.append((" | " + p["tech"], FONT, body, None))
            right = []
            if p.get("link"):
                right.append((p["link"], FONT, body, _link_url(p["link"])))
            if p.get("date"):
                if right:
                    right.append((" | ", FONT, body, None))
                right.append((format_dates(p["date"]), FONT, body, None))
            w.row(left, right)
            for b in p.get("bullets", []):
                w.bullet(b)
            w.gap(4)

    def listing(title: str, key: str):
        items = [i for i in data.get(key, []) if i]
        if not items:
            return
        w.heading(title)
        for item in items:
            w.bullet(item)

    order = [experience, education] if experience_first(data) else [education]
    order += [skills] + ([] if experience in order else [experience]) + [projects]
    for section in order:
        section()
    listing("Certifications", "certifications")
    listing("Achievements", "achievements")
    return w


def section_order(data: dict) -> List[str]:
    names = ["Experience", "Education"] if experience_first(data) else ["Education"]
    names += ["Skills"] + ([] if "Experience" in names else ["Experience"]) + ["Projects", "Certifications", "Achievements"]
    present = {
        "Education": bool(data.get("education")), "Skills": bool(data.get("skills")),
        "Experience": bool(data.get("experience")), "Projects": bool(data.get("projects")),
        "Certifications": bool(data.get("certifications")), "Achievements": bool(data.get("achievements")),
    }
    return [n for n in names if present[n]]


# =====================================================
# FITTING
# =====================================================

def _first_fit(data: dict) -> Tuple[Optional[Level], float]:
    """The loosest level that fits, or (None, overflow at the tightest)."""
    for level in LEVELS:
        writer = _layout(data, level)
        if writer.fits:
            return level, 0.0
    return None, writer.overflow


def _label(item: dict, kind: str) -> str:
    if kind == "experience":
        return ", ".join(p for p in [item.get("title"), item.get("company")] if p) or "A job"
    return item.get("title") or "A project"


def _auto_fit(data: dict, keywords: List[str]) -> Tuple[dict, Level, List[dict]]:
    removed: List[dict] = []
    level, _ = _first_fit(data)
    if level:
        return data, level, removed

    def attempt():
        return _first_fit(data)[0]

    # 3. Optional sections
    if data.get("achievements"):
        data["achievements"] = []
        removed.append({"kind": "section", "label": "Achievements"})
        if (level := attempt()):
            return data, level, removed
    if data.get("certifications"):
        data["certifications"] = []
        removed.append({"kind": "section", "label": "Certifications"})
        if (level := attempt()):
            return data, level, removed
    if any(e.get("coursework") for e in data.get("education", [])):
        for e in data["education"]:
            e["coursework"] = ""
        removed.append({"kind": "section", "label": "Relevant coursework"})
        if (level := attempt()):
            return data, level, removed

    # 4. Bullet limits, keeping the most relevant
    trimmed = _limit_bullets(data, MAX_JOB_BULLETS, MAX_PROJECT_BULLETS, keywords)
    if trimmed:
        removed.append({"kind": "bullets", "label": f"{trimmed} bullet point{'s' if trimmed != 1 else ''}", "count": trimmed})
        if (level := attempt()):
            return data, level, removed

    # 5. The most relevant projects
    projects = data.get("projects", [])
    if len(projects) > MAX_PROJECTS:
        ranked = sorted(range(len(projects)), key=lambda i: (-_project_relevance(projects[i], keywords), i))
        keep = sorted(ranked[:MAX_PROJECTS])
        for i in sorted(set(range(len(projects))) - set(keep)):
            removed.append({"kind": "project", "label": _label(projects[i], "project")})
        data["projects"] = [projects[i] for i in keep]
        if (level := attempt()):
            return data, level, removed

    # 6. Very long resumes: older jobs, then fewer bullets, then projects
    while len(data.get("experience", [])) > 1:
        job = data["experience"].pop()
        removed.append({"kind": "experience", "label": _label(job, "experience")})
        if (level := attempt()):
            return data, level, removed
    for limit in (2, 1):
        trimmed = _limit_bullets(data, limit, 1, keywords, keep_latest_job=True)
        if trimmed:
            _add_bullets(removed, trimmed)
            if (level := attempt()):
                return data, level, removed
    while data.get("projects"):
        ranked = sorted(range(len(data["projects"])), key=lambda i: _project_relevance(data["projects"][i], keywords))
        project = data["projects"].pop(ranked[0])
        removed.append({"kind": "project", "label": _label(project, "project")})
        if (level := attempt()):
            return data, level, removed
    for limit in (2, 1):
        trimmed = _limit_bullets(data, limit, 1, keywords)
        if trimmed:
            _add_bullets(removed, trimmed)
            if (level := attempt()):
                return data, level, removed

    raise RenderError("This resume can't fit on one page even after trimming.")


def _add_bullets(removed: List[dict], count: int):
    for entry in removed:
        if entry["kind"] == "bullets":
            entry["count"] += count
            entry["label"] = f"{entry['count']} bullet point{'s' if entry['count'] != 1 else ''}"
            return
    removed.append({"kind": "bullets", "label": f"{count} bullet point{'s' if count != 1 else ''}", "count": count})


def _project_relevance(project: dict, keywords: List[str]) -> int:
    text = " ".join([project.get("title") or "", project.get("tech") or ""] + project.get("bullets", []))
    return _relevance(text, keywords)


def _limit_bullets(data: dict, job_limit: int, project_limit: int, keywords: List[str],
                   keep_latest_job: bool = False) -> int:
    trimmed = 0
    for index, job in enumerate(data.get("experience", [])):
        if keep_latest_job and index == 0:
            continue
        job["bullets"], cut = _keep_best(job.get("bullets", []), job_limit, keywords)
        trimmed += cut
    for project in data.get("projects", []):
        project["bullets"], cut = _keep_best(project.get("bullets", []), project_limit, keywords)
        trimmed += cut
    return trimmed


# =====================================================
# PUBLIC
# =====================================================

def render_resume(resume: dict, keywords: Optional[List[str]] = None, include: Optional[dict] = None) -> Tuple[bytes, dict]:
    """(PDF bytes, report). `include` is the user's own choice of jobs,
    projects and optional sections; then only spacing, font size and bullet
    limits are applied, and RenderError says how much is still too long."""

    data = copy.deepcopy(resume or {})
    keywords = [k for k in (keywords or []) if isinstance(k, str)][:40]
    removed: List[dict] = []

    # Remember each item's position, so the report can say which ones made it
    for key in ("experience", "projects"):
        data[key] = [dict(item, _position=i) for i, item in enumerate(data.get(key) or []) if isinstance(item, dict)]
    available = {
        "certifications": bool(data.get("certifications")),
        "achievements": bool(data.get("achievements")),
        "coursework": any(e.get("coursework") for e in data.get("education", [])),
    }

    if (data.get("summary") or "").strip():
        removed.append({"kind": "format", "label": "Summary (not part of this format)"})

    if include is not None:
        jobs, projects = data.get("experience", []), data.get("projects", [])
        keep_jobs = set(include.get("experience", range(len(jobs))))
        keep_projects = set(include.get("projects", range(len(projects))))
        if jobs and 0 not in keep_jobs:
            keep_jobs.add(0)  # the most recent job always stays
        for i, job in enumerate(jobs):
            if i not in keep_jobs:
                removed.append({"kind": "experience", "label": _label(job, "experience"), "chosen": True})
        for i, project in enumerate(projects):
            if i not in keep_projects:
                removed.append({"kind": "project", "label": _label(project, "project"), "chosen": True})
        data["experience"] = [j for i, j in enumerate(jobs) if i in keep_jobs]
        data["projects"] = [p for i, p in enumerate(projects) if i in keep_projects]
        for key, label in (("certifications", "Certifications"), ("achievements", "Achievements")):
            if not include.get(key, True) and data.get(key):
                data[key] = []
                removed.append({"kind": "section", "label": label, "chosen": True})
        if not include.get("coursework", True) and any(e.get("coursework") for e in data.get("education", [])):
            for e in data["education"]:
                e["coursework"] = ""
            removed.append({"kind": "section", "label": "Relevant coursework", "chosen": True})

        level, overflow = _first_fit(data)
        if not level:
            trimmed = _limit_bullets(data, MAX_JOB_BULLETS, MAX_PROJECT_BULLETS, keywords)
            if trimmed:
                _add_bullets(removed, trimmed)
            level, overflow = _first_fit(data)
        if not level:
            lines = max(1, int(overflow // (LEVELS[-1].body * 1.25)) + 1)
            raise RenderError(
                f"Still about {lines} line{'s' if lines != 1 else ''} too long. Remove one more project or job.",
                overflow_lines=lines,
            )
    else:
        data, level, auto_removed = _auto_fit(data, keywords)
        removed += auto_removed

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{data.get('name') or 'Resume'} - Resume")
    pdf.setAuthor(data.get("name") or "")
    pdf.setCreator("GradHire")
    writer = _layout(data, level, pdf)
    if not writer.fits:  # measuring and drawing share the code; this can't happen
        raise RenderError("The resume layout didn't fit one page.")
    pdf.showPage()
    pdf.save()

    report = {
        "pages": 1,
        "font_size": level.body,
        "margin_inches": level.margin,
        "removed": removed,
        "sections": section_order(data),
        "included": {
            "experience": [j["_position"] for j in data.get("experience", [])],
            "projects": [p["_position"] for p in data.get("projects", [])],
            "certifications": bool(data.get("certifications")),
            "achievements": bool(data.get("achievements")),
            "coursework": any(e.get("coursework") for e in data.get("education", [])),
        },
        "available": available,
    }
    return buffer.getvalue(), report
