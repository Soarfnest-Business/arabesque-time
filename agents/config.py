import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class AgentConfig:
    allowed_suffixes: List[str] = field(
        default_factory=lambda: [
            ".html",
            ".css",
            "README.md",
        ]
    )
    max_files: int = int(os.getenv("AGENT_MAX_FILES", "5"))
    max_total_lines: int = int(os.getenv("AGENT_MAX_LINES", "300"))
    default_branch_candidates: List[str] = field(
        default_factory=lambda: ["dgm", "main", "master"]
    )
    base_branch: str = os.getenv("AGENT_BASE_BRANCH", "dgm")

    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_api_key: str = (
        os.getenv("OPENAI_API_KEY") or os.getenv("OPEN_AI") or os.getenv("OPENAI_KEY") or ""
    )
    # Optional enterprise/proxy params
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_org: str = os.getenv("OPENAI_ORG") or os.getenv("OPENAI_ORGANIZATION", "")

    github_token: str = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT") or ""
    github_repo: str = os.getenv("GITHUB_REPOSITORY", "")
