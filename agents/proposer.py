import json
import os
import time
import logging
from dataclasses import dataclass
from typing import List, Optional

from .config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass
class ProposedFile:
    path: str
    content: str


class Proposer:
    def __init__(self, repo_root: str, cfg: Optional[AgentConfig] = None):
        self.repo_root = repo_root
        self.cfg = cfg or AgentConfig()

    def _is_allowed(self, rel_path: str) -> bool:
        allowed = any(rel_path.endswith(s) for s in self.cfg.allowed_suffixes)
        if not allowed:
            return False
        if rel_path.endswith((".html", ".css")):
            return rel_path.startswith("templates/") or rel_path.startswith("static/")
        return True

    def _fallback_small_change(self, goal: str) -> List[ProposedFile]:
        readme_path = os.path.join(self.repo_root, "README.md")
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                readme = f.read()
        except FileNotFoundError:
            readme = ""
        banner = (
            "\n\n---\n"
            "Auto-proposed update by agent (preview).\n\n"
            f"Goal: {goal}\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        return [ProposedFile(path="README.md", content=readme + banner)]

    def _propose_with_openai(self, goal: str) -> List[ProposedFile]:
        import openai

        if not self.cfg.openai_api_key:
            raise RuntimeError("OpenAI API key not configured")
        openai.api_key = self.cfg.openai_api_key
        logger.info("proposer.openai.start model=%s goal_len=%d", self.cfg.openai_model, len(goal))

        candidates: List[str] = []
        for root, _dirs, files in os.walk(self.repo_root):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), self.repo_root)
                if self._is_allowed(rel):
                    candidates.append(rel)
        candidates = sorted(candidates)[:20]
        logger.info("proposer.openai.files candidates=%d", len(candidates))

        system = (
            "You are a careful code editor. Generate small, safe UI-focused edits. "
            "Only modify files we allow. Return JSON array [{path, content}] with full file contents."
        )
        user = (
            "Goal: "
            + goal
            + "\nAllowed files (subset):\n- "
            + "\n- ".join(candidates)
            + "\nConstraints: max 3 files, max ~250 lines total."
        )
        t0 = time.monotonic()
        resp = openai.chat.completions.create(
            model=self.cfg.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        dt = time.monotonic() - t0
        logger.info("proposer.openai.call.ok model=%s dt=%.2fs", self.cfg.openai_model, dt)
        text = resp.choices[0].message.content or "[]"
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.exception("proposer.openai.decode.failed")
            raise RuntimeError("OpenAI returned no valid proposal") from e

        results: List[ProposedFile] = []
        total_lines = 0
        for item in data:
            path = item.get("path")
            content = item.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            if not self._is_allowed(path):
                continue
            line_count = content.count("\n") + 1
            if len(results) >= self.cfg.max_files or total_lines + line_count > self.cfg.max_total_lines:
                break
            results.append(ProposedFile(path=path, content=content))
            total_lines += line_count

        if not results:
            raise RuntimeError("OpenAI returned no valid proposal")
        return results

    def propose(self, goal: str) -> List[ProposedFile]:
        # Propagate errors to the caller (no silent fallback)
        return self._propose_with_openai(goal)

    def apply_files(self, files: List[ProposedFile]):
        for f in files:
            abs_path = os.path.join(self.repo_root, f.path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as fp:
                fp.write(f.content)
