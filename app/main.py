# =====================================================
# LOAD ENVIRONMENT (MUST BE FIRST)
# =====================================================

from dotenv import load_dotenv
import os

load_dotenv()


# =====================================================
# IMPORTS
# =====================================================

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import pdfplumber
import io
import re
import logging

from app.services.ai_optimizer import optimize_resume_ai
from app.services.jobs import fetch_jobs
from app.services.domain_classifier import generate_job_query
from app.services.resume_builder import build_resume_pdf


# =====================================================
# APP CONFIG
# =====================================================

app = FastAPI(
    title="GradHire API",
    description="AI-powered resume optimization and job matching backend",
    version="1.0.0"
)

MAX_FILE_SIZE = 5 * 1024 * 1024
ALLOWED_COUNTRIES = {"in", "us"}

logging.basicConfig(level=logging.WARNING)


# =====================================================
# REQUEST MODELS
# =====================================================

class OptimizeRequest(BaseModel):
    resume_text: str = Field(..., min_length=50)
    job_description: str = Field(..., min_length=20)


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
# UPLOAD RESUME
# =====================================================

@app.post("/resume/upload")
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

@app.post("/jobs/from-resume")
async def jobs_from_resume(
    file: UploadFile = File(...),
    country: str = Query("in")
):

    if country not in ALLOWED_COUNTRIES:
        raise HTTPException(400, "Unsupported country")

    text = await extract_resume_text(file)

    try:

        query = generate_job_query(text)

        jobs = fetch_jobs(
            query=query,
            country=country,
            resume_text=text
        )

        if not jobs:

            logging.warning("Fallback job query used")

            jobs = fetch_jobs(
                query="junior software engineer",
                country=country,
                resume_text=text
            )

        return jobs or []

    except Exception as e:

        logging.error(f"Job fetch failed: {e}")

        return []


# =====================================================
# RESUME OPTIMIZATION
# =====================================================

@app.post("/resume/optimize")
async def optimize_resume(request: OptimizeRequest):

    try:

        result = optimize_resume_ai(
            request.resume_text,
            request.job_description
        )

        improved_bullets = []

        for job in result.get("experience", []):

            improved_bullets.extend(job.get("bullets", []))

        return {

            "missing_skills": result.get("missing_skills", [])[:10],

            "improved_bullets": improved_bullets[:5],

            "ats_keywords": result.get("skills", [])[:10]
        }

    except Exception as e:

        logging.error(f"Optimize failed: {str(e)}")

        raise HTTPException(
            status_code=500,
            detail="Resume optimization failed"
        )


# =====================================================
# DOWNLOAD OPTIMIZED RESUME
# =====================================================

@app.post("/resume/download")
async def download_resume(
    file: UploadFile = File(...),
    job_description: str = Form(...)
):

    if not job_description.strip():
        raise HTTPException(400, "Missing job description")

    resume_text = await extract_resume_text(file)

    try:

        optimized = optimize_resume_ai(
            resume_text,
            job_description
        )

        resume_data = {

            "name": optimized.get("name", "Candidate Name"),

            "contact": optimized.get(
                "contact",
                "Email • Phone • LinkedIn"
            ),

            "summary": optimized.get("summary", ""),

            "skills": optimized.get("skills", []),

            "experience": optimized.get("experience", []),

            "projects": optimized.get("projects", []),

            "education": optimized.get("education", [])
        }

        pdf_path = build_resume_pdf(resume_data)

        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename="optimized_resume.pdf"
        )

    except Exception as e:

        logging.error(f"Resume generation failed: {e}")

        raise HTTPException(
            status_code=500,
            detail="Failed to generate optimized resume"
        )
