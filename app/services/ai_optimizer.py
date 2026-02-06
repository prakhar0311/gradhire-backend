import os
import json
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

# Safety limits (prevents huge token usage)
MAX_TEXT_LENGTH = 4000


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
            ]
        )

        content = response.choices[0].message.content.strip()

        # Try parsing JSON safely
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            print("⚠️ Invalid JSON from OpenAI:", content)

            # Safe fallback
            parsed = {
                "missing_skills": [],
                "improved_bullets": [
                    "AI response formatting issue. Please retry."
                ],
                "ats_keywords": []
            }

        # Ensure required keys exist
        return json.dumps({
            "missing_skills": parsed.get("missing_skills", []),
            "improved_bullets": parsed.get("improved_bullets", []),
            "ats_keywords": parsed.get("ats_keywords", [])
        })

    except Exception as e:
        print("❌ OpenAI error:", str(e))
        raise Exception("AI optimization failed")
