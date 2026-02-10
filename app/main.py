from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import pdfplumber
import io
import os
import re
import json

from app.services.ai_optimizer import optimize_resume_ai
from app.services.jobs import fetch_jobs
from app.services.domain_classifier import generate_job_query
from app.services.resume_builder import build_resume_pdf

# -------- LOAD ENV --------
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

app = FastAPI()

# -------- CONFIG --------
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_COUNTRIES = {"in", "us"}

# =====================================================
# TEXT NORMALIZATION
# =====================================================

def normalize_text(text: str) -> str:
    """
    Fix PDF extraction formatting issues.
    """

    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text)

    return text.lower().strip()

# =====================================================
# RESUME DETECTION
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
    non_resume_score = sum(signal in text for signal in NON_RESUME_SIGNALS)

    return resume_score >= 2 and non_resume_score == 0

# =====================================================
# PDF EXTRACTION
# =====================================================

async def extract_resume_text(file: UploadFile) -> str:

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF resumes are allowed")

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "Resume too large (max 5MB)")

    try:
        text = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
    except Exception:
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
    return {"message": "GradHire backend running 🚀"}

# =====================================================
# UPLOAD
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

    query = generate_job_query(text)

    try:
        jobs = fetch_jobs(
            query=query,
            country=country,
            resume_text=text
        )

        if not jobs and country == "in":
            jobs = fetch_jobs(
                query="junior software engineer",
                country=country,
                resume_text=text
            )

        return jobs

    except Exception:
        return []

# =====================================================
# RESUME OPTIMIZATION (JSON PREVIEW)
# =====================================================

@app.post("/resume/optimize")
async def optimize_resume(payload: dict):

    resume = payload.get("resume_text", "").strip()
    job = payload.get("job_description", "").strip()

    if not resume or not job:
        raise HTTPException(400, "Missing resume or job description")

    try:
        result = optimize_resume_ai(resume, job)

        return {
        "missing_skills": result.get("missing_skills", []),
        "improved_bullets": result.get("experience_improvements", []),
        "ats_keywords": result.get("ats_keywords", [])
        }  


    except Exception as e:
        print("❌ AI optimization failed:", str(e))

        return {
            "missing_skills": [],
            "improved_bullets": [
                "Optimization temporarily unavailable. Please try again."
            ],
            "ats_keywords": []
        }


# =====================================================
# DOWNLOAD OPTIMIZED RESUME PDF
# =====================================================

@app.post("/resume/download")
async def download_resume(
    file: UploadFile = File(...),
    job_description: str = ""
):

    resume_text = await extract_resume_text(file)

    if not job_description.strip():
        raise HTTPException(400, "Missing job description")

    try:
        optimized = optimize_resume_ai(resume_text, job_description)

        resume_data = {
            "name": "Optimised Resume",
            "contact": "",
            "summary":optimized.get("summary", ""),
            "skills": optimized.get("skills", []),
            "experience_improvements": optimized.get("experience_improvements", []),
            "project_improvements": optimized.get("project_improvements", [])
        }

        pdf_path = build_resume_pdf(resume_data)

        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename="optimized_resume.pdf"
        )

    except Exception as e:
        print("❌ Resume download failed:", str(e))
        raise HTTPException(500, "Failed to generate resume")
