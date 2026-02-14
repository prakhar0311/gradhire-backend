import os
import json
#import re
from openai import OpenAI


client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
- Skills: 6–10 strongest skills from resume
- Experience: max 3 roles
- Projects: 2–3 projects
- Each role/project: 3–4 bullets
- Output raw JSON only
"""


MAX_TEXT_LENGTH = 4000
MAX_TOKENS = 800


#def extract_json(text: str) -> dict:

    #match = re.search(r"\{.*\}", text, re.DOTALL)

    #if not match:
        #raise ValueError("No JSON found in AI response")

    #return json.loads(match.group())


def safe_list(value, limit=None):

    if not isinstance(value, list):
        return []

    clean = [item for item in value if isinstance(item, (dict, str))]

    return clean[:limit] if limit else clean


def fallback_extract_contact(resume_text: str):

    email = re.search(r'[\w\.-]+@[\w\.-]+', resume_text)
    phone = re.search(r'\+?\d[\d\s\-]{8,}', resume_text)

    parts = []

    if email:
        parts.append(email.group())

    if phone:
        parts.append(phone.group())

    return " | ".join(parts)


def fallback_extract_name(resume_text: str):

    lines = resume_text.split("\n")

    if lines:
        first = lines[0].strip()

        if len(first) < 60:
            return first

    return "Candidate Name"


def optimize_resume_ai(resume_text: str, job_description: str) -> dict:

    try:

        response = client.chat.completions.create(

            model="gpt-4o-mini",

            temperature=0,  # IMPORTANT: makes output deterministic

            response_format={"type": "json_object"},  # CRITICAL FIX

            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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

        parsed = json.loads(response.choices[0].message.content)

        return {
            "name": parsed.get("name", "Candidate Name"),
            "contact": parsed.get("contact", ""),
            "summary": parsed.get("summary", ""),
             
            # UI Only (this appears in ui only)
            "missing_skills": safe_list(parsed.get("missing_skills"), 8),


            "skills": parsed.get("skills", [])[:10],
            "experience": parsed.get("experience", [])[:3],
            "projects": parsed.get("projects", [])[:3],
            "education": parsed.get("education", [])[:2],
        }

    except Exception as e:

        print("❌ OpenAI error:", str(e))

        return {
            "name": "Candidate Name",
            "contact": "",
            "summary": "",
            "skills": [],
            "experience": [],
            "projects": [],
            "education": [],
        }
