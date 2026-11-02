"""Auth, ownership, rate limits and abuse protection. No network calls:
Supabase and the token check are replaced by fakes.

Run: python -m unittest discover -s tests -t .
"""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("OPENAI_API_KEY", "test")

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app.services import account, analysis_store, jobs, match_store
from app.services.account import AccountError

OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
RESUME = "33333333-3333-3333-3333-333333333333"


# =====================================================
# A tiny in-memory stand-in for the Supabase client
# =====================================================

class FakeQuery:

    def __init__(self, db, table):
        self.db, self.table, self.filters, self.action = db, table, [], ("select", None)

    def select(self, *_):
        return self

    def eq(self, column, value):
        self.filters.append((column, str(value)))
        return self

    def in_(self, column, values):
        self.filters.append((column, {str(v) for v in values}))
        return self

    def lt(self, *_):
        return self

    def limit(self, _):
        return self

    def upsert(self, rows, **_):
        self.action = ("upsert", rows if isinstance(rows, list) else [rows])
        return self

    def update(self, values):
        self.action = ("update", values)
        return self

    def delete(self):
        self.action = ("delete", None)
        return self

    def _matches(self, row):
        return all(str(row.get(c)) in v if isinstance(v, set) else str(row.get(c)) == v for c, v in self.filters)

    def execute(self):
        kind, payload = self.action
        rows = self.db.setdefault(self.table, [])
        if kind == "upsert":
            self.db.setdefault("_writes", []).append((self.table, payload))
            rows.extend(payload)
            return SimpleNamespace(data=payload)
        if kind in ("update", "delete"):
            self.db.setdefault("_writes", []).append((self.table, kind))
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[r for r in rows if self._matches(r)])


class FakeSupabase:

    def __init__(self, db):
        self.db = db

    def table(self, name):
        return FakeQuery(self.db, name)


def fake_db():
    return {
        "resumes": [{"id": RESUME, "user_id": OWNER, "parsed_data": {"text": "Python developer resume"}}],
        "jobs": [],
        "job_matches": [],
    }


def user(user_id):
    return SimpleNamespace(id=user_id, app_metadata={})


def client():
    # No `with`: the startup environment check isn't part of these tests
    return TestClient(main.app, raise_server_exceptions=False)


# =====================================================
# Signed-in endpoints require a valid token
# =====================================================

class AuthRequiredTests(unittest.TestCase):

    def setUp(self):
        main.limiter = main.RateLimiter()

    def test_user_endpoints_reject_missing_token(self):
        c = client()
        self.assertEqual(c.delete("/account").status_code, 401)
        self.assertEqual(c.post("/jobs/for-resume", json={"resume_id": RESUME}).status_code, 401)
        job = {"id": "0" * 36, "title": "Dev", "applyURL": "https://www.adzuna.in/details/1"}
        self.assertEqual(c.post("/jobs/ensure", json=job).status_code, 401)

    def test_invalid_or_expired_token_is_rejected(self):
        expired = AccountError(401, "Your session has expired. Please sign in again.")
        headers = {"Authorization": "Bearer " + "x" * 40}
        with mock.patch.object(main, "verify_user", side_effect=expired), \
                mock.patch.object(account, "verify_user", side_effect=expired):
            c = client()
            self.assertEqual(c.post("/jobs/for-resume", json={"resume_id": RESUME}, headers=headers).status_code, 401)
            self.assertEqual(c.delete("/account", headers=headers).status_code, 401)

    def test_verify_user_treats_auth_errors_as_401(self):
        fake = SimpleNamespace(auth=SimpleNamespace(get_user=mock.Mock(side_effect=Exception("bad signature"))))
        with mock.patch.object(account, "get_supabase", return_value=fake):
            with self.assertRaises(AccountError) as raised:
                account.verify_user("forged.token.value")
        self.assertEqual(raised.exception.status_code, 401)


# =====================================================
# A user can never reach another user's resume
# =====================================================

class OwnershipTests(unittest.TestCase):

    def test_matching_loads_only_the_users_own_resume(self):
        with mock.patch.object(match_store, "get_supabase", return_value=FakeSupabase(fake_db())):
            text, _ = match_store.load_resume_for_matching(OWNER, RESUME)
            self.assertIn("Python", text)
            with self.assertRaises(match_store.MatchStoreError) as raised:
                match_store.load_resume_for_matching(OTHER, RESUME)
            self.assertEqual(raised.exception.status_code, 404)
            # A guessed or malformed id is just "not found"
            with self.assertRaises(match_store.MatchStoreError):
                match_store.load_resume_for_matching(OWNER, "not-a-uuid")

    def test_other_users_resume_returns_404_from_the_endpoint(self):
        with mock.patch.object(main, "verify_user", return_value=user(OTHER)), \
                mock.patch.object(match_store, "get_supabase", return_value=FakeSupabase(fake_db())):
            main.limiter = main.RateLimiter()
            r = client().post("/jobs/for-resume", json={"resume_id": RESUME},
                              headers={"Authorization": "Bearer " + "x" * 40})
        self.assertEqual(r.status_code, 404)

    def test_saving_matches_for_someone_elses_resume_writes_nothing(self):
        db = fake_db()
        with mock.patch.object(match_store, "get_supabase", return_value=FakeSupabase(db)), \
                mock.patch.object(match_store, "verify_user", return_value=user(OTHER)), \
                mock.patch.object(match_store, "save_served_jobs"):
            match_store.save_matches("token", RESUME, [{"id": "j1", "matchScore": 80}])
        self.assertFalse([w for w in db.get("_writes", []) if w[0] in ("job_matches", "resumes")])

    def test_stored_analyses_are_only_used_for_the_owner(self):
        with mock.patch.object(analysis_store, "get_supabase", return_value=FakeSupabase(fake_db())):
            self.assertTrue(analysis_store.owns_resume(OWNER, RESUME))
            self.assertFalse(analysis_store.owns_resume(OTHER, RESUME))
            self.assertFalse(analysis_store.owns_resume(OWNER, "../etc"))

        body = main.AnalyzeRequest(resume_text="r", job_description="j", resume_id=RESUME, job_id=RESUME)
        request = SimpleNamespace(headers={"authorization": "Bearer " + "x" * 40})
        with mock.patch.object(main, "verify_user", return_value=user(OTHER)), \
                mock.patch.object(analysis_store, "owns_resume", return_value=False):
            self.assertIsNone(main._account_context(request, body))


# =====================================================
# Rate limiting
# =====================================================

def request_from(ip="1.2.3.4", headers=None):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=ip))


class RateLimitTests(unittest.TestCase):

    def test_limit_then_429_and_rejected_hits_dont_count(self):
        limiter = main.RateLimiter()
        rule = [("k", 3, 60, "slow down")]
        for _ in range(3):
            limiter.check(rule)
        for _ in range(5):
            with self.assertRaises(HTTPException) as raised:
                limiter.check(rule)
            self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(len(limiter._hits["k"]), 3)

    def test_concurrent_requests_cannot_pass_together(self):
        limiter = main.RateLimiter()
        passed, start = [], threading.Event()

        def hit():
            start.wait()
            try:
                limiter.check([("shared", 10, 60, "")])
                passed.append(1)
            except HTTPException:
                pass

        threads = [threading.Thread(target=hit) for _ in range(60)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()
        self.assertEqual(len(passed), 10)

    def test_guest_ip_comes_from_cloudflare_not_a_spoofed_header(self):
        spoofed = {"x-forwarded-for": "6.6.6.6, 9.9.9.9", "cf-connecting-ip": "9.9.9.9"}
        self.assertEqual(main.client_ip(request_from(headers=spoofed)), "9.9.9.9")
        self.assertEqual(main.client_ip(request_from(headers={"true-client-ip": "8.8.8.8"})), "8.8.8.8")
        self.assertEqual(main.client_ip(request_from("5.5.5.5")), "5.5.5.5")

    def test_guests_and_users_have_separate_ai_limits(self):
        main.limiter = main.RateLimiter()
        guest = request_from(headers={"cf-connecting-ip": "7.7.7.7"})
        for _ in range(main.AI_PER_IP_MINUTE):
            main.enforce_ai_limits(guest)
        with self.assertRaises(HTTPException):
            main.enforce_ai_limits(guest)
        # A signed-in user on the same network has their own limit
        main.enforce_ai_limits(guest, user_id=OWNER)

    def test_pdf_rescoring_is_limited_but_never_blocks_the_pdf(self):
        main.limiter = main.RateLimiter()
        guest = request_from(headers={"cf-connecting-ip": "4.4.4.4"})
        allowed = [main.score_allowed(guest, None) for _ in range(main.SCORE_PER_MINUTE + 5)]
        self.assertEqual(allowed.count(True), main.SCORE_PER_MINUTE)

    def test_uncached_matching_is_limited_per_user(self):
        main.limiter = main.RateLimiter()
        for _ in range(main.MATCH_PER_HOUR):
            main.enforce_match_limits(request_from(), OWNER)
        with self.assertRaises(HTTPException) as raised:
            main.enforce_match_limits(request_from(), OWNER)
        self.assertEqual(raised.exception.status_code, 429)
        main.enforce_match_limits(request_from(), OTHER)


# =====================================================
# Input and resource limits
# =====================================================

class AbuseProtectionTests(unittest.TestCase):

    def setUp(self):
        main.limiter = main.RateLimiter()

    def test_oversized_bodies_are_rejected(self):
        big = b"0" * (main.MAX_BODY_BYTES + 1)
        r = client().post("/resume/upload", files={"file": ("a.pdf", big, "application/pdf")})
        self.assertEqual(r.status_code, 413)

    def test_badly_typed_resume_is_a_422_not_a_500(self):
        for resume in ({"name": "A", "skills": 5}, {"name": ["A"]}, {"experience": "text"}):
            self.assertEqual(client().post("/resume/render", json={"resume": resume}).status_code, 422)

    def test_job_with_null_fields_does_not_break_the_feed(self):
        built = jobs.build_job({"id": "1", "title": None, "company": None, "location": None,
                                "description": None, "redirect_url": "https://www.adzuna.in/details/1"}, 50)
        self.assertEqual((built["title"], built["company"], built["location"]), ("", "Unknown", "Remote"))

    def test_match_cache_is_capped(self):
        saved = dict(jobs._cache)
        try:
            jobs._cache.clear()
            jobs.cache_set("new_grad:in", ["feed"], -1)  # stale feed entries are kept
            for i in range(jobs.MATCH_CACHE_MAX_ENTRIES + 50):
                jobs.cache_set(f"match:in:q:20:{i}", [], 60)
            self.assertEqual(len([k for k in jobs._cache if k.startswith("match:")]), jobs.MATCH_CACHE_MAX_ENTRIES)
            self.assertIn("new_grad:in", jobs._cache)
            self.assertIn(f"match:in:q:20:{jobs.MATCH_CACHE_MAX_ENTRIES + 49}", jobs._cache)
        finally:
            jobs._cache.clear()
            jobs._cache.update(saved)


# =====================================================
# Delete Account removes every file, and can't hang
# =====================================================

class FakeBucket:

    def __init__(self, files, stuck=()):
        self.files, self.stuck = set(files), set(stuck)

    def list(self, folder, options):
        entries = {}
        for path in self.files:
            if path.startswith(folder + "/"):
                rest = path[len(folder) + 1:]
                entries[rest.split("/")[0]] = None if "/" in rest else "file-id"
        items = [{"name": n, "id": i} for n, i in sorted(entries.items())]
        return items[options["offset"]:options["offset"] + options["limit"]]

    def remove(self, paths):
        self.files -= set(paths) - self.stuck


class DeleteFilesTests(unittest.TestCase):

    def _run(self, bucket):
        fake = mock.Mock()
        fake.storage.from_.return_value = bucket
        with mock.patch.object(account, "get_supabase", return_value=fake):
            return account.delete_user_files(OWNER)

    def test_removes_all_files_including_subfolders_and_only_the_users(self):
        files = [f"{OWNER}/{i}.pdf" for i in range(250)] + [f"{OWNER}/a/b.pdf", f"{OWNER}/a/c/d.pdf", f"{OTHER}/x.pdf"]
        bucket = FakeBucket(files)
        self.assertEqual(self._run(bucket), 252)
        self.assertEqual(bucket.files, {f"{OTHER}/x.pdf"})

    def test_a_file_that_wont_delete_fails_instead_of_looping(self):
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            self._run(FakeBucket([f"{OWNER}/a.pdf"], stuck={f"{OWNER}/a.pdf"}))
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
