import json
import os
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import AgentConfig
from .git_utils import (
    get_repo_root,
    get_current_branch,
    fetch_branch,
    checkout_branch,
    commit_all,
    push_branch,
)
from .proposer import Proposer
from .llm import chat_completion

logger = logging.getLogger(__name__)


@dataclass
class ReviewDecision:
    action: str  # "approve" | "request_changes" | "reject"
    summary: str
    suggestions: List[str]


def _collect_pr_context(pr) -> Tuple[List[dict], int]:
    files = []
    total_changed = 0
    for f in pr.get_files():
        item = {
            "filename": f.filename,
            "status": f.status,
            "additions": f.additions,
            "deletions": f.deletions,
            "changes": f.changes,
            "patch": f.patch or "",
        }
        total_changed += int(f.changes or 0)
        files.append(item)
    return files, total_changed


def _openai_decide(cfg: AgentConfig, title: str, body: str, files: List[dict]) -> ReviewDecision:
    try:
        import openai

        if not cfg.openai_api_key:
            raise RuntimeError("OpenAI API key not configured")
        openai.api_key = cfg.openai_api_key

        # Keep prompt concise: include filenames and truncated patches
        parts = []
        for f in files[:20]:
            patch = f.get("patch", "")
            if len(patch) > 2000:
                patch = patch[:2000] + "\n... (truncated)"
            parts.append(
                f"# {f['filename']} (+{f['additions']}/-{f['deletions']})\n{patch}"
            )
        content = "\n\n".join(parts)
        system = (
            "You are a strict code reviewer for a Flask+Jinja UI repo. "
            "Focus on validity, safety, small diff size, allowed paths (templates/*.html, static/*.css, README.md). "
            "Return a compact JSON: {action, summary, suggestions[]} where action is one of approve, request_changes, reject."
        )
        user = (
            f"Title: {title}\nBody: {body[:1000]}\nFiles and patches:\n{content}\n"
            "Decide based on: small changes, no backend breakage, HTML validity, CSS sanity, accessibility improvements."
        )
        logger.info("reviewer.openai.start model=%s files=%d", cfg.openai_model, len(files))
        t0 = time.monotonic()
        resp = chat_completion(
            cfg,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=cfg.openai_model,
            temperature=0.0,
        )
        dt = time.monotonic() - t0
        logger.info("reviewer.openai.call.ok model=%s dt=%.2fs", cfg.openai_model, dt)
        txt = resp.choices[0].message.content or "{}"
        data = json.loads(txt)
        action = data.get("action", "request_changes")
        summary = data.get("summary", "Automatic review")
        suggestions = data.get("suggestions", [])
        if action not in {"approve", "request_changes", "reject"}:
            action = "request_changes"
        return ReviewDecision(action=action, summary=summary, suggestions=suggestions)
    except Exception as e:
        logger.exception("reviewer.openai.call.failed model=%s", cfg.openai_model)
        raise RuntimeError(f"OpenAI review failed: {e}")


def review_and_act(pr_number: int, auto_fix: bool = True, auto_merge: bool = True) -> str:
    from github import Github

    cfg = AgentConfig()
    if not cfg.github_token:
        raise RuntimeError("Missing GITHUB_TOKEN/GITHUB_PAT")
    slug = cfg.github_repo
    if not slug:
        raise RuntimeError("Missing GITHUB_REPOSITORY")

    gh = Github(cfg.github_token)
    repo = gh.get_repo(slug)
    pr = repo.get_pull(pr_number)
    files, _ = _collect_pr_context(pr)

    decision = _openai_decide(cfg, pr.title, pr.body or "", files)

    # Determine if the acting user is the PR author (GitHub forbids reviewing own PR)
    try:
        acting_login = gh.get_user().login
    except Exception:
        acting_login = ""
    pr_author = (pr.user.login or "") if getattr(pr, "user", None) else ""
    own_pr = acting_login.lower() == pr_author.lower() and acting_login != ""

    # Post review or fall back to comment when own PR
    if not own_pr:
        event = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "reject": "REQUEST_CHANGES",
        }[decision.action]
        pr.create_review(body=decision.summary, event=event)
    else:
        # Use a plain comment to avoid 422 on self-reviews
        pr.create_issue_comment(f"Auto-review (self): {decision.action}\n\n{decision.summary}")

    if decision.action == "approve" and auto_merge:
        pr.merge(merge_method="squash", commit_message=pr.title)
        return f"approved_and_merged:{pr.html_url}"

    if decision.action == "request_changes" and auto_fix:
        # Try to apply fixes using proposer on the PR head branch
        head_ref = pr.head.ref
        root = get_repo_root()
        curr = get_current_branch()
        try:
            fetch_branch("origin", head_ref)
            checkout_branch(head_ref)

            proposer = Proposer(root, cfg)
            goal = (
                "以下の指摘へ対応して修正: "
                + "; ".join(decision.suggestions)[:400]
            )
            files = proposer.propose(goal)
            proposer.apply_files(files)
            commit_all(f"agent(review): apply suggested fixes - {goal[:60]}")
            push_branch(head_ref)
        finally:
            try:
                checkout_branch(curr)
            except Exception:
                pass

        # Re-evaluate quickly; if OK, merge
        pr = repo.get_pull(pr_number)  # refresh
        files2, _ = _collect_pr_context(pr)
        decision2 = _openai_decide(cfg, pr.title, pr.body or "", files2)
        if decision2.action == "approve" and auto_merge:
            if not own_pr:
                pr.create_review(body="自動修正後の再レビュー: 承認", event="APPROVE")
            else:
                pr.create_issue_comment("自動修正後の再レビュー: 承認")
            pr.merge(merge_method="squash", commit_message=pr.title)
            return f"fixed_then_merged:{pr.html_url}"
        else:
            if not own_pr:
                pr.create_review(body="自動修正を試みましたが、承認条件を満たしません。追加対応が必要です。", event="COMMENT")
            else:
                pr.create_issue_comment("自動修正を試みましたが、承認条件を満たしません。追加対応が必要です。")
            return f"fix_applied_needs_followup:{pr.html_url}"

    return f"review_completed:{pr.html_url}:{decision.action}"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PR reviewer")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--no-fix", action="store_true", help="Do not attempt auto-fix")
    parser.add_argument("--no-merge", action="store_true", help="Do not auto-merge")
    args = parser.parse_args()

    res = review_and_act(args.pr, auto_fix=not args.no_fix, auto_merge=not args.no_merge)
    print(res)


if __name__ == "__main__":
    main()
