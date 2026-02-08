import os
import json
import re
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
You are an expert resume optimizer and career coach.

Analyze the resume and job description.

Return ONLY valid JSON in this exact format:

{
  "missing_skills": [string],
  "improved_bullets": [string],
  "ats_keywords": [string]
}

Rules:
- Output raw JSON only
- No explanations
- No markdown
- No extra text
- Keep bullets concise and professional
- Focus on realistic improvements
"""

# Safety limits
MAX_TEXT_LENGTH = 4000
MAX_TOKENS = 500


def extract_json(text: str) -> dict:
    """
    Extract JSON safely from model output.
    Handles cases where model adds extra text.
    """

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        raise ValueError("No JSON found in AI response")

    return json.loads(match.group())


def optimize_resume_ai(resume_text: str, job_description: str) -> str:
    """
    Calls OpenAI to optimize resume.
    Always returns valid JSON string.
    Raises Exception on failure.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-5-mini",
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
            temperature=0.2,
            max_tokens=MAX_TOKENS,
            timeout=25
        )

        raw_content = response.choices[0].message.content.strip()

        try:
            parsed = extract_json(raw_content)

        except Exception as e:
            print("⚠️ JSON parse failed:", raw_content)

            parsed = {
                "missing_skills": [],
                "improved_bullets": [
                    "AI formatting issue. Please retry."
                ],
                "ats_keywords": []
            }

        return json.dumps({
            "missing_skills": parsed.get("missing_skills", []),
            "improved_bullets": parsed.get("improved_bullets", []),
            "ats_keywords": parsed.get("ats_keywords", [])
        })

    except Exception as e:
        print("❌ OpenAI error:", str(e))

        # Hard fallback — never crash API
        return json.dumps({
            "missing_skills": [],
            "improved_bullets": [
                "Optimization temporarily unavailable. Please try again."
            ],
            "ats_keywords": []
        })
