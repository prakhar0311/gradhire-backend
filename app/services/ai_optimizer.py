import os
import json
import re
from openai import OpenAI


# =====================================================
# OPENAI CLIENT
# =====================================================

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# =====================================================
# SYSTEM PROMPT
# =====================================================

SYSTEM_PROMPT = """
You are a professional FAANG resume editor specializing in junior software engineers and new graduates.

Improve and restructure the resume for a software engineering role.

IMPORTANT RULES:
- Preserve truth. Do NOT invent fake experience.
- Optimize for ATS and FAANG resume standards.
- Extract contact info if available.
- Identify important missing skills from the job description.

Return ONLY valid JSON in this EXACT format:

{
  "name": string,
  "contact": string,
  "summary": string,

  "missing_skills": [string],

  "skills": [string],

  "experience": [
    {
      "title": string,
      "company": string,
      "duration": string,
      "location": string,
      "bullets": [string]
    }
  ],

  "projects": [
    {
      "title": string,
      "bullets": [string]
    }
  ],

  "education": [
    {
      "degree": string,
      "school": string,
      "duration": string,
      "location": string
    }
  ]
}

Rules:
- missing_skills: 4–8 important skills present in job description but missing in resume
- Skills: 6–12 strongest skills from resume
- Experience: max 3 roles
- Projects: 2–3 projects
- Each role/project: 3–4 bullets
- Output raw JSON only
"""


MAX_TEXT_LENGTH = 4000
MAX_TOKENS = 800


# =====================================================
# SAFE LIST HANDLER
# =====================================================

def safe_list(value, limit=None):

    if not isinstance(value, list):
        return []

    clean = []

    for item in value:
        if isinstance(item, (dict, str)):
            clean.append(item)

    return clean[:limit] if limit else clean


# =====================================================
# FALLBACK CONTACT EXTRACTION
# =====================================================

def fallback_extract_contact(resume_text: str):

    email = re.search(r'[\w\.-]+@[\w\.-]+', resume_text)
    phone = re.search(r'\+?\d[\d\s\-]{8,}', resume_text)
    linkedin = re.search(r'linkedin\.com\/\S+', resume_text)

    parts = []

    if email:
        parts.append(email.group())

    if phone:
        parts.append(phone.group())

    if linkedin:
        parts.append(linkedin.group())

    return " | ".join(parts)


# =====================================================
# FALLBACK NAME EXTRACTION
# =====================================================

def fallback_extract_name(resume_text: str):

    lines = resume_text.split("\n")

    for line in lines[:5]:
        line = line.strip()

        if 3 < len(line) < 50 and not any(char.isdigit() for char in line):
            return line

    return "Candidate Name"


# =====================================================
# SKILL CATEGORIZATION (FAANG STYLE)
# =====================================================

LANGUAGES = {
    "python", "java", "javascript", "typescript",
    "c", "c++", "c#", "swift", "kotlin",
    "go", "ruby", "php", "sql"
}

FRAMEWORKS = {
    "react", "react.js", "next.js", "angular",
    "vue", "node", "node.js", "express",
    "flask", "django", "spring", "spring boot",
    "swiftui"
}

TOOLS = {
    "git", "docker", "aws", "linux",
    "postgresql", "mongodb", "firebase",
    "xcode", "kubernetes"
}

CONCEPTS = {
    "rest api", "restful apis",
    "microservices",
    "ci/cd",
    "oop",
    "object oriented programming",
    "cloud computing"
}


def categorize_skills(skills_list):

    categories = {
        "Languages": [],
        "Frameworks": [],
        "Tools": [],
        "Concepts": []
    }

    for skill in skills_list:

        if not isinstance(skill, str):
            continue

        s = skill.lower()

        if s in LANGUAGES:
            categories["Languages"].append(skill)

        elif s in FRAMEWORKS:
            categories["Frameworks"].append(skill)

        elif s in TOOLS:
            categories["Tools"].append(skill)

        else:
            categories["Concepts"].append(skill)

    return categories


# =====================================================
# MAIN OPTIMIZER FUNCTION
# =====================================================

def optimize_resume_ai(resume_text: str, job_description: str) -> dict:

    try:

        response = client.chat.completions.create(

            model="gpt-4o-mini",

            temperature=0,

            response_format={"type": "json_object"},

            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": f"""
Resume:
{resume_text[:MAX_TEXT_LENGTH]}

Job Description:
{job_description[:MAX_TEXT_LENGTH]}
"""
                }
            ],

            max_tokens=MAX_TOKENS,
            timeout=30
        )

        parsed = response.choices[0].message.parsed


        # Safe fallback extraction
        name = parsed.get("name") or fallback_extract_name(resume_text)

        contact = parsed.get("contact") or fallback_extract_contact(resume_text)

        raw_skills = safe_list(parsed.get("skills"), 12)

        categorized_skills = categorize_skills(raw_skills)

        return {

            "name": name,

            "contact": contact,

            "summary": parsed.get("summary", ""),

            # UI only
            "missing_skills": safe_list(parsed.get("missing_skills"), 8),

            # Resume + ATS
            "skills": categorized_skills,

            "experience": safe_list(parsed.get("experience"), 3),

            "projects": safe_list(parsed.get("projects"), 3),

            "education": safe_list(parsed.get("education"), 2),
        }


    except Exception as e:

        print("❌ OpenAI error:", str(e))

        return {

            "name": fallback_extract_name(resume_text),

            "contact": fallback_extract_contact(resume_text),

            "summary": "",

            "missing_skills": [],

            "skills": {
                "Languages": [],
                "Frameworks": [],
                "Tools": [],
                "Concepts": []
            },

            "experience": [],

            "projects": [],

            "education": [],
        }

