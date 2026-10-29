# =====================================================
# LOAD ENVIRONMENT (MUST BE FIRST)
# =====================================================

from dotenv import load_dotenv
import os

load_dotenv()


# =====================================================
# IMPORTS
# =====================================================

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Form, Request, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
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
    close_client,
    AdzunaUnavailable
)
from app.services.domain_classifier import generate_job_query
from app.services.resume_builder import build_resume_pdf
from app.services.supabase_client import check_connection
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


def remove_file(path: str):
    try:
        os.remove(path)
    except OSError:
        pass

logging.basicConfig(level=logging.WARNING)


# =====================================================
# REQUEST MODELS
# =====================================================

class OptimizeRequest(BaseModel):
    resume_text: str = Field(..., min_length=50)
    job_description: str = Field(..., min_length=20)


class ScoreRequest(BaseModel):
    job_description: str = Field(..., min_length=1)
    resume_texts: List[str] = Field(..., min_length=1, max_length=4)


# =====================================================
# OPTIMIZED RESUME SHAPE (shared by optimize + download)
# =====================================================

def build_resume_data(optimized: dict) -> dict:

    return {
        "name": optimized.get("name", "Candidate Name"),
        "contact": optimized.get("contact", "Email • Phone • LinkedIn"),
        "summary": optimized.get("summary", ""),
        "skills": optimized.get("skills", []),
        "experience": optimized.get("experience", []),
        "projects": optimized.get("projects", []),
        "education": optimized.get("education", [])
    }


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
    file: UploadFile = File(...),
    country: str = Query("in")
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

        return jobs

    except AdzunaUnavailable:

        raise HTTPException(503, "Job search is temporarily unavailable. Please try again.")

    except Exception as e:

        logging.error(f"Job fetch failed: {e}")

        raise HTTPException(500, "Could not load jobs. Please try again.")


@app.get("/jobs/new-grad", dependencies=[Depends(enforce_general_limits)])
async def new_grad_jobs(
    country: str = Query("in"),
    page: int = Query(1, ge=1, le=50),
    max_days: Optional[int] = Query(None)
):

    if country not in ALLOWED_COUNTRIES:
        raise HTTPException(400, "Unsupported country")

    if max_days is not None and max_days not in ALLOWED_MAX_DAYS:
        raise HTTPException(400, "Unsupported max_days")

    try:

        return await fetch_new_grad_jobs(country=country, page=page, max_days=max_days)

    except AdzunaUnavailable:

        raise HTTPException(503, "Job search is temporarily unavailable. Please try again.")

    except Exception as e:

        logging.error(f"New grad fetch failed: {e}")

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
        logging.error(f"PDF extraction failed: {e}")
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

    pdf_path = None

    try:

        if provided is not None:
            optimized = provided
        else:
            optimized = await run_in_threadpool(
                optimize_resume_ai,
                resume_text,
                job_description
            )

        resume_data = build_resume_data(optimized)

        pdf_path = await run_in_threadpool(build_resume_pdf, resume_data)

        # Delete the generated PDF as soon as it has been sent
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename="optimized_resume.pdf",
            background=BackgroundTask(remove_file, pdf_path)
        )

    except Exception as e:

        if pdf_path:
            remove_file(pdf_path)

        logging.error(f"Resume generation failed: {e}")

        raise HTTPException(
            status_code=500,
            detail="Failed to generate optimized resume"
        )