#!/usr/bin/env python3
"""Scan the checked-out repo's remote branches and emit stale/escalate candidates as JSON.

Standalone by design (WO-1535's AC): no ClickUp dependency, no network calls beyond
`gh` (used only for the default-branch lookup and one open-PR listing, both read-only).
Run from a checkout with `fetch-depth: 0` so merge status and commit history are real,
not paginated API calls.

DELETE_DAYS (0 = off) adds a third tier, "delete": idle at least that long, no open PR
from it and no open PR into it (deleting a PR's base branch closes the PR). scan.py only
labels them; prune.py deletes them a run later, after the report has announced them. A
branch matching KEEP_GLOBS never enters that tier: it stays in the report, marked "kept",
in the tier its age gives it.

Because an open PR is what spares a branch from that tier, a failed or unreadable open-PR
lookup is fatal when DELETE_DAYS is set instead of reading as "no PR", and so is a failed
default-branch lookup, which decides what counts as merged.

Usage: STALE_DAYS=30 ESCALATE_DAYS=60 DELETE_DAYS=70 PROTECTED_GLOBS="main,dev,release/*,hotfix/*" \
       KEEP_GLOBS="backup/*" python scan.py > stale_branches.json
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
import time


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def run_ok(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stdout


def detect_default_branch(strict=False):
    """Prefer the GitHub API (authoritative, works from any ref); fall back to the
    origin/HEAD symref; fall back to 'main' if both are unavailable. In CI the symref
    does not exist (actions/checkout does not create it), so with deletion on a failed
    API lookup is fatal rather than a guess that could make dev-merged branches look
    unmerged."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        ok, out = run_ok(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
        if ok and out.strip():
            return out.strip()
        if strict:
            sys.exit(f"default-branch lookup failed for {repo}; refusing to mark branches for deletion blind.")
    ok, out = run_ok(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if ok and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    return "main"


def split_globs(value):
    # Commas or any whitespace: an Actions variable may hold one pattern per line, and
    # branch names cannot contain whitespace.
    return [g for g in re.split(r"[,\s]+", value) if g]


def matches_any(name, globs):
    return any(fnmatch.fnmatch(name, pat) for pat in globs)


def is_protected(name, default_branch, protected_globs):
    return name == default_branch or matches_any(name, protected_globs)


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


def open_prs(strict=False):
    """Every open PR in one call: {head branch: (url, author)} for PRs from this repo,
    and {base branch: url}. Returns ({}, {}) when the lookup fails in report-only mode."""
    ok, out = run_ok(
        [
            "gh", "pr", "list", "--state", "open", "--limit", "1000",
            "--json", "url,author,headRefName,baseRefName,isCrossRepository",
        ]
    )
    try:
        prs = json.loads(out) if ok else None
    except json.JSONDecodeError:
        prs = None
    if not isinstance(prs, list):
        if strict:
            sys.exit("open-PR lookup failed or returned no JSON list; refusing to mark branches for deletion blind.")
        return {}, {}
    heads, bases = {}, {}
    for pr in prs:
        if not pr.get("isCrossRepository"):  # a fork's branch name says nothing about ours
            heads.setdefault(pr.get("headRefName"), (pr.get("url"), (pr.get("author") or {}).get("login")))
        bases.setdefault(pr.get("baseRefName"), pr.get("url"))
    return heads, bases


def main():
    stale_days = int(os.environ.get("STALE_DAYS", "30"))
    escalate_days = int(os.environ.get("ESCALATE_DAYS", "60"))
    delete_days = max(int(os.environ.get("DELETE_DAYS", "") or 0), 0)
    protected_globs = split_globs(os.environ.get("PROTECTED_GLOBS", "main,dev,release/*,hotfix/*"))
    keep_globs = split_globs(os.environ.get("KEEP_GLOBS", ""))
    now = int(os.environ.get("STALE_BRANCHES_NOW", "") or time.time())

    default_branch = detect_default_branch(strict=delete_days > 0)

    ref_lines = run(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)|%(objectname)|%(committerdate:unix)|%(authorname)",
            "refs/remotes/origin",
        ]
    ).splitlines()

    branches = {}
    for line in ref_lines:
        if not line.strip() or "|" not in line:
            continue
        ref, sha, ts, author = line.split("|", 3)
        if not ref.startswith("origin/") or ref == "origin/HEAD":
            continue
        name = ref[len("origin/"):]
        branches[name] = {"sha": sha, "committer_ts": int(ts), "last_author": author}

    candidates = {n: v for n, v in branches.items() if not is_protected(n, default_branch, protected_globs)}

    merged_out = run(["git", "branch", "-r", "--merged", f"origin/{default_branch}"])
    merged = set()
    for line in merged_out.splitlines():
        n = line.strip().lstrip("* ").strip()
        if n.startswith("origin/"):
            merged.add(n[len("origin/"):])

    pr_heads, pr_bases = open_prs(strict=delete_days > 0)

    results = []
    for name, info in sorted(candidates.items()):
        if name in merged:
            continue  # already merged — handled by auto-delete/backfill, not this job
        age_days = (now - info["committer_ts"]) // 86400
        if age_days < stale_days:
            continue

        pr_url, pr_author = pr_heads.get(name, (None, None))
        base_of_pr = pr_bases.get(name)
        kept = matches_any(name, keep_globs)
        if delete_days and age_days >= delete_days and not pr_url and not base_of_pr and not kept:
            tier = "delete"
        elif age_days >= escalate_days:
            tier = "escalate"
        else:
            tier = "stale"
        created_by = pr_author or branch_created_by(default_branch, name, info["last_author"])

        results.append(
            {
                "branch": name,
                "sha": info["sha"],
                "created_by": created_by,
                "last_activity": time.strftime("%Y-%m-%d", time.gmtime(info["committer_ts"])),
                "last_activity_by": info["last_author"],
                "age_days": age_days,
                "tier": tier,
                "pr_url": pr_url,
                "base_of_pr": base_of_pr,
                "kept": kept,
            }
        )

    results.sort(key=lambda r: -r["age_days"])
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
