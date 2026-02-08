import re

# =====================================================
# SOFTWARE SPECIALIZATION CLASSIFIER (GradHire v1)
# =====================================================

SPECIALIZATION_KEYWORDS = {

    "frontend": [
        r"\breact\b",
        r"\bnext\.?js\b",
        r"\bvue\b",
        r"\bangular\b",
        r"\bfrontend\b",
        r"\bhtml\b",
        r"\bcss\b",
        r"\btailwind\b",
        r"\bbootstrap\b",
        r"\bui\b",
    ],

    "backend": [
        r"\bbackend\b",
        r"\bnode\b",
        r"\bexpress\b",
        r"\bpython\b",
        r"\bjava\b",
        r"\bspring\b",
        r"\bdjango\b",
        r"\bapi\b",
        r"\bmicroservices\b",
        r"\bserver\b",
    ],

    "ios": [
        r"\bios\b",
        r"\bswift\b",
        r"\bswiftui\b",
        r"\bxcode\b",
        r"\bobjective[- ]?c\b",
    ],

    "android": [
        r"\bandroid\b",
        r"\bkotlin\b",
        r"\bjava android\b",
        r"\bjetpack\b",
    ],

    "data": [
        r"\bdata\b",
        r"\bmachine learning\b",
        r"\bml\b",
        r"\bai\b",
        r"\bpandas\b",
        r"\bnumpy\b",
        r"\btensorflow\b",
        r"\bscikit\b",
    ],

    "fullstack": [
        r"\bfull[- ]?stack\b",
        r"\bmern\b",
        r"\bmean\b",
    ],
}

# =====================================================
# JOB QUERY MAP
# =====================================================

QUERY_MAP = {
    "frontend": "junior frontend developer",
    "backend": "junior backend developer",
    "ios": "junior ios developer",
    "android": "junior android developer",
    "data": "junior data analyst",
    "fullstack": "junior full stack developer",
    "general": "junior software engineer",
}

# =====================================================
# SPECIALIZATION DETECTION
# =====================================================

def detect_specialization(text: str) -> str:

    if not text:
        return "general"

    text = text.lower()

    scores = {}

    for spec, patterns in SPECIALIZATION_KEYWORDS.items():

        score = 0

        for pattern in patterns:
            matches = re.findall(pattern, text)
            score += len(matches)

        scores[spec] = score

    best_spec = max(scores, key=scores.get)

    # No strong signal → general software
    if scores[best_spec] == 0:
        return "general"

    return best_spec

# =====================================================
# PUBLIC API
# =====================================================

def generate_job_query(resume_text: str) -> str:

    spec = detect_specialization(resume_text)

    return QUERY_MAP.get(spec, "junior software engineer")
