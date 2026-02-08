import re

# -------- DOMAIN KEYWORDS --------

DOMAIN_KEYWORDS = {
    "software": [
        r"\bsoftware\b",
        r"\breact\b",
        r"\bpython\b",
        r"\bjava\b",
        r"\bswift\b",
        r"\bfrontend\b",
        r"\bbackend\b",
        r"\bfull[- ]?stack\b",
        r"\bweb developer\b",
    ],

    "aerospace": [
        r"\baerospace\b",
        r"\baircraft\b",
        r"\baerodynamics\b",
        r"\bcfd\b",
        r"\bscramjet\b",
        r"\bflight\b",
        r"\bansys\b",
    ],

    "mechanical": [
        r"\bmechanical\b",
        r"\bsolid mechanics\b",
        r"\bthermodynamics\b",
        r"\bmachine design\b",
        r"\bfea\b",
        r"\bhypermesh\b",
    ],

    "electrical": [
        r"\belectrical\b",
        r"\bcircuit\b",
        r"\bpower systems\b",
        r"\bembedded\b",
        r"\belectronics\b",
    ],

    "civil": [
        r"\bcivil\b",
        r"\bconstruction\b",
        r"\bstructural\b",
        r"\binfrastructure\b",
        r"\bsurveying\b",
    ],
}

# -------- QUERY MAP --------

DOMAIN_QUERY_MAP = {
    "software": "junior software engineer",
    "aerospace": "entry level aerospace engineer",
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

    for domain, patterns in DOMAIN_KEYWORDS.items():

        score = 0

        for pattern in patterns:
            matches = re.findall(pattern, text)
            score += len(matches)

        scores[domain] = score

    # Pick highest score
    best_domain = max(scores, key=scores.get)

    # If no strong signal → general engineering
    if scores[best_domain] == 0:
        return "general"

    return best_domain


# -------- QUERY GENERATION --------

def generate_job_query(resume_text: str) -> str:

    domain = detect_engineering_domain(resume_text)

    return DOMAIN_QUERY_MAP.get(domain, "graduate engineer")
