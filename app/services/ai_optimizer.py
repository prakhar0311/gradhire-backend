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
  "skills": [string],
  "experience_improvements": [string],
  "project_improvements": [string]
}

Rules:
- Output raw JSON only
- No explanations
- No markdown
- Improve clarity and impact
- Add relevant missing skills
- Rewrite bullets to sound professional
- Keep concise and ATS-friendly
"""

# Safety limits
MAX_TEXT_LENGTH = 4000
MAX_TOKENS = 600


def extract_json(text: str) -> dict:
    """
    Extract JSON safely from model output.
    Handles cases where model adds extra text.
    """

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        raise ValueError("No JSON found in AI response")

    return json.loads(match.group())


def optimize_resume_ai(resume_text: str, job_description: str) -> dict:
    """
    Calls OpenAI to optimize resume.
    Always returns structured dict.
    Never crashes the API.
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
            #temperature=0.2,
            max_completion_tokens=MAX_TOKENS,
            timeout=45
        )

        raw_content = response.choices[0].message.content.strip()

        try:
            parsed = extract_json(raw_content)

        except Exception:
            print("⚠️ JSON parse failed:", raw_content)

            parsed = {
                "summary": "",
                "skills": [],
                "experience_improvements": [
                    "AI formatting issue. Please retry."
                ],
                "project_improvements": []
            }

        # Ensure required keys exist
        return {
            "summary": parsed.get("summary", ""),
            "skills": parsed.get("skills", []),
            "experience_improvements": parsed.get(
                "experience_improvements", []
            ),
            "project_improvements": parsed.get(
                "project_improvements", []
            )
        }

    except Exception as e:
        print("❌ OpenAI error:", str(e))

        # Hard fallback — never crash API
        return {
            "summary": "",
            "skills": [],
            "experience_improvements": [
                "Optimization temporarily unavailable. Please try again."
            ],
            "project_improvements": []
        }
