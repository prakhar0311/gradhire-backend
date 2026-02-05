from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from dotenv import load_dotenv
import pdfplumber
import io
import os
import re

from app.services.jobs import fetch_jobs

# -------- LOAD ENV --------
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

app = FastAPI()

# -------- CONFIG --------
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# -------- RESUME DETECTION --------

RESUME_KEYWORDS = [
    "experience", "education", "skills", "projects",
    "work experience", "professional experience",
    "software", "engineer", "developer",
    "university", "bachelor", "master",
    "python", "java", "react", "swift"
]

NON_RESUME_KEYWORDS = [
    "boarding pass", "flight", "gate", "seat",
    "invoice", "receipt", "ticket", "payment",
    "tax", "bank", "statement"
]

def is_valid_resume(text: str):

    if not text or len(text) < 200:
        return False

    text_lower = text.lower()

    resume_score = sum(
        1 for k in RESUME_KEYWORDS if k in text_lower
    )

    non_resume_score = sum(
        1 for k in NON_RESUME_KEYWORDS if k in text_lower
    )

    # Resume must strongly outweigh non-resume signals
    return resume_score >= 3 and non_resume_score == 0

# -------- KEYWORD EXTRACTION --------

def extract_keywords(text: str):
    keywords = [
        "ios", "swift", "frontend", "react",
        "backend", "python", "java",
        "full stack", "software engineer"
    ]

    text_lower = text.lower()

    for k in keywords:
        if k in text_lower:
            return k

    return "software engineer"

# -------- ROOT --------

@app.get("/")
def root():
    return {"message": "GradHire backend running 🚀"}

# -------- UPLOAD + VALIDATION --------

@app.post("/resume/upload")
async def upload_resume(file: UploadFile = File(...)):

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF resumes are allowed"
        )

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="Resume too large (max 5MB)"
        )

    text = ""

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                if page.extract_text():
                    text += page.extract_text() + "\n"
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid PDF file"
        )

    if not is_valid_resume(text):
        raise HTTPException(
            status_code=400,
            detail="This document does not appear to be a resume"
        )

    return {
        "filename": file.filename,
        "size": len(content),
        "status": "resume validated",
        "text": text
    }

# -------- SMART JOB SEARCH --------

@app.post("/jobs/from-resume")
async def jobs_from_resume(
    file: UploadFile = File(...),
    country: str = Query("in")
):

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF resumes are allowed"
        )

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="Resume too large (max 5MB)"
        )

    text = ""

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                if page.extract_text():
                    text += page.extract_text()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid PDF file"
        )

    if not is_valid_resume(text):
        raise HTTPException(
            status_code=400,
            detail="This document does not appear to be a resume"
        )

    query = extract_keywords(text)

    try:
        jobs = fetch_jobs(
            query=query,
            country=country,
            resume_text=text
        )

        if not jobs and country == "in":
            jobs = fetch_jobs(
                query="software engineer",
                country=country,
                resume_text=text
            )

        return jobs

    except Exception:
        return []
