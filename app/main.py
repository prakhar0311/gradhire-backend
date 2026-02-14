from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import pdfplumber
import io
import os
import re
import logging

from app.services.ai_optimizer import optimize_resume_ai
from app.services.jobs import fetch_jobs
from app.services.domain_classifier import generate_job_query
from app.services.resume_builder import build_resume_pdf


# =====================================================
# LOAD ENVIRONMENT
# =====================================================

load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

# =====================================================
# APP CONFIG
# =====================================================

app = FastAPI(
    title="GradHire API",
    description="AI-powered resume optimization and job matching backend",
    version="1.0.0"
)

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_COUNTRIES = {"in", "us"}

logging.basicConfig(level=logging.INFO)


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
# RESUME DETECTION (TECH-FOCUSED)
# =====================================================

RESUME_SIGNALS = [
    "experience",
    "education",
    "skills",
    "projects",
    "summary",
    "work",
    "university",
    "bachelor",
    "master",
    "engineer",
    "developer",
]

TECH_SIGNALS = [
    "python", "java", "swift", "react", "javascript",
    "sql", "aws", "docker", "api", "git", "node",
    "frontend", "backend", "ios", "android"
]

NON_RESUME_SIGNALS = [
    "boarding pass",
    "flight",
    "invoice",
    "receipt",
    "ticket",
    "payment",
    "bank statement",
]


def is_valid_resume(text: str) -> bool:

    text = normalize_text(text)

    if len(text) < 150:
        return False

    resume_score = sum(signal in text for signal in RESUME_SIGNALS)
    tech_score = sum(signal in text for signal in TECH_SIGNALS)
    non_resume_score = sum(signal in text for signal in NON_RESUME_SIGNALS)

    return (
        resume_score >= 2
        and tech_score >= 1
        and non_resume_score == 0
    )


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
        raise HTTPException(400, "Resume too large (max 5MB)")

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
            "This document does not appear to be a software engineering resume"
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

        # fallback protection
        if not jobs:

            logging.warning("Primary query returned no jobs. Using fallback.")

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
# RESUME OPTIMIZATION (JSON)
# =====================================================

@app.post("/resume/optimize")
async def optimize_resume(request: OptimizeRequest):

    try:

        result = optimize_resume_ai(
            request.resume_text,
            request.job_description
        )

        # Extract bullets safely from experience
        improved_bullets = []

        for job in result.get("experience", []):
            improved_bullets.extend(job.get("bullets", []))

        # Limit to 5 bullets max
        improved_bullets = improved_bullets[:5]

        return {
            "missing_skills": result.get("missing_skills", []),
            "improved_bullets": improved_bullets,
            "ats_keywords": result.get("skills", [])
        }

    except Exception as e:

        logging.error(f"Optimize failed: {e}")

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
            500,
            "Failed to generate optimized resume"
        )
