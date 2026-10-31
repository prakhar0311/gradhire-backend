# =====================================================
# LOAD ENVIRONMENT (MUST BE FIRST)
# =====================================================

from dotenv import load_dotenv
import os

load_dotenv()


# =====================================================
# IMPORTS
# =====================================================

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Form, Request, Depends, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import List, Optional

import pdfplumber
import io
import re
import json
import time
import logging
from collections import defaultdict, deque

from app.services.ai_optimizer import optimize_resume_ai, resume_to_text
from app.services.jobs import (
    fetch_jobs,
    compute_match_score,
    fetch_new_grad_jobs,
    search_new_grad_jobs,
    clean_search_query,
    SEARCH_QUERY_MAX_LENGTH,
    close_client,
    AdzunaUnavailable
)
from app.services.domain_classifier import generate_job_query
from app.services.resume_pdf import render_resume, RenderError
from fastapi.responses import Response, JSONResponse
from app.services.supabase_client import check_connection
from app.services.account import delete_account, verify_user, AccountError
from app.services.job_store import save_served_jobs, ensure_job, JobStoreError
from app.services.match_store import save_matches, load_resume_for_matching, MatchStoreError
from app.services import resume_analysis, analysis_store
from fastapi.middleware.cors import CORSMiddleware


# =====================================================
# APP CONFIG
# =====================================================

app = FastAPI(
    title="GradHire API",
    description="AI-powered resume optimization and job matching backend",
    version="1.0.0"
)

# CORS CONFIG
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.lovable\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown():
    await close_client()


MAX_FILE_SIZE = 5 * 1024 * 1024
ALLOWED_COUNTRIES = {"in", "us"}
ALLOWED_MAX_DAYS = {1, 3, 7, 30}


# =====================================================
# RATE LIMITING (in-memory; fine for a single instance)
# =====================================================

AI_PER_MINUTE = 5          # OpenAI calls per user per minute
AI_PER_DAY = 30            # OpenAI calls per user per day
AI_GLOBAL_PER_DAY = 500    # Hard cap on OpenAI calls per day across all users
GENERAL_PER_MINUTE = 30    # Other endpoints, per user per minute

MINUTE = 60
DAY = 24 * 60 * 60


class RateLimiter:

    def __init__(self):
        self._hits = defaultdict(deque)
        self._calls = 0

    def _prune(self, key, window, now):
        hits = self._hits[key]
        while hits and now - hits[0] >= window:
            hits.popleft()
        return hits

    def check(self, rules):
        """rules: [(key, limit, window, message)]. Records a hit only if every
        rule passes; otherwise raises 429 with the first failing rule's message."""
        now = time.monotonic()

        for key, limit, window, message in rules:
            if len(self._prune(key, window, now)) >= limit:
                raise HTTPException(429, message)

        for key, _, _, _ in rules:
            self._hits[key].append(now)

        self._calls += 1
        if self._calls % 1000 == 0:
            self._cleanup(now)

    def _cleanup(self, now):
        # Drop idle keys so memory doesn't grow with every IP ever seen
        for key in list(self._hits):
            if not self._hits[key] or now - self._hits[key][-1] >= DAY:
                del self._hits[key]


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Render's proxy puts the real client first in X-Forwarded-For. It can be
    # spoofed, which is why AI_GLOBAL_PER_DAY exists as a hard backstop.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_ai_limits(request: Request):
    ip = client_ip(request)
    limiter.check([
        (f"ai-min:{ip}", AI_PER_MINUTE, MINUTE,
         "Too many requests. Please wait a minute and try again."),
        (f"ai-day:{ip}", AI_PER_DAY, DAY,
         "You've reached today's limit for AI optimizations. Please try again tomorrow."),
        ("ai-global-day", AI_GLOBAL_PER_DAY, DAY,
         "AI optimization is busy right now. Please try again later."),
    ])


def enforce_general_limits(request: Request):
    ip = client_ip(request)
    limiter.check([
        (f"general-min:{ip}", GENERAL_PER_MINUTE, MINUTE,
         "Too many requests. Please wait a minute and try again."),
    ])


logging.basicConfig(level=logging.WARNING)


# =====================================================
# REQUEST MODELS
# =====================================================

class OptimizeRequest(BaseModel):
    resume_text: str = Field(..., min_length=50)
    job_description: str = Field(..., min_length=20)


class AnalyzeRequest(BaseModel):
    resume_text: str = Field(..., min_length=1, max_length=30000)
    job_description: str = Field(..., min_length=1, max_length=20000)
    # Signed-in users: the saved resume and the job, so the result is stored
    resume_id: Optional[str] = Field(None, max_length=36)
    job_id: Optional[str] = Field(None, max_length=36)


class ResumeMatchRequest(BaseModel):
    resume_id: str = Field(..., max_length=36)
    country: str = Field("in", max_length=4)


class DeleteAccountRequest(BaseModel):
    # Fresh Sign in with Apple authorization code, used to revoke Apple tokens
    apple_authorization_code: Optional[str] = Field(None, max_length=2048)


class EnsureJobRequest(BaseModel):
    # The job as the app received it from /jobs/*; the id must match the URL
    id: str = Field(..., min_length=36, max_length=36)
    title: str = Field(..., min_length=1, max_length=1000)
    company: Optional[str] = Field(None, max_length=1000)
    location: Optional[str] = Field(None, max_length=1000)
    description: Optional[str] = Field(None, max_length=50000)
    applyURL: str = Field(..., min_length=10, max_length=2048)
    postedAt: Optional[str] = Field(None, max_length=64)


class ScoreRequest(BaseModel):
    job_description: str = Field(..., min_length=1)
    resume_texts: List[str] = Field(..., min_length=1, max_length=4)


# =====================================================
# OPTIMIZED RESUME SHAPE (shared by optimize + download)
# =====================================================

def build_resume_data(optimized: dict) -> dict:

    # Nothing is filled in: a missing name or contact stays missing
    data = {
        "name": optimized.get("name") or "",
        "contact": optimized.get("contact") or "",
        "summary": optimized.get("summary") or "",
        "skills": optimized.get("skills") or [],
        "experience": optimized.get("experience") or [],
        "projects": optimized.get("projects") or [],
        "education": optimized.get("education") or [],
        "certifications": optimized.get("certifications") or [],
        "achievements": optimized.get("achievements") or [],
    }
    for field in ("phone", "email", "linkedin", "github", "portfolio", "city"):
        data[field] = optimized.get(field) or ""
    return data


# =====================================================
# TEXT NORMALIZATION
# =====================================================

def normalize_text(text: str) -> str:

    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text)

    return text.lower().strip()


# =====================================================
# INDUSTRY-GRADE RESUME VALIDATION (GradHire)
# =====================================================

TECH_KEYWORDS = {

    # languages
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "swift",
    "kotlin", "rust", "matlab", "r",

    # web
    "react", "angular", "vue", "node", "express", "html", "css",
    "frontend", "backend", "fullstack",

    # database
    "sql", "mysql", "postgresql", "mongodb", "firebase", "redis",

    # cloud / devops
    "aws", "azure", "gcp", "docker", "kubernetes", "jenkins",
    "ci/cd", "devops",

    # mobile
    "ios", "android", "flutter", "react native",

    # AI/ML
    "machine learning", "deep learning", "pytorch", "tensorflow",
    "scikit", "neural network", "nlp", "computer vision",
    "huggingface",

    # tools / backend
    "git", "github", "api", "rest", "fastapi", "flask", "django",

    # data
    "pandas", "numpy", "data analysis", "data science"
}


RESUME_SECTION_KEYWORDS = {

    "education",
    "experience",
    "projects",
    "skills",
    "technical skills",
    "work experience",
    "academic projects",
    "internship",
    "summary"
}


NON_RESUME_KEYWORDS = {

    "invoice",
    "receipt",
    "boarding pass",
    "ticket",
    "payment receipt",
    "bank statement"
}


MIN_WORD_COUNT = 120


def is_valid_resume(text: str) -> bool:

    if not text:
        return False

    text_lower = normalize_text(text)

    words = text_lower.split()

    # Minimum length check
    if len(words) < MIN_WORD_COUNT:
        return False


    # Must contain resume sections
    has_section = any(
        keyword in text_lower
        for keyword in RESUME_SECTION_KEYWORDS
    )

    if not has_section:
        return False


    # Must contain tech keywords
    tech_matches = sum(
        keyword in text_lower
        for keyword in TECH_KEYWORDS
    )

    if tech_matches < 2:
        return False


    # Reject obvious non-resumes
    non_resume_matches = sum(
        keyword in text_lower
        for keyword in NON_RESUME_KEYWORDS
    )

    if non_resume_matches >= 2:
        return False


    # Must contain core resume sections (important for fresh grads)
    core_sections = [

        "education",
        "projects",
        "experience",
        "internship"
    ]

    has_core = any(
        section in text_lower
        for section in core_sections
    )

    if not has_core:
        return False


    return True


# =====================================================
# PDF EXTRACTION
# =====================================================

async def extract_resume_text(file: UploadFile) -> str:

    if not file.filename:
        raise HTTPException(400, "Missing file")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF resumes are allowed")

    content = await file.read()

    if not content:
        raise HTTPException(400, "Empty file")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "Resume too large")

    try:

        text = ""

        with pdfplumber.open(io.BytesIO(content)) as pdf:

            if len(pdf.pages) == 0:
                raise HTTPException(400, "Invalid PDF")

            for page in pdf.pages:

                extracted = page.extract_text()

                if extracted:
                    text += extracted + "\n"

    except HTTPException:
        raise

    except Exception as e:

        logging.error(f"PDF extraction failed: {e}")

        raise HTTPException(400, "Invalid PDF file")

    if not is_valid_resume(text):

        raise HTTPException(
            400,
            "This document does not appear to be a resume"
        )

    return text


# =====================================================
# ROOT
# =====================================================

@app.get("/")
def root():

    return {
        "status": "ok",
        "service": "GradHire backend",
        "version": "1.0"
    }


# =====================================================
# SUPABASE HEALTH CHECK
# =====================================================

@app.get("/health/supabase", dependencies=[Depends(enforce_general_limits)])
async def supabase_health():

    # The Supabase client is synchronous; keep it off the event loop
    result = await run_in_threadpool(check_connection)

    if not result["ok"]:
        raise HTTPException(503, f"Supabase: {result['reason']}")

    return {"supabase": "ok", "jobs_table": "reachable"}


# =====================================================
# DELETE ACCOUNT
# =====================================================

@app.delete("/account", dependencies=[Depends(enforce_general_limits)])
async def delete_account_endpoint(
    request: Request,
    body: Optional[DeleteAccountRequest] = None
):

    auth_header = request.headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer ") or len(auth_header) < 20:
        raise HTTPException(401, "Please sign in to delete your account.")

    access_token = auth_header[7:].strip()
    apple_code = body.apple_authorization_code if body else None

    try:
        return await run_in_threadpool(delete_account, access_token, apple_code)
    except AccountError as e:
        raise HTTPException(e.status_code, e.message)


# =====================================================
# ENSURE A JOB EXISTS (before the app tracks it)
# =====================================================
# The app saves applications straight to Supabase; this is only called when
# that fails because the job isn't in the shared jobs table yet.

@app.post("/jobs/ensure", dependencies=[Depends(enforce_general_limits)])
async def ensure_job_endpoint(request: Request, body: EnsureJobRequest):

    auth_header = request.headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer ") or len(auth_header) < 20:
        raise HTTPException(401, "Please sign in to track jobs.")

    try:
        await run_in_threadpool(verify_user, auth_header[7:].strip())
        return await run_in_threadpool(ensure_job, body.model_dump())
    except AccountError as e:
        raise HTTPException(e.status_code, e.message)
    except JobStoreError as e:
        raise HTTPException(e.status_code, e.message)


# =====================================================
# UPLOAD RESUME
# =====================================================

@app.post("/resume/upload", dependencies=[Depends(enforce_general_limits)])
async def upload_resume(file: UploadFile = File(...)):

    text = await extract_resume_text(file)

    return {
        "filename": file.filename,
        "status": "resume validated",
        "text": text
    }


# =====================================================
# JOB SEARCH
# =====================================================

@app.post("/jobs/from-resume", dependencies=[Depends(enforce_general_limits)])
async def jobs_from_resume(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    country: str = Query("in"),
    # Set by signed-in users whose resume is saved to their account; the
    # matches are then stored for the dashboard
    resume_id: Optional[str] = Form(None, max_length=36)
):

    if country not in ALLOWED_COUNTRIES:
        raise HTTPException(400, "Unsupported country")

    text = await extract_resume_text(file)

    query = generate_job_query(text)

    try:

        # fetch_jobs already falls back to generic entry-level queries
        jobs = await fetch_jobs(
            query=query,
            country=country,
            resume_text=text
        )

        # Keep the shared jobs table in step with what users can track, and
        # store the matches when the resume is saved to the user's account
        auth_header = request.headers.get("authorization", "")
        if resume_id and auth_header.lower().startswith("bearer "):
            background_tasks.add_task(save_matches, auth_header[7:].strip(), resume_id, jobs)
        else:
            background_tasks.add_task(save_served_jobs, jobs)

        return jobs

    except AdzunaUnavailable:

        raise HTTPException(503, "Job search is temporarily unavailable. Please try again.")

    except Exception as e:

        logging.error(f"Job fetch failed: {type(e).__name__}")

        raise HTTPException(500, "Could not load jobs. Please try again.")


# Matches for a resume saved to the user's account, using its stored text
# (no PDF upload). Jobs scored before keep their stored score.
@app.post("/jobs/for-resume", dependencies=[Depends(enforce_general_limits)])
async def jobs_for_resume(
    request: Request,
    body: ResumeMatchRequest,
    background_tasks: BackgroundTasks
):

    if body.country not in ALLOWED_COUNTRIES:
        raise HTTPException(400, "Unsupported country")

    auth_header = request.headers.get("authorization", "")

    if not auth_header.lower().startswith("bearer ") or len(auth_header) < 20:
        raise HTTPException(401, "Please sign in to see your matches.")

    access_token = auth_header[7:].strip()

    try:
        user = await run_in_threadpool(verify_user, access_token)
        text, stored = await run_in_threadpool(load_resume_for_matching, user.id, body.resume_id)
    except AccountError as e:
        raise HTTPException(e.status_code, e.message)
    except MatchStoreError as e:
        raise HTTPException(e.status_code, e.message)
    except Exception as e:
        logging.error(f"Loading resume for matching failed: {type(e).__name__}")
        raise HTTPException(500, "Could not load your matches. Please try again.")

    try:

        fetched = await fetch_jobs(
            query=generate_job_query(text),
            country=body.country,
            resume_text=text
        )

        # Copies: fetch_jobs results are cached and shared between requests
        jobs = []
        for job in fetched:
            job = dict(job)
            previous = stored.get(job.get("id"))
            if previous:
                job["matchScore"] = previous["match_score"]
                job["matchedSkills"] = previous.get("matched_skills") or job.get("matchedSkills") or []
                job["missingSkills"] = previous.get("missing_skills") or job.get("missingSkills") or []
            jobs.append(job)

        background_tasks.add_task(save_matches, access_token, body.resume_id, jobs)

        return jobs

    except AdzunaUnavailable:

        raise HTTPException(503, "Job search is temporarily unavailable. Please try again.")

    except Exception as e:

        logging.error(f"Resume match failed: {type(e).__name__}")

        raise HTTPException(500, "Could not load jobs. Please try again.")


@app.get("/jobs/new-grad", dependencies=[Depends(enforce_general_limits)])
async def new_grad_jobs(
    background_tasks: BackgroundTasks,
    country: str = Query("in"),
    page: int = Query(1, ge=1, le=50),
    max_days: Optional[int] = Query(None),
    q: Optional[str] = Query(None, max_length=SEARCH_QUERY_MAX_LENGTH)
):

    if country not in ALLOWED_COUNTRIES:
        raise HTTPException(400, "Unsupported country")

    if max_days is not None and max_days not in ALLOWED_MAX_DAYS:
        raise HTTPException(400, "Unsupported max_days")

    query = None

    if q is not None:
        query = clean_search_query(q)
        if query is None:
            raise HTTPException(422, "Search needs at least 2 letters or numbers")

    try:

        if query:
            result = await search_new_grad_jobs(query=query, country=country, page=page, max_days=max_days)
        else:
            result = await fetch_new_grad_jobs(country=country, page=page, max_days=max_days)

        background_tasks.add_task(save_served_jobs, result["jobs"], True)

        return result

    except AdzunaUnavailable:

        raise HTTPException(503, "Job search is temporarily unavailable. Please try again.")

    except Exception as e:

        logging.error(f"New grad fetch failed: {type(e).__name__}")

        raise HTTPException(500, "Could not load jobs. Please try again.")


# =====================================================
# RESUME OPTIMIZATION
# =====================================================

@app.post("/resume/optimize", dependencies=[Depends(enforce_ai_limits)])
async def optimize_resume(request: OptimizeRequest):

    try:

        # OpenAI's client is synchronous; keep it off the event loop
        result = await run_in_threadpool(
            optimize_resume_ai,
            request.resume_text,
            request.job_description
        )

        improved_bullets = []

        for job in result.get("experience", []):

            improved_bullets.extend(job.get("bullets", []))

        optimized_resume = build_resume_data(result)

        return {

            "missing_skills": result.get("missing_skills", [])[:10],

            "improved_bullets": improved_bullets[:5],

            "ats_keywords": result.get("skills", [])[:10],

            # The exact resume shown/scored/downloaded by the app
            "optimized_resume": optimized_resume,

            "optimized_resume_text": resume_to_text(optimized_resume)
        }

    except Exception as e:

        logging.error(f"Optimize failed: {str(e)}")

        raise HTTPException(
            status_code=500,
            detail="Resume optimization failed"
        )


# =====================================================
# RESUME ANALYSIS + OPTIMIZATION (Optimize screen, v2)
# =====================================================
# Two steps so the app's progress is real: /resume/analysis (job keywords,
# matched vs missing, the original's score) then /resume/optimize-v2
# (rewritten bullets, the optimized resume and its score). Signed-in users'
# results are stored per (resume, job) and reused while the text is the
# same. /resume/optimize stays as it was for older app versions.

def _account_context(request: Request, body: AnalyzeRequest):
    """(user_id, resume_hash, job_hash) when the result can be stored, else None."""
    auth_header = request.headers.get("authorization", "")
    if not (body.resume_id and body.job_id and auth_header.lower().startswith("bearer ")):
        return None
    try:
        user = verify_user(auth_header[7:].strip())
    except AccountError:
        return None
    if not analysis_store.owns_resume(user.id, body.resume_id):
        return None
    return (
        str(user.id),
        resume_analysis.content_hash(body.resume_text.strip()),
        resume_analysis.content_hash(body.job_description.strip()),
    )


def _analysis_error(e: Exception):
    if isinstance(e, resume_analysis.AnalysisError):
        return HTTPException(422, e.message)
    logging.error(f"Resume analysis failed: {type(e).__name__}")
    return HTTPException(500, "Resume analysis failed. Please try again.")


@app.post("/resume/analysis", dependencies=[Depends(enforce_general_limits)])
async def resume_analysis_endpoint(request: Request, body: AnalyzeRequest):

    context = await run_in_threadpool(_account_context, request, body)

    if context:
        stored = await run_in_threadpool(
            analysis_store.load, context[0], body.resume_id, body.job_id,
            context[1], context[2], resume_analysis.ANALYSIS_VERSION
        )
        if stored:
            return {"analysis": {k: stored[k] for k in ("version", "keywords", "missing_skills", "score_before")},
                    "result": stored}

    if not resume_analysis.is_cached("analysis", body.resume_text, body.job_description):
        enforce_ai_limits(request)

    try:
        analysis = await run_in_threadpool(resume_analysis.analyze, body.resume_text, body.job_description)
    except Exception as e:
        raise _analysis_error(e)

    return {"analysis": resume_analysis.public_analysis(analysis), "result": None}


@app.post("/resume/optimize-v2", dependencies=[Depends(enforce_general_limits)])
async def resume_optimize_v2(request: Request, body: AnalyzeRequest, background_tasks: BackgroundTasks):

    context = await run_in_threadpool(_account_context, request, body)

    if context:
        stored = await run_in_threadpool(
            analysis_store.load, context[0], body.resume_id, body.job_id,
            context[1], context[2], resume_analysis.ANALYSIS_VERSION
        )
        if stored:
            return stored

    if not resume_analysis.is_cached("optimized", body.resume_text, body.job_description):
        enforce_ai_limits(request)

    try:
        result = await run_in_threadpool(resume_analysis.optimize, body.resume_text, body.job_description)
    except Exception as e:
        raise _analysis_error(e)

    if context:
        background_tasks.add_task(
            analysis_store.save, context[0], body.resume_id, body.job_id,
            context[1], context[2], resume_analysis.ANALYSIS_VERSION, result
        )

    return result


# =====================================================
# SCORE RESUMES (same logic as job matching)
# =====================================================

@app.post("/resume/score", dependencies=[Depends(enforce_general_limits)])
async def score_resumes(request: ScoreRequest):

    # compute_match_score is the exact function used when matching jobs,
    # so "before" and "after" are scored identically.
    return {
        "scores": [
            compute_match_score(text, request.job_description)
            for text in request.resume_texts
        ]
    }


# =====================================================
# DOWNLOAD OPTIMIZED RESUME
# =====================================================

@app.post("/resume/download", dependencies=[Depends(enforce_general_limits)])
async def download_resume(
    request: Request,
    file: UploadFile = File(...),
    job_description: str = Form(...),
    optimized_resume: Optional[str] = Form(None)
):

    if not job_description.strip():
        raise HTTPException(400, "Missing job description")

    content = await file.read()

    if not content:
        raise HTTPException(400, "Empty file")

    try:
        text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        resume_text = text.strip()
    except Exception as e:
        logging.error(f"PDF extraction failed: {type(e).__name__}")
        raise HTTPException(400, "Could not read the PDF file")

    if not resume_text:
        raise HTTPException(400, "Could not extract text from PDF")

    # Prefer the resume the app already showed and scored; older app
    # versions don't send it, so fall back to optimizing again.
    provided = None

    if optimized_resume:
        try:
            parsed = json.loads(optimized_resume)
            if isinstance(parsed, dict):
                provided = parsed
        except ValueError:
            logging.warning("Ignoring invalid optimized_resume payload")

    # Only an older app (no optimized_resume) makes this endpoint call OpenAI
    if provided is None:
        enforce_ai_limits(request)

    try:

        if provided is not None:
            optimized = provided
        else:
            optimized = await run_in_threadpool(
                optimize_resume_ai,
                resume_text,
                job_description
            )

        # The same one-page renderer as /resume/render
        pdf_bytes, report = await run_in_threadpool(render_resume, build_resume_data(optimized))
        return _pdf_response(pdf_bytes, report)

    except RenderError as e:
        raise HTTPException(422, e.message)

    except Exception as e:

        logging.error(f"Resume generation failed: {type(e).__name__}")

        raise HTTPException(
            status_code=500,
            detail="Failed to generate optimized resume"
        )


# =====================================================
# RENDER A RESUME (one-page PDF from structured data)
# =====================================================
# No AI and no upload: the app sends the structured resume (from the
# optimization result) and optionally which jobs, projects and sections to
# include. The response is the PDF; X-Resume-Layout says what was left out
# to fit one page.

MAX_RENDER_JSON = 60000


class RenderRequest(BaseModel):
    resume: dict
    keywords: List[str] = Field(default_factory=list, max_length=40)
    include: Optional[dict] = None


def _pdf_response(pdf_bytes: bytes, report: dict) -> Response:
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="resume.pdf"',
            # ASCII-only JSON, so names with accents survive the header
            "X-Resume-Layout": json.dumps(report, ensure_ascii=True, separators=(",", ":")),
        },
    )


@app.post("/resume/render", dependencies=[Depends(enforce_general_limits)])
async def render_resume_endpoint(body: RenderRequest):

    if len(json.dumps(body.resume)) > MAX_RENDER_JSON:
        raise HTTPException(413, "This resume is too large to render.")

    include = body.include
    if include is not None:
        try:
            include = {
                "experience": [int(i) for i in include.get("experience", [])][:20],
                "projects": [int(i) for i in include.get("projects", [])][:20],
                "certifications": bool(include.get("certifications", True)),
                "achievements": bool(include.get("achievements", True)),
                "coursework": bool(include.get("coursework", True)),
            }
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(422, "Invalid choice of sections.")

    try:
        pdf_bytes, report = await run_in_threadpool(
            render_resume, build_resume_data(body.resume), body.keywords, include
        )
    except RenderError as e:
        return JSONResponse(status_code=422, content={"detail": e.message, "overflow_lines": e.overflow_lines})
    except Exception as e:
        logging.error(f"Resume render failed: {type(e).__name__}")
        raise HTTPException(500, "Couldn't create the PDF. Please try again.")

    return _pdf_response(pdf_bytes, report)