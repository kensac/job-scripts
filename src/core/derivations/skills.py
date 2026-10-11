"""One canonical spelling per technology, so a frequency table means something.

Postings write the same skill a dozen ways - "JS", "Javascript", "JavaScript
(ES6)" - and an aggregate that groups on the raw string reports twelve rare
skills instead of one common one. The extracted strings are stored exactly as
the posting wrote them; this maps them to a canonical form on the way into
job_skills, so a better mapping can be applied by re-running this over stored
raw values rather than by re-running a paid AI pass.
"""

from __future__ import annotations

import re

# Aliases observed in this corpus, not imagined: each left-hand side was
# extracted from a real posting and collapses onto a form that other postings
# spell out. Keys are already lowercased and space-collapsed by _normalize.
ALIASES: dict[str, str] = {
    "js": "javascript",
    "node": "node.js",
    "nodejs": "node.js",
    "node js": "node.js",
    "reactjs": "react",
    "react.js": "react",
    "vuejs": "vue",
    "vue.js": "vue",
    "angularjs": "angular",
    "golang": "go",
    "go lang": "go",
    "c sharp": "c#",
    "csharp": "c#",
    "c plus plus": "c++",
    "cpp": "c++",
    "postgres": "postgresql",
    "psql": "postgresql",
    "postgre sql": "postgresql",
    "ms sql": "sql server",
    "mssql": "sql server",
    "microsoft sql server": "sql server",
    "k8s": "kubernetes",
    "gcp": "google cloud",
    "google cloud platform": "google cloud",
    "amazon web services": "aws",
    "amazon web services (aws)": "aws",
    "microsoft azure": "azure",
    "ci/cd": "ci/cd",
    "cicd": "ci/cd",
    "continuous integration": "ci/cd",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "nlp": "natural language processing",
    "llm": "large language models",
    "llms": "large language models",
    "genai": "generative ai",
    "gen ai": "generative ai",
    "tf": "tensorflow",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "pandas/numpy": "pandas",
    "restful apis": "rest apis",
    "rest api": "rest apis",
    "restful api": "rest apis",
    "api development": "rest apis",
    "graphql apis": "graphql",
    "power bi": "power bi",
    "powerbi": "power bi",
    "ms excel": "excel",
    "microsoft excel": "excel",
    "advanced excel": "excel",
    "ms office": "microsoft office",
    "office 365": "microsoft office",
    "o365": "microsoft office",
    "unix": "linux",
    "unix/linux": "linux",
    "linux/unix": "linux",
    "shell scripting": "bash",
    "shell": "bash",
    "git/github": "git",
    "github": "git",
    "gitlab": "git",
    "version control": "git",
    "iac": "infrastructure as code",
    "terraform/iac": "terraform",
    "sap erp": "sap",
    "oracle db": "oracle",
    "oracle database": "oracle",
    "objective c": "objective-c",
    "dot net": ".net",
    "dotnet": ".net",
    ".net core": ".net",
    "asp.net": ".net",
    "spring boot": "spring",
    "springboot": "spring",
}

# Trailing qualifiers a posting hangs off a skill name; stripping them collapses
# "Python (preferred)" and "Python 3+" onto "python" without touching names that
# legitimately contain punctuation, like c++, c#, .net and node.js.
_TRAILING = re.compile(r"\s*[\(\[].*$|\s*[:;,]\s*$|\s+\d+(\.\d+)*\+?\s*$")
_WHITESPACE = re.compile(r"\s+")

# Lead-ins the model emits despite being told to write bare names: the pilot
# produced "Experience with Plaxis" alongside other postings' plain "Plaxis",
# which would split one skill into two buckets. Stripped here rather than
# leaned on the prompt for, because a prompt fix costs another paid pass and
# this one re-runs over stored raw values for free.
_LEAD_IN = re.compile(
    r"^(strong |solid |demonstrated |proven |excellent |advanced |basic |working |hands[- ]on )*"
    r"(experience (with|in|using)|expertise (with|in)|proficiency (with|in)|proficient (with|in)"
    r"|knowledge of|familiarity with|understanding of|background in|skills? (with|in)"
    r"|ability to use)\s+"
)

# Behavioural qualities the model keeps returning as skills despite the prompt.
# They are real requirements, but they are not the technology frequency table
# this powers, and one bucket of "communication" drowns the signal.
SOFT_SKILLS = frozenset(
    {
        "communication",
        "written communication",
        "verbal communication",
        "teamwork",
        "collaboration",
        "leadership",
        "organization",
        "organisation",
        "organizational skills",
        "time management",
        "problem solving",
        "problem-solving",
        "critical thinking",
        "analytical thinking",
        "attention to detail",
        "adaptability",
        "flexibility",
        "work ethic",
        "self-motivated",
        "interpersonal skills",
        "customer service",
        "multitasking",
    }
)

# Longer than any real technology name; anything past it is a sentence the model
# failed to reduce to a name, and grouping sentences produces noise, not a
# frequency table. "Amazon Web Services (AWS)" is 26 characters.
MAX_SKILL_CHARS = 40


def canonical(raw: str) -> str:
    """Canonical form of one extracted skill, or "" when it is not a skill name.

    Returning "" rather than the raw string is deliberate: an unusable value
    must not silently become its own bucket in the aggregate.
    """
    text = _WHITESPACE.sub(" ", (raw or "").strip().lower())
    text = _LEAD_IN.sub("", text)
    # Trailing punctuation goes, leading punctuation stays: ".net" and "c++"
    # are the names, and stripping a leading dot turned ".NET Core" into
    # "net core", which then missed its own alias.
    text = _TRAILING.sub("", text).lstrip(" -/&").rstrip(" .-/&")
    if not text or len(text) > MAX_SKILL_CHARS or text in SOFT_SKILLS:
        return ""
    return ALIASES.get(text, text)
