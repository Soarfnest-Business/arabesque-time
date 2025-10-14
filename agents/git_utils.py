import os
import re
import subprocess
from typing import Optional


def run(cmd: list[str], cwd: Optional[str] = None) -> str:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{res.stderr}")
    return res.stdout.strip()


def get_repo_root() -> str:
    return run(["git", "rev-parse", "--show-toplevel"])


def get_current_branch() -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"])  # may be HEAD in detached


def detect_default_branch(candidates: list[str]) -> str:
    try:
        symref = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])  # -> refs/remotes/origin/main
        m = re.search(r"origin/(.+)$", symref)
        if m:
            return m.group(1)
    except Exception:
        pass
    branches = run(["git", "branch", "-r"]).splitlines()
    names = [b.strip().split("/")[-1] for b in branches if "origin/" in b]
    for c in candidates:
        if c in names:
            return c
    return "main"


def parse_repo_slug_from_remote() -> Optional[str]:
    try:
        url = run(["git", "remote", "get-url", "origin"]).strip()
    except Exception:
        return None
    m = re.match(r"https?://[^/]+/([^/]+/[^/.]+)(?:\.git)?$", url)
    if m:
        return m.group(1)
    m = re.match(r"git@[^:]+:([^/]+/[^/.]+)(?:\.git)?$", url)
    if m:
        return m.group(1)
    return None


def create_branch(branch: str):
    try:
        remotes = run(["git", "remote"]).splitlines()
    except Exception:
        remotes = []
    if any(r.strip() == "origin" for r in remotes):
        try:
            run(["git", "fetch", "origin", "--prune"])
        except Exception:
            pass
    run(["git", "checkout", "-b", branch])


def commit_all(message: str):
    try:
        name = run(["git", "config", "user.name"]).strip()
    except Exception:
        name = ""
    try:
        email = run(["git", "config", "user.email"]).strip()
    except Exception:
        email = ""
    if not name:
        run(["git", "config", "user.name", os.getenv("GIT_AUTHOR_NAME", "agent-bot")])
    if not email:
        run(["git", "config", "user.email", os.getenv("GIT_AUTHOR_EMAIL", "agent-bot@example.com")])
    run(["git", "add", "-A"])  # add all changes
    run(["git", "commit", "-m", message])


def push_branch(branch: str):
    remote = ensure_authed_remote()
    run(["git", "push", "-u", remote, branch])


def checkout_branch(branch: str):
    run(["git", "checkout", branch])


def fetch_branch(remote: str, branch: str):
    # Use provided remote if it exists; otherwise fall back to authed remote
    try:
        remotes = [r.strip() for r in run(["git", "remote"]).splitlines()]
    except Exception:
        remotes = []
    use_remote = remote if remote in remotes else ensure_authed_remote()
    run(["git", "fetch", use_remote, f"{branch}:{branch}"])


def ensure_authed_remote() -> str:
    """Ensure an authenticated remote exists and return its name.
    Creates/updates 'agent-origin' pointing to https://x-access-token:<token>@github.com/<slug>.git
    """
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT")
    slug = parse_repo_slug_from_remote() or os.getenv("GITHUB_REPOSITORY")
    if not (token and slug):
        raise RuntimeError("Cannot configure remote: missing GITHUB_TOKEN/PAT or GITHUB_REPOSITORY")

    authed = f"https://x-access-token:{token}@github.com/{slug}.git"
    name = "agent-origin"
    try:
        # If remote exists, update URL; otherwise add
        remotes = [r.strip() for r in run(["git", "remote"]).splitlines()]
        if name in remotes:
            run(["git", "remote", "set-url", name, authed])
        else:
            run(["git", "remote", "add", name, authed])
    except Exception as e:
        raise RuntimeError(f"Failed to configure authenticated remote: {e}")
    return name
