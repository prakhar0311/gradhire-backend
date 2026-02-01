from fastapi import FastAPI, UploadFile, File, Query
from dotenv import load_dotenv
import pdfplumber
import io
import os

from app.services.jobs import fetch_jobs

# -------- LOAD ENV --------
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

app = FastAPI()

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

# -------- UPLOAD + PARSE RESUME --------
@app.post("/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    content = await file.read()

    text = ""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            if page.extract_text():
                text += page.extract_text() + "\n"

    return {
        "filename": file.filename,
        "size": len(content),
        "status": "uploaded successfully",
        "text": text
    }

# -------- RESUME OPTIMIZATION (MOCK) --------
@app.post("/resume/optimize")
async def optimize_resume(payload: dict):

    resume = payload.get("resume_text", "").lower()
    job = payload.get("job_description", "").lower()

    if not resume or not job:
        return {"error": "Missing resume or job description"}

    common_skills = [
        "python", "java", "react", "swift", "aws", "docker",
        "kubernetes", "sql", "javascript", "typescript",
        "ci/cd", "git", "rest", "api", "microservices"
    ]

    missing_skills = [
        skill.upper()
        for skill in common_skills
        if skill in job and skill not in resume
    ]

    if not missing_skills:
        missing_skills = ["Leadership", "System Design"]

    improved_bullets = []

    if "react" in resume:
        improved_bullets.append(
            "Developed high-performance React applications with reusable components."
        )

    if "python" in resume:
        improved_bullets.append(
            "Built scalable backend services using Python and REST APIs."
        )

    if not improved_bullets:
        improved_bullets = [
            "Built scalable applications following best coding practices.",
            "Collaborated with teams to deliver high-quality features."
        ]

    ats_keywords = list(set(
        missing_skills[:3] + ["Agile", "APIs", "Cloud", "Engineering"]
    ))

    return {
        "missing_skills": missing_skills[:5],
        "improved_bullets": improved_bullets[:5],
        "ats_keywords": ats_keywords[:6]
    }

# -------- SMART JOB SEARCH --------
# -------- SMART JOB SEARCH --------
@app.post("/jobs/from-resume")
async def jobs_from_resume(
    file: UploadFile = File(...),
    country: str = Query("in")
):
    content = await file.read()

    text = ""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            if page.extract_text():
                text += page.extract_text()

    query = extract_keywords(text)

    print("🔍 Resume keyword:", query)

    try:
        jobs = fetch_jobs(
            query=query,
            country=country,
            resume_text=text
        )

        print("📊 First fetch jobs:", len(jobs))

        # 🔥 FORCE fallback if India returns nothing
        if not jobs and country == "in":
            jobs = fetch_jobs(
                query="software engineer",
                country=country,
                resume_text=text
            )
            print("📊 Fallback jobs:", len(jobs))

        return jobs

    except Exception as e:
        print("❌ Job search failed:", str(e))
        return []

