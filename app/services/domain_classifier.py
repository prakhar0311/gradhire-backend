import re
from typing import Dict

# =====================================================
# GradHire Production Domain Classifier v2
# Accurate, Stable, Launch-Ready
# =====================================================

# Strong skill signals
SPECIALIZATION_KEYWORDS: Dict[str, list] = {

    "frontend": [
        r"\breact\b",
        r"\bnext\.?js\b",
        r"\bvue\b",
        r"\bangular\b",
        r"\bjavascript\b",
        r"\btypescript\b",
        r"\bhtml\b",
        r"\bcss\b",
        r"\btailwind\b",
        r"\bbootstrap\b",
        r"\bfrontend\b",
        r"\bweb developer\b",
    ],

    "backend": [
        r"\bbackend\b",
        r"\bnode\.?js\b",
        r"\bexpress\b",
        r"\bpython\b",
        r"\bjava\b",
        r"\bspring\b",
        r"\bdjango\b",
        r"\bflask\b",
        r"\bapi\b",
        r"\bmicroservices\b",
        r"\bserver\b",
        r"\bsql\b",
        r"\bpostgres\b",
        r"\bmongodb\b",
    ],

    "ios": [
        r"\bios\b",
        r"\bswift\b",
        r"\bswiftui\b",
        r"\bxcode\b",
        r"\buikit\b",
        r"\bobjective[- ]?c\b",
    ],

    "android": [
        r"\bandroid\b",
        r"\bkotlin\b",
        r"\bjetpack\b",
        r"\bandroid studio\b",
    ],

    "data": [
        r"\bmachine learning\b",
        r"\bml\b",
        r"\bai\b",
        r"\bpandas\b",
        r"\bnumpy\b",
        r"\bscikit\b",
        r"\btensorflow\b",
        r"\bdata analyst\b",
        r"\bdata science\b",
    ],

    "devops": [
        r"\bdocker\b",
        r"\bkubernetes\b",
        r"\baws\b",
        r"\bazure\b",
        r"\bgcp\b",
        r"\bci/cd\b",
        r"\bterraform\b",
    ],

    "fullstack": [
        r"\bfull[- ]?stack\b",
        r"\bmern\b",
        r"\bmean\b",
    ],
}

# =====================================================
# Query mapping optimized for GradHire platform
# =====================================================

QUERY_MAP = {

    "frontend": "junior frontend developer",

    "backend": "junior backend developer",

    "ios": "junior ios developer",

    "android": "junior android developer",

    "data": "junior data analyst",

    "devops": "junior devops engineer",

    "fullstack": "junior full stack developer",

    "general": "junior software engineer",
}

# =====================================================
# Clean and normalize text
# =====================================================

def normalize(text: str) -> str:

    if not text:
        return ""

    text = text.lower()

    text = re.sub(r"\s+", " ", text)

    return text


# =====================================================
# Score specialization strength
# =====================================================

def score_specializations(text: str) -> Dict[str, int]:

    scores = {}

    for spec, patterns in SPECIALIZATION_KEYWORDS.items():

        score = 0

        for pattern in patterns:
            matches = re.findall(pattern, text)
            score += len(matches)

        scores[spec] = score

    return scores


# =====================================================
# Detect best specialization
# =====================================================

def detect_specialization(resume_text: str) -> str:

    if not resume_text:
        return "general"

    text = normalize(resume_text)

    scores = score_specializations(text)

    best_spec = max(scores, key=scores.get)

    best_score = scores.get(best_spec, 0)

    # Require minimum confidence threshold
    if best_score < 2:
        return "general"

    return best_spec


# =====================================================
# Public API used by main.py
# =====================================================

def generate_job_query(resume_text: str) -> str:

    try:

        specialization = detect_specialization(resume_text)

        return QUERY_MAP.get(
            specialization,
            QUERY_MAP["general"]
        )

    except Exception as e:

        print("⚠️ Domain classifier error:", str(e))

        return QUERY_MAP["general"]
