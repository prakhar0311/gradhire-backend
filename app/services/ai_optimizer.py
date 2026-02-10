import os
import json
import re
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
You are a professional resume editor and career coach.

Improve the user's resume for the target job.

Preserve truth. Do NOT invent fake experience.

Return ONLY valid JSON in this exact format:

{
  "summary": string,
  "missing_skills": [string],
  "ats_keywords": [string],
  "experience_improvements": [string],
  "project_improvements": [string]
}

Rules:
- Output raw JSON only
- No explanations
- No markdown
- Summary: 2–3 concise sentences
- Missing skills: 5–8 key skill gaps
- ATS keywords: 6–10 optimized keywords
- Return EXACTLY 4 improved experience bullets
- Keep content concise and ATS-friendly
"""

MAX_TEXT_LENGTH = 4000
MAX_TOKENS = 600


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        raise ValueError("No JSON found in AI response")

    return json.loads(match.group())


def optimize_resume_ai(resume_text: str, job_description: str) -> dict:
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"""
Resume:
{resume_text[:MAX_TEXT_LENGTH]}

Job description:
{job_description[:MAX_TEXT_LENGTH]}
"""
                }
            ],
            max_tokens=MAX_TOKENS
        )

        raw_content = response.choices[0].message.content

        if not raw_content:
            raise ValueError("Empty AI response")

        parsed = extract_json(raw_content.strip())

        # 🔒 Enforce UI stability limits
        return {
            "summary": parsed.get("summary", ""),
            "missing_skills": parsed.get("missing_skills", [])[:8],
            "ats_keywords": parsed.get("ats_keywords", [])[:10],
            "experience_improvements": parsed.get(
                "experience_improvements", []
            )[:5],
            "project_improvements": parsed.get(
                "project_improvements", []
            )
        }

    except Exception as e:
        print("❌ OpenAI error:", str(e))

        return {
            "summary": "",
            "missing_skills": [],
            "ats_keywords": [],
            "experience_improvements": [
                "Optimization temporarily unavailable. Please try again."
            ],
            "project_improvements": []
        }
