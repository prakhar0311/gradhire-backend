import re
from app.services.ai_optimizer import optimize_resume_ai  # optional fallback

# -------- DOMAIN KEYWORDS --------

DOMAIN_KEYWORDS = {
    "software": [
        "software", "react", "python", "java", "swift",
        "frontend", "backend", "full stack", "web developer"
    ],
    "aerospace": [
        "aerospace", "aircraft", "aerodynamics", "cfd",
        "scramjet", "flight", "ansys fluent"
    ],
    "mechanical": [
        "mechanical", "solid mechanics", "thermodynamics",
        "machine design", "fea", "hypermesh"
    ],
    "electrical": [
        "electrical", "circuit", "power systems",
        "embedded", "electronics"
    ],
    "civil": [
        "civil", "construction", "structural",
        "infrastructure", "surveying"
    ],
}

# -------- QUERY MAP --------

DOMAIN_QUERY_MAP = {
    "software": "junior software engineer",
    "aerospace": "aerospace engineer entry level",
    "mechanical": "graduate mechanical engineer",
    "electrical": "junior electrical engineer",
    "civil": "graduate civil engineer",
    "general": "graduate engineer",
}


# -------- DOMAIN DETECTION --------

def detect_engineering_domain(text: str) -> str:

    if not text:
        return "general"

    text = text.lower()

    scores = {}

    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for k in keywords if k in text)
        scores[domain] = score

    best_domain = max(scores, key=scores.get)

    # If no strong signal → general engineering
    if scores[best_domain] == 0:
        return "general"

    return best_domain


# -------- QUERY GENERATION --------

def generate_job_query(resume_text: str) -> str:

    domain = detect_engineering_domain(resume_text)

    return DOMAIN_QUERY_MAP.get(domain, "graduate engineer")
