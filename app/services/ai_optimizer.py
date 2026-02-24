import os
import json
import re
from openai import OpenAI


# =====================================================
# OPENAI CLIENT (SAFE INITIALIZATION)
# =====================================================

def get_openai_client():

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in environment variables")

    return OpenAI(api_key=api_key)


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
"""


# Increased reliability
MAX_TEXT_LENGTH = 4000
MAX_TOKENS = 1200


# =====================================================
# SAFE LIST
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
# FALLBACK CONTACT
# =====================================================

def fallback_extract_contact(resume_text):

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
# FALLBACK NAME
# =====================================================

def fallback_extract_name(resume_text):

    for line in resume_text.split("\n")[:5]:

        line = line.strip()

        if 3 < len(line) < 50 and not any(char.isdigit() for char in line):

            return line

    return "Candidate Name"


# =====================================================
# SAFE JSON PARSE
# =====================================================

def safe_json_parse(content):

    try:
        return json.loads(content)

    except:

        try:
            start = content.find("{")
            end = content.rfind("}") + 1

            return json.loads(content[start:end])

        except:
            return {}


# =====================================================
# BUILD SAFE RESPONSE
# =====================================================

def build_safe_response(parsed, resume_text):

    return {

        "name": parsed.get("name") or fallback_extract_name(resume_text),

        "contact": parsed.get("contact") or fallback_extract_contact(resume_text),

        "summary": parsed.get("summary", ""),

        "missing_skills": safe_list(parsed.get("missing_skills"), 8),

        "skills": safe_list(parsed.get("skills"), 12),

        "experience": safe_list(parsed.get("experience"), 3),

        "projects": safe_list(parsed.get("projects"), 3),

        "education": safe_list(parsed.get("education"), 2),
    }


# =====================================================
# FALLBACK RESPONSE
# =====================================================

def fallback_response(resume_text):

    return {

        "name": fallback_extract_name(resume_text),

        "contact": fallback_extract_contact(resume_text),

        "summary": "",

        "missing_skills": [],

        "skills": [],

        "experience": [],

        "projects": [],

        "education": [],
    }


# =====================================================
# MAIN OPTIMIZER (PRODUCTION SAFE)
# =====================================================

def optimize_resume_ai(resume_text, job_description):

    client = get_openai_client()

    parsed = {}

    # Retry logic (2 attempts)
    for attempt in range(2):

        try:

            response = client.chat.completions.create(

                model="gpt-4o-mini",

                temperature=0,

                response_format={"type": "json_object"},

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

                max_tokens=MAX_TOKENS
            )

            content = response.choices[0].message.content.strip()

            parsed = safe_json_parse(content)

            print("AI parsed result:", parsed)

            if parsed:
                return build_safe_response(parsed, resume_text)

        except Exception as e:

            print(f"❌ OpenAI attempt {attempt+1} failed:", e)

    # Final fallback
    return fallback_response(resume_text)
