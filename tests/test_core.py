"""Critical backend logic. No network or AI calls: the AI rubric is stubbed.

Run: python -m unittest discover -s tests -t .
"""

import io
import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test")

import pdfplumber

from app.services import resume_analysis as R
from app.services.jobs import is_entry_level, is_senior_title
from app.services.resume_pdf import LEVELS, fit_resume, render_resume


def _structure(jobs=1, projects=2, bullets=3):
    return {
        "name": "Test Person", "email": "test@example.com", "phone": "", "linkedin": "", "github": "",
        "portfolio": "", "city": "Pune", "contact": "test@example.com",
        "summary": "", "skills": ["Python", "Django", "Git"],
        "education": [{"degree": "B.Tech Computer Science", "school": "Example College", "duration": "2021 - 2025",
                       "location": "", "gpa": "CGPA 8.0", "coursework": "Data Structures, DBMS"}],
        "experience": [
            {"title": f"Intern {i}", "company": f"Company {i}", "duration": "Jan 2024 - Jun 2024", "location": "",
             "bullets": [f"Built REST APIs in Django for feature {i}-{b} used by the team every day" for b in range(bullets)]}
            for i in range(jobs)
        ],
        "projects": [
            {"title": f"Project {i}", "tech": "Python, Django", "link": "", "date": "2023",
             "bullets": [f"Built a Django app number {i}-{b} with user login and search" for b in range(bullets)]}
            for i in range(projects)
        ],
        "certifications": ["Cert A", "Cert B"], "achievements": ["Award A", "Award B"],
    }


KEYWORDS = [
    {"name": "Python", "required": True}, {"name": "Django", "required": True},
    {"name": "PostgreSQL", "required": True}, {"name": "Docker", "required": False},
]
JOB = "Junior Backend Developer. Requirements: Python, Django, PostgreSQL. Nice to have: Docker."


class ScoringTests(unittest.TestCase):

    def setUp(self):
        self._chat = R._chat_json
        R._chat_json = lambda system, user, max_tokens, model=R.MODEL: {"rating": 3}
        R._cache.clear()

    def tearDown(self):
        R._chat_json = self._chat

    def test_same_resume_scores_the_same_twice(self):
        structure = _structure()
        first = R.score_resume(structure, KEYWORDS, JOB)
        R._cache.clear()
        second = R.score_resume(structure, KEYWORDS, JOB)
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(first["breakdown"], second["breakdown"])

    def test_listing_a_skill_raises_required_and_keywords(self):
        structure = _structure()
        before = R.score_resume(structure, KEYWORDS, JOB)
        structure["skills"].append("PostgreSQL")
        after = R.score_resume(structure, KEYWORDS, JOB)
        self.assertGreater(after["total"], before["total"])
        self.assertEqual(after["breakdown"]["required_present"], before["breakdown"]["required_present"] + 1)

    def test_placeholders_do_not_count_as_numbers(self):
        self.assertGreater(R._bullet_quality("Built an API used by 300 students in the college"),
                           R._bullet_quality("Built an API used by [N] students in the college"))


class RewriteCheckTests(unittest.TestCase):

    RESUME = "Built REST APIs in Django for the team. Skills: Python, Django, Git. Made a website in React."
    ORIGINAL = "Built REST APIs in Django for the team"

    def check(self, text, item_text=None):
        return R._check_new_text(text, self.ORIGINAL, self.RESUME, [{"name": "PostgreSQL"}], item_text)

    def test_rejects_invented_numbers(self):
        self.assertIn("numbers", self.check("Built 12 REST APIs in Django for the team"))

    def test_rejects_invented_skills(self):
        self.assertIn("skills", self.check("Built REST APIs in Django and Kubernetes for the team"))

    def test_rejects_missing_job_keywords(self):
        self.assertIsNotNone(self.check("Built REST APIs in Django with PostgreSQL for the team"))

    def test_rejects_leadership_claims(self):
        self.assertIn("leading", self.check("Led the REST API work in Django for the team"))

    def test_skill_only_where_that_item_shows_it(self):
        # React is in the resume, but not in this job's item
        self.assertIn("doesn't show", self.check("Built REST APIs in Django and React for the team",
                                                 item_text="Backend Intern Built REST APIs in Django"))

    def test_accepts_placeholder_results(self):
        self.assertIsNone(self.check("Built REST APIs in Django for the team, cutting response time by [X%]"))


class NewGradFilterTests(unittest.TestCase):

    def test_senior_titles_are_blocked(self):
        for title in ["Senior Software Engineer", "Lead Developer", "Engineering Manager", "Software Engineer II"]:
            self.assertTrue(is_senior_title(title), title)
            self.assertFalse(is_entry_level(title, ""), title)

    def test_entry_level_titles_pass(self):
        for title in ["Junior Developer", "Software Engineer Intern", "Graduate Engineer Trainee"]:
            self.assertFalse(is_senior_title(title), title)
            self.assertTrue(is_entry_level(title, "Freshers welcome"), title)

    def test_more_than_two_years_is_rejected(self):
        self.assertFalse(is_entry_level("Backend Developer", "Requires 4+ years of experience"))
        self.assertTrue(is_entry_level("Backend Developer", "0-1 years of experience"))


class OnePageTests(unittest.TestCase):

    def test_long_resume_is_exactly_one_page_and_never_below_10pt(self):
        long = _structure(jobs=6, projects=10, bullets=6)
        pdf, report = render_resume(long, ["Python", "Django"])
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            self.assertEqual(len(doc.pages), 1)
            self.assertGreaterEqual(min(c["size"] for c in doc.pages[0].chars), 10.0)
        self.assertGreaterEqual(report["font_size"], 10.0)
        self.assertTrue(report["removed"])

    def test_trimming_follows_the_order_and_keeps_the_essentials(self):
        long = _structure(jobs=6, projects=10, bullets=6)
        data, level, removed, _ = fit_resume(long, ["Python"])
        labels = [r["label"] for r in removed]
        # Optional sections go first, in this order
        self.assertEqual(labels[:3], ["Achievements", "Certifications", "Relevant coursework"])
        self.assertTrue(data["education"])
        self.assertTrue(data["skills"])
        self.assertEqual(data["experience"][0]["title"], "Intern 0")  # the most recent job stays
        self.assertIn(level, LEVELS)

    def test_short_resume_keeps_everything(self):
        _, _, removed, _ = fit_resume(_structure(), [])
        self.assertEqual(removed, [])

    def test_text_reads_in_section_order(self):
        pdf, _ = render_resume(_structure(), [])
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            text = doc.pages[0].extract_text()
        order = [text.index(s) for s in ["Education", "Skills", "Experience", "Projects", "Certifications"]]
        self.assertEqual(order, sorted(order))

    def test_users_choice_that_does_not_fit_is_refused(self):
        from app.services.resume_pdf import RenderError
        long = _structure(jobs=6, projects=10, bullets=6)
        everything = {"experience": list(range(6)), "projects": list(range(10)),
                      "certifications": True, "achievements": True, "coursework": True}
        with self.assertRaises(RenderError):
            fit_resume(long, [], everything)


class AlternativesTests(unittest.TestCase):

    def test_either_or_skill_is_satisfied_by_the_other(self):
        keywords = [{"name": "Django", "required": True}, {"name": "FastAPI", "required": True}]
        resolved = R._resolve_alternatives(keywords, {"Django": "bullet", "FastAPI": None},
                                           "Python (Django or FastAPI)")
        fastapi = next(k for k in resolved if k["name"] == "FastAPI")
        self.assertFalse(fastapi["required"])


if __name__ == "__main__":
    unittest.main()
