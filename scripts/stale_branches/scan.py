#!/usr/bin/env python3
"""Scan the checked-out repo's remote branches and emit stale/escalate candidates as JSON.

Standalone by design (WO-1535's AC): no ClickUp dependency, no network calls beyond
`gh` (used only for the default-branch lookup and open-PR lookup, both read-only).
Run from a checkout with `fetch-depth: 0` so merge status and commit history are real,
not paginated API calls.

Usage: STALE_DAYS=30 ESCALATE_DAYS=60 PROTECTED_GLOBS="main,dev,release/*,hotfix/*" \
       python scan.py > stale_branches.json
"""
import fnmatch
import json
import os
import subprocess
import sys
import time


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def run_ok(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stdout


def detect_default_branch():
    """Prefer the GitHub API (authoritative, works from any ref); fall back to the
    origin/HEAD symref; fall back to 'main' if both are unavailable."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        ok, out = run_ok(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
        if ok and out.strip():
            return out.strip()
    ok, out = run_ok(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if ok and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    return "main"


def is_protected(name, default_branch, protected_globs):
    if name == default_branch:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in protected_globs)


def branch_created_by(default_branch, name, fallback_author):
    """Author of the oldest commit UNIQUE to this branch (not shared with the base
    branch) — the best available proxy for "who created this branch". Using plain
    `git log <branch> --reverse -1` without the `base..branch` range would instead
    return the repo's very first commit ever, since most history is reachable from
    every branch tip back to the root commit."""
    ok, out = run_ok(
        ["git", "log", f"origin/{default_branch}..origin/{name}", "--reverse", "--format=%an", "-1"]
    )
    name_out = out.strip()
    return name_out if ok and name_out else fallback_author


def open_pr_for(name):
    ok, out = run_ok(
        ["gh", "pr", "list", "--head", name, "--state", "open", "--json", "number,url,author"]
    )
    if not ok or not out.strip():
        return None, None
    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return None, None
    if not prs:
        return None, None
    pr = prs[0]
    return pr.get("url"), (pr.get("author") or {}).get("login")


def main():
    stale_days = int(os.environ.get("STALE_DAYS", "30"))
    escalate_days = int(os.environ.get("ESCALATE_DAYS", "60"))
    protected_globs = [
        g.strip() for g in os.environ.get("PROTECTED_GLOBS", "main,dev,release/*,hotfix/*").split(",") if g.strip()
    ]
    now = int(os.environ.get("STALE_BRANCHES_NOW", "") or time.time())

    default_branch = detect_default_branch()

    ref_lines = run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)|%(committerdate:unix)|%(authorname)",
            "refs/remotes/origin",
        ]
    ).splitlines()

    branches = {}
    for line in ref_lines:
        if not line.strip() or "|" not in line:
            continue
        ref, ts, author = line.split("|", 2)
        if not ref.startswith("origin/") or ref == "origin/HEAD":
            continue
        name = ref[len("origin/"):]
        branches[name] = {"committer_ts": int(ts), "last_author": author}

    candidates = {n: v for n, v in branches.items() if not is_protected(n, default_branch, protected_globs)}

    merged_out = run(["git", "branch", "-r", "--merged", f"origin/{default_branch}"])
    merged = set()
    for line in merged_out.splitlines():
        n = line.strip().lstrip("* ").strip()
        if n.startswith("origin/"):
            merged.add(n[len("origin/"):])

    results = []
    for name, info in sorted(candidates.items()):
        if name in merged:
            continue  # already merged — handled by auto-delete/backfill, not this job
        age_days = (now - info["committer_ts"]) // 86400
        if age_days < stale_days:
            continue
        tier = "escalate" if age_days >= escalate_days else "stale"

        pr_url, pr_author = open_pr_for(name)
        created_by = pr_author or branch_created_by(default_branch, name, info["last_author"])

        results.append(
            {
                "branch": name,
                "created_by": created_by,
                "last_activity": time.strftime("%Y-%m-%d", time.gmtime(info["committer_ts"])),
                "last_activity_by": info["last_author"],
                "age_days": age_days,
                "tier": tier,
                "pr_url": pr_url,
            }
        )

    results.sort(key=lambda r: -r["age_days"])
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
