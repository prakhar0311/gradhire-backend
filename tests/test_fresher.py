"""Fresher filtering: title rules, experience parsing, duplicates and the AI
check's handling. Examples are real postings (title or description text)
seen in the app. No network or AI calls.

Run: python -m unittest discover -s tests -t .
"""

import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("OPENAI_API_KEY", "test")

from app.services import fresher_ai, jobs
from app.services.fresher import (
    assess, dedupe, graduation_batches, is_broken, is_senior_title, min_years_required,
)


class TitleRuleTests(unittest.TestCase):

    def test_senior_titles_are_excluded(self):
        for title in [
            "Asst Dir-QA Engineer - Databricks & Python",
            "Embedded Software Engineering Technical Leader- BMC, Redfish, L2, L3",
            "Senior Java Backend Developer", "Sr. Python Developer", "Team Lead - Backend",
            "Principal Engineer", "Staff Software Engineer", "Engineering Manager", "Head of Data",
            "Solutions Architect", "VP, Technology", "Software Engineer II", "Software Engineer III",
            "SDE 2", "SDE-II Backend", "SDE III", "Developer 3", "Engineer L4", "Experienced Java Developer",
            "Advanced Associate, Information Security(Fraud Investigation)", "Mid-level Frontend Developer",
            "Regional Account Executive, MaaS Sales Junior Middle",
            "ABB Talent Pool(Unpause - Returnee program) - Technology and Data Science",
        ]:
            self.assertTrue(is_senior_title(title), title)

    def test_entry_titles_are_not_excluded(self):
        for title in [
            "Trainee Software Developer", "Junior Next.js Developer", "Software Engineer, New Grad",
            "Associate Software Engineer", "Graduate Engineer Trainee", "Python Intern",
            "Leadership Development Trainee", "Field Engineer - Diploma Engineer Trainee(2026 passout)",
            "Software Engineer I A - GBS IND", "Headless CMS Developer Intern",
        ]:
            self.assertFalse(is_senior_title(title), title)

    def test_associate_is_mid_level_only_at_those_banks(self):
        title = "AM Public-Bengaluru-Associate-Software Engineering"
        self.assertTrue(is_senior_title(title, "Goldman Sachs"))
        self.assertFalse(is_senior_title(title, "NTT America, Inc."))


class ExperienceParsingTests(unittest.TestCase):

    def test_real_requirements(self):
        cases = {
            # Ranges count from the low end
            "Job Title: Associate Database Administrator Experience: 0-1 Years Shift: 24x7": 0,
            "Experience Level: 0–2 Years Location: Preferably Hyderabad": 0,
            "Junior PHP/Laravel Developer - Education ERP Experience: 1.5 -3 Years": 1.5,
            "HIRING ALERT | PYTHON DEVELOPER Experience: 3–8 Years ... Experience: 3.00-8.00 Years": 3,
            "Java Developer Joining Location: Bangalore Experience - 4 to 6 Yrs": 4,
            "Urgent Hiring Java Developer Experience -2 yrs to 7yrs .": 2,
            "Location Dadar Mumbai Onsite Remote Experience 6 Months 1 Year": 0.5,
            # The highest stated minimum wins
            "3 to 5 years in platform, infrastructure, or SRE — with at least 2 years focused on cloud": 3,
            "Should have total of 5-7 years of experience. Should have min. of 2 years of experience in React": 5,
            # No unit after the numbers
            "Qualifications: Any Graduation Years of Experience: 1 to 3 About Accenture": 1,
            "Role Descriptions: Minimum experience 2 (No internship or training experience will consider)": 2,
            # Single values and limits
            "2 years supporting Oracle Siebel CRM applications in a production environment": 2,
            "Experience: Around 1 year Salary: ₹8,000–₹15,000 per month": 1,
            "Kozhikode, Kerala Full-time Up to 2 yrs": 0,
            "minimum 2 years of experience with Django": 2,
            "3+ years of experience in Java": 3,
        }
        for text, expected in cases.items():
            self.assertEqual(min_years_required(text), expected, text)

    def test_numbers_that_are_not_requirements(self):
        for text in [
            "As a global medical technology leader for nearly 100 years, a patient's mobility",
            "Clean Harbors was founded in 1980 near Boston",
            "Selected candidates sign a 2 years service bond",
            "Eligibility: 4 years B.Tech degree in CSE",
            "With over 25 years as an engineering services provider",
            "Experience: 20% travel",
            "Join a team building great products",
        ]:
            self.assertIsNone(min_years_required(text), text)

    def test_graduation_batches(self):
        self.assertEqual(graduation_batches("Opening For : Fresher Batch : 2026 & 2027 Qualification"), [2026, 2027])
        self.assertEqual(graduation_batches("Field Engineer - Diploma Engineer Trainee(2026 passout)"), [2026])
        self.assertEqual(graduation_batches("Software Engineering Intern 2027 and 2028 Batch Only"), [2027, 2028])
        self.assertEqual(graduation_batches("Product Management Associate - 2027 graduates"), [2027])
        self.assertEqual(graduation_batches("Founded in 2024, we build AI tools"), [])


class AssessTests(unittest.TestCase):

    def test_decisions(self):
        self.assertEqual(assess("Associate Platform Engineer", "3 to 5 years in platform").status, "exclude")
        self.assertEqual(assess("Junior UX Designer", "Experience: 3-5 Years").status, "exclude")
        self.assertEqual(assess("Python Developer", "Experience: 0.6-1 year").status, "fresher")
        # Exactly 1 year is still allowed (only more than 1 is excluded)
        self.assertEqual(assess("Customer Service Associate", "Years of Experience: 1 to 3 years").status, "fresher")
        self.assertEqual(assess("Trainee Engineer", "Graduate Trainee (GT) programme").status, "fresher")
        self.assertEqual(assess("Trainee Software Developer", "Fresher Batch : 2026 & 2027").batches, [2026, 2027])
        # "under the guidance of a Senior ..." is a junior job: never excluded by wording alone
        junior = assess("Junior Next.js Developer", "build features under the guidance of a Senior Next.js Developer")
        self.assertNotEqual(junior.status, "exclude")
        # No signal either way: the AI decides
        self.assertEqual(assess("Java Developer", "Design, develop, and maintain efficient Java code.").status, "unclear")
        self.assertEqual(assess("Java Developer", "We are seeking a seasoned Java Developer").status, "unclear")


def job(title, company="Acme", location="Pune", description="Build and test web applications with the team.", **extra):
    return {"id": f"{title}|{company}|{location}|{len(description)}", "title": title, "company": company,
            "location": location, "description": description, "applyURL": "https://www.adzuna.in/details/1", **extra}


class DuplicateAndBrokenTests(unittest.TestCase):

    def test_same_posting_variants_are_merged(self):
        kept = dedupe([
            job("Junior PHP/Laravel Developer- Education ERP", "CNV AI Labs Inc", "Remote"),
            job("Junior PHP/Laravel Developer - Education ERP", "CNV AI Labs Inc", "Remote"),
            job("Associate Data Scientist - Value & Access Strategy", "Amgen"),
            job("Associate Data Scientist - Value and Access Strategy", "Amgen"),
            job("Cisco_110_Junior Test Automation Engineer", "NewVison"),
            job("Junior Test Automation Engineer", "NewVison"),
        ])
        self.assertEqual(len(kept), 3)

    def test_same_text_in_another_city_is_merged(self):
        text = "Ladybird Web Solution is offering an internship programme for freshers to start their careers " * 3
        kept = dedupe([
            job("Internship For Software Engineers - Freshers", "Ladybird Web Solution", "Pune", text),
            job("Internship For Software Engineers - Freshers", "Ladybird Web Solution", "Hyderabad", text),
        ])
        self.assertEqual(len(kept), 1)

    def test_different_roles_are_kept(self):
        boilerplate = "Join a Montreal headquartered company that helps organizations around the world " * 3
        kept = dedupe([
            job("Front-End Intern", "Explorance", "Pune", boilerplate),
            job("Back-End Intern", "Explorance", "Pune", boilerplate),
            job("Junior PHP Developer", "CNV AI Labs Inc"),
            job("Junior PHP/Laravel Developer - Education ERP", "CNV AI Labs Inc"),
            job("Trainee Engineer", "Black Box"),
            job("Trainee Engineer", "Other Company"),
        ])
        self.assertEqual(len(kept), 6)

    def test_earlier_pages_count_as_seen(self):
        seen = [job("Python Developer", "VY Systems")]
        self.assertEqual(dedupe([job("Python Developer", "VY Systems")], seen), [])

    def test_broken_and_spam_listings(self):
        self.assertTrue(is_broken(job("Dev", applyURL="")))
        self.assertTrue(is_broken(job("Dev", applyURL="http://example.com/job")))
        self.assertTrue(is_broken(job("", description="A real description of a real job here.")))
        self.assertTrue(is_broken(job("Dev", description="Apply")))
        self.assertTrue(is_broken(job("Backdoor&Fresher&&Hyderabad")))
        self.assertFalse(is_broken(job("Junior Developer")))


class AiCheckTests(unittest.TestCase):

    def test_malformed_model_output_is_ignored(self):
        self.assertEqual(fresher_ai.parse_response("not json", 3), {})
        self.assertEqual(fresher_ai.parse_response('{"jobs": "x"}', 3), {})
        parsed = fresher_ai.parse_response(
            '{"jobs": [{"i": 0, "fresher": false, "min_years": 3, "batches": [2025, 1999], "confidence": 0.9},'
            ' {"i": 7, "fresher": true, "confidence": 1}, {"i": 1, "fresher": "yes", "confidence": 1},'
            ' {"i": 2, "fresher": true, "min_years": "two", "confidence": 1.5}]}', 3)
        self.assertEqual(parsed, {0: {"fresher": False, "min_years": 3.0, "batches": [2025], "confidence": 0.9}})

    def test_only_confident_no_answers_hide_a_job(self):
        self.assertTrue(fresher_ai.excludes({"fresher": False, "confidence": 0.8}))
        self.assertFalse(fresher_ai.excludes({"fresher": False, "confidence": 0.5}))
        self.assertFalse(fresher_ai.excludes({"fresher": True, "confidence": 0.9}))
        self.assertFalse(fresher_ai.excludes(None))

    def test_pipeline_uses_rules_then_ai(self):
        listings = [
            job("Senior Java Developer"),
            job("Java Developer", description="Experience: 4 to 6 Yrs. Spring Boot and microservices."),
            job("Trainee Software Developer", description="Opening For : Fresher Batch : 2026 & 2027"),
            job("Java Developer", "Seasoned Co", description="We are seeking a seasoned Java Developer to lead."),
            job("Python Developer", "Unsure Co", description="Develop and maintain applications using Python."),
            job("Backend Developer", "Slow Co", description="Build APIs and services with the backend team."),
        ]
        answers = {
            listings[3]["id"]: {"fresher": False, "min_years": None, "batches": [], "confidence": 0.9},
            listings[4]["id"]: {"fresher": False, "min_years": None, "batches": [], "confidence": 0.4},
            # Slow Co has no answer yet: it's shown, not hidden
        }

        async def classify(unclear, wait):
            return {j["id"]: answers[j["id"]] for j in unclear if j["id"] in answers}

        with mock.patch.object(fresher_ai, "classify", side_effect=classify):
            kept = asyncio.run(jobs.keep_fresher_jobs(listings))

        self.assertEqual([j["company"] for j in kept], ["Acme", "Unsure Co", "Slow Co"])
        self.assertEqual(kept[0]["batches"], [2026, 2027])


if __name__ == "__main__":
    unittest.main()
