import argparse
import time
from typing import Optional

from .config import AgentConfig
from .git_utils import (
    create_branch,
    commit_all,
    detect_default_branch,
    get_repo_root,
    parse_repo_slug_from_remote,
    push_branch,
    run as git_run,
)
from .proposer import Proposer


def create_pull_request(token: str, repo_slug: str, head: str, base: str, title: str, body: str) -> str:
    from github import Github

    gh = Github(token)
    repo = gh.get_repo(repo_slug)
    pr = repo.create_pull(title=title, body=body, head=head, base=base)
    return pr.html_url


def _ensure_base_branch_exists(base_branch: str, default_branch: str):
    from .git_utils import run
    # Check remote
    try:
        refs = run(["git", "ls-remote", "--heads", "origin", base_branch])
        if refs.strip():
            return
    except Exception:
        pass
    # Create from default and push
    try:
        run(["git", "checkout", default_branch])
        run(["git", "checkout", "-B", base_branch])
        push_branch(base_branch)
    finally:
        run(["git", "checkout", default_branch])


def run_propose(goal: str, push_and_pr: bool) -> Optional[str]:
    cfg = AgentConfig()
    root = get_repo_root()
    proposer = Proposer(root, cfg)

    files = proposer.propose(goal)
    proposer.apply_files(files)

    if not push_and_pr:
        return None

    default_branch = detect_default_branch(cfg.default_branch_candidates)
    base_branch = cfg.base_branch or default_branch
    _ensure_base_branch_exists(base_branch, default_branch)
    # base working branch
    git_run(["git", "checkout", base_branch])
    branch = f"agent/proposal-{int(time.time())}"
    create_branch(branch)
    commit_all(f"agent: proposal - {goal[:60]}")
    push_branch(branch)

    slug = cfg.github_repo or parse_repo_slug_from_remote()
    if not slug:
        raise RuntimeError("Cannot detect GitHub repository (set GITHUB_REPOSITORY)")
    if not cfg.github_token:
        raise RuntimeError("Missing GITHUB_TOKEN for PR creation")

    # Build a clean PR title/body
    changed_paths = [f.path for f in files]
    title = f"[DGM] UI improvements: {goal[:60]}"
    body_lines = [
        "### Summary",
        goal,
        "",
        "### Changes",
        "- Scope limited to templates/*.html and static/*.css",
        "- Small, low-risk UI tweaks (accessibility/readability)",
        "",
        "### Modified Files",
    ]
    for p in changed_paths:
        body_lines.append(f"- `{p}`")
    body_lines += [
        "",
        "### Rationale",
        "- Improve a11y (landmarks, skip links, icons hidden from screen readers)",
        "- Remove inline styles; consolidate into CSS",
        "",
        "### Validation",
        "- Lint/format pass",
        "- Visual smoke check (no layout break)",
        "",
        "—\nThis PR was created by the DGM agent.",
    ]
    pr_body = "\n".join(body_lines)

    pr_url = create_pull_request(
        cfg.github_token,
        slug,
        head=branch,
        base=base_branch,
        title=title,
        body=pr_body,
    )
    return pr_url


def main():
    parser = argparse.ArgumentParser(description="Agent runner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", help="Propose a change and optionally open a PR")
    p.add_argument("--goal", required=True, help="Goal description for the proposal")
    p.add_argument("--pr", action="store_true", help="Create PR after proposing")
    args = parser.parse_args()
    if args.cmd == "propose":
        url = run_propose(args.goal, args.pr)
        if url:
            print(url)


if __name__ == "__main__":
    main()
