CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "finance": [
        "expense", "expenses", "spend", "spending", "budget", "invoice", "invoicing",
        "payment", "payments", "billing", "payroll", "accounting", "bookkeeping", "reimburse",
    ],
    "communication": [
        "chat", "message", "messaging", "email", "notify", "notification",
        "collaborate", "team communication", "standup", "announcement",
    ],
    "knowledge-management": [
        "documentation", "docs", "wiki", "notes", "note-taking", "knowledge base",
        "second brain", "research notes",
    ],
    "developer-tools": [
        "code", "coding", "repository", "repo", "git", "pull request", "issue tracker",
        "ci/cd", "deploy", "sdk", "api integration", "developer",
        "package", "npm", "pypi", "dependency", "library",
    ],
    "databases": [
        "database", "sql", "postgres", "mysql", "backend as a service", "data warehouse",
        "store data", "schema",
    ],
    "files": ["file", "files", "storage", "drive", "folder", "sync files", "csv", "spreadsheet"],
    "automation": [
        "automate", "automation", "workflow", "workflows", "zap", "integrate apps",
        "no-code", "pipeline", "browser automation", "scraping", "web scraping",
    ],
    "project-management": [
        "project management", "task management", "tasks", "sprint", "backlog", "kanban",
        "roadmap", "tickets", "jira",
    ],
    "monitoring": [
        "monitoring", "errors", "error tracking", "crash", "logs", "observability",
        "uptime", "alerts", "metrics",
    ],
    "weather": [
        "weather", "forecast", "temperature", "rain", "rainy", "snow", "sunny",
        "cloudy", "humidity", "wind speed", "climate", "celsius", "fahrenheit",
    ],
    "research": [
        "research", "researching", "search", "web search", "search the web",
        "look up", "define", "definition", "summary of", "what is",
    ],
}

SENSITIVE_TERMS = [
    "payment", "payments", "pii", "personal data", "health", "medical",
    "financial", "credentials", "customer data", "ssn", "banking",
]

FREE_TERMS = ["free", "open source", "open-source", "oss", "no budget", "cheap"]


def parse_intent(problem: str) -> dict:
    text = problem.lower()
    scores = {cat: sum(1 for kw in kws if kw in text) for cat, kws in CATEGORY_KEYWORDS.items()}
    best_cat, best_score = max(scores.items(), key=lambda kv: kv[1])
    category_hint = best_cat if best_score > 0 else None
    matched_keywords = [kw for kw in CATEGORY_KEYWORDS.get(category_hint, []) if kw in text] if category_hint else []
    return {
        "category_hint": category_hint,
        "constraints": {
            "free_or_oss_preferred": any(t in text for t in FREE_TERMS),
            "handles_sensitive_data": any(t in text for t in SENSITIVE_TERMS),
        },
        "matched_keywords": matched_keywords,
    }
