import re
from typing import Dict, List, Pattern, Set, Tuple


# =====================================================
# SKILL EXTRACTION (no AI calls)
# =====================================================
# A curated list of skills that show up in entry-level postings, each with
# the spellings people use. Matching is whole-word and case-insensitive, so
# "Node.js", "nodejs" and "node js" are one skill, and "java" doesn't match
# "javascript".
#
# Very ambiguous words (Go, R, C, Rest, Spring, Express) are only matched in
# unambiguous forms like "golang" or "spring boot".

SKILLS: Dict[str, List[str]] = {

    # Languages
    "Python": ["python"],
    "Java": ["java"],
    "JavaScript": ["javascript", "js", "ecmascript"],
    "TypeScript": ["typescript"],
    "C++": ["c++", "cpp"],
    "C#": ["c#", "csharp"],
    "C": ["c programming", "c language", "embedded c"],
    "Go": ["golang", "go language"],
    "Rust": ["rust"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift", "swiftui"],
    "Dart": ["dart"],
    "PHP": ["php"],
    "Ruby": ["ruby"],
    "Scala": ["scala"],
    "R": ["r programming", "rstudio", "r language"],
    "MATLAB": ["matlab"],
    "Bash": ["bash", "shell scripting", "shell script"],
    "SQL": ["sql", "t-sql", "pl/sql", "plsql"],

    # Web and mobile
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3", "sass", "scss"],
    "React": ["react", "reactjs", "react.js"],
    "React Native": ["react native"],
    "Angular": ["angular", "angularjs"],
    "Vue": ["vue", "vuejs", "vue.js"],
    "Next.js": ["next.js", "nextjs"],
    "Node.js": ["node.js", "nodejs", "node js"],
    "Express": ["express.js", "expressjs"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Spring Boot": ["spring boot", "springboot", "spring framework"],
    ".NET": [".net", "asp.net", "dotnet", ".net core"],
    "Flutter": ["flutter"],
    "Android": ["android"],
    "iOS": ["ios"],
    "REST APIs": ["rest api", "rest apis", "restful", "restful api", "restful apis"],
    "GraphQL": ["graphql"],
    "Microservices": ["microservices", "microservice"],

    # Data and AI
    "Machine Learning": ["machine learning", "ml"],
    "Deep Learning": ["deep learning"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "opencv"],
    "Generative AI": ["generative ai", "genai", "llm", "llms", "large language models"],
    "TensorFlow": ["tensorflow"],
    "PyTorch": ["pytorch"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "Spark": ["spark", "pyspark", "apache spark"],
    "Hadoop": ["hadoop"],
    "Kafka": ["kafka"],
    "Airflow": ["airflow"],
    "ETL": ["etl"],
    "Data Analysis": ["data analysis", "data analytics"],
    "Data Visualization": ["data visualization", "data visualisation"],
    "Statistics": ["statistics", "statistical analysis"],
    "Excel": ["excel", "ms excel", "microsoft excel", "advanced excel"],
    "Power BI": ["power bi", "powerbi"],
    "Tableau": ["tableau"],
    "Looker": ["looker"],

    # Databases
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Oracle": ["oracle"],
    "SQL Server": ["sql server", "mssql"],
    "Firebase": ["firebase"],
    "Elasticsearch": ["elasticsearch"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery"],

    # Cloud and DevOps
    "AWS": ["aws", "amazon web services"],
    "Azure": ["azure"],
    "GCP": ["gcp", "google cloud"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "Jenkins": ["jenkins"],
    "CI/CD": ["ci/cd", "ci cd", "continuous integration", "continuous delivery"],
    "Linux": ["linux", "unix"],
    "Git": ["git", "github", "gitlab", "bitbucket"],
    "DevOps": ["devops"],

    # Testing and practices
    "Unit Testing": ["unit testing", "unit tests", "junit", "pytest", "jest"],
    "Selenium": ["selenium"],
    "Test Automation": ["test automation", "automation testing"],
    "Manual Testing": ["manual testing"],
    "Agile": ["agile"],
    "Scrum": ["scrum"],
    "Jira": ["jira"],
    "System Design": ["system design"],
    "Data Structures": ["data structures", "dsa"],
    "Algorithms": ["algorithms"],
    "OOP": ["oop", "object oriented", "object-oriented"],

    # Security and networking
    "Cybersecurity": ["cybersecurity", "cyber security", "information security"],
    "Networking": ["networking", "tcp/ip", "computer networks"],

    # Design and product
    "Figma": ["figma"],
    "UI/UX": ["ui/ux", "ux design", "ui design", "user experience"],

    # Business tools
    "SAP": ["sap"],
    "Salesforce": ["salesforce"],
    "Google Analytics": ["google analytics"],
    "SEO": ["seo"],
    "PowerPoint": ["powerpoint", "ms powerpoint"],
}

MAX_SKILLS_PER_JOB = 8


def _compile(aliases: List[str]) -> Pattern:
    # Not part of a longer word: "java" must not match "javascript"
    alternatives = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9+#.]){'(?:' + alternatives + ')'}(?![a-z0-9+#]|\.[a-z])")


_PATTERNS: List[Tuple[str, Pattern]] = [(name, _compile(aliases)) for name, aliases in SKILLS.items()]

def extract_skills(text: str) -> Set[str]:
    """Canonical skill names mentioned in the text."""

    if not text:
        return set()

    lowered = text.lower()
    found = {name for name, pattern in _PATTERNS if pattern.search(lowered)}

    # "React Native" contains "React"; count React only if it's also
    # mentioned on its own
    if "React Native" in found and not re.search(r"(?<!native )\breact(?!\s+native)\b", lowered):
        found.discard("React")

    return found


def skill_gap(resume_skills: Set[str], job_text: str) -> Tuple[List[str], List[str]]:
    """(matched, missing) skills for one job, in a stable order."""

    job_skills = extract_skills(job_text)
    order = list(SKILLS.keys())

    matched = sorted(job_skills & resume_skills, key=order.index)
    missing = sorted(job_skills - resume_skills, key=order.index)

    return matched[:MAX_SKILLS_PER_JOB], missing[:MAX_SKILLS_PER_JOB]
