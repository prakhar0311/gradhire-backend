from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from dotenv import load_dotenv
import pdfplumber
import io
import os
import json
import re

from app.services.ai_optimizer import optimize_resume_ai
from app.services.jobs import fetch_jobs
from app.services.domain_classifier import generate_job_query

# -------- LOAD ENV --------
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

app = FastAPI()

# -------- CONFIG --------
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_COUNTRIES = {"in", "us"}

# -------- RESUME DETECTION --------

RESUME_STRUCTURE_KEYWORDS = [
    "experience",
    "education",
    "skills",
    "projects",
    "summary",
    "professional",
    "university",
    "bachelor",
    "master",
    "intern",
]

NON_RESUME_KEYWORDS = [
    "boarding pass",
    "flight",
    "gate",
    "seat",
    "invoice",
    "receipt",
    "ticket",
    "payment",
    "tax",
    "bank statement",
]


def normalize_text(text: str) -> str:
    """Clean messy PDF extraction"""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_valid_resume(text: str) -> bool:

    text = normalize_text(text).lower()

    # Too short → not a resume
    if len(text) < 200:
        return False

    # Reject obvious non-resumes
    if any(k in text for k in NON_RESUME_KEYWORDS):
        return False

    # Resume structure signals
    structure_hits = sum(k in text for k in RESUME_STRUCTURE_KEYWORDS)

    # Word diversity check (real resumes have lots of unique words)
    words = set(re.findall(r"\b\w+\b", text))
    diversity_score = len(words)

    return structure_hits >= 2 and diversity_score >= 50


# -------- PDF EXTRACTION HELPER --------

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

        text = normalize_text(text)

    except Exception:
        raise HTTPException(400, "Invalid PDF file")

    if not is_valid_resume(text):
        raise HTTPException(
            400,
            "This document does not appear to be a resume"
        )

    return text


# -------- ROOT --------

@app.get("/")
def root():
    return {"message": "GradHire backend running 🚀"}


# -------- UPLOAD --------

@app.post("/resume/upload")
async def upload_resume(file: UploadFile = File(...)):

    text = await extract_resume_text(file)

    return {
        "filename": file.filename,
        "status": "resume validated",
        "text": text
    }


# -------- JOB SEARCH --------

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


# -------- SAFE AI JSON PARSER --------

def parse_ai_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Invalid AI response")

    return json.loads(match.group())


# -------- RESUME OPTIMIZATION --------

@app.post("/resume/optimize")
async def optimize_resume(payload: dict):

    resume = payload.get("resume_text", "").strip()
    job = payload.get("job_description", "").strip()

    if not resume or not job:
        raise HTTPException(400, "Missing resume or job description")

    try:
        ai_response = optimize_resume_ai(resume, job)
        parsed = parse_ai_json(ai_response)

        return {
            "missing_skills": parsed.get("missing_skills", []),
            "improved_bullets": parsed.get("improved_bullets", []),
            "ats_keywords": parsed.get("ats_keywords", [])
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
