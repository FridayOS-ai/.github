#!/usr/bin/env python3
"""Delete the remote branches whose work is already in the default branch.

GitHub's delete-on-merge removes only the head branch of the PR that merged. A branch
that reaches the default branch some other way stays: a branch stacked under another
PR, a Preview-train branch merged inside one combined PR, a branch merged by hand. On
9 Nov 2026, 205 of fros-desktop's 225 merged branches were of that kind (fridayos-hub
01-features/platform/research/2026-11-09-origin-branch-prune-audit). This script sweeps
them on every weekly run, so a merged branch is gone within a week.

A branch is deleted only if ALL of these hold:
  - its work is in the default branch, by one of:
      merged          its tip is an ancestor of the default branch
      patches in      every one of its commits has an equal patch there (`git cherry`)
      squash-merged   every file it changed matches the default branch today
  - no open PR comes from it and no open PR goes into it (deleting a PR's base closes it),
  - it is not protected and not on the keep list,
  - its last commit is at least MERGED_GRACE_DAYS old (default 1), so a branch just cut
    from the default branch's tip, with nothing pushed yet, is left alone.

No notice run and no archive tag: nothing is lost, because the work is in the default
branch (and a branch that had a PR keeps its commits at refs/pull/<n>/head). Each
deletion carries a lease on the tip that was checked, so a branch pushed to during the
run is left alone.

Like scan.py, it fails closed: a failed default-branch or open-PR lookup stops the run
before anything is deleted.

Usage: python merged.py [deletions.json] > merged.json   (in a checkout with fetch-depth: 0)
       deletions.json is prune.py's output from this run; the branches it deleted are skipped.
Env: DRY_RUN=true|false, MERGED_GRACE_DAYS, PROTECTED_GLOBS, KEEP_GLOBS,
     GITHUB_REPOSITORY, GH_TOKEN (contents:write), STALE_BRANCHES_NOW (testing).
Output: {"dry_run", "default_branch", "deleted": [...], "failed": [...]}
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import detect_default_branch, matches_any, open_prs, run, run_ok, split_globs  # noqa: E402


def log(msg):
    sys.stderr.write(msg + "\n")


def remote_branches():
    out = run(["git", "for-each-ref", "--format=%(refname:short)|%(objectname)|%(committerdate:unix)|%(authorname)",
               "refs/remotes/origin"])
    branches = {}
    for line in out.splitlines():
        ref, sha, ts, author = line.split("|", 3)
        if ref.startswith("origin/") and ref != "origin/HEAD":
            branches[ref[len("origin/"):]] = {"sha": sha, "ts": int(ts), "author": author}
    return branches


def how_landed(base, ref):
    """How the branch's work reached the base branch, or None if some of it has not."""
    if subprocess.run(["git", "merge-base", "--is-ancestor", ref, base]).returncode == 0:
        return "merged"
    ok, out = run_ok(["git", "cherry", base, ref])
    if ok and not any(line.startswith("+") for line in out.splitlines()):
        return "patches in"
    ok, mb = run_ok(["git", "merge-base", base, ref])
    if not ok:
        return None
    ok, files = run_ok(["git", "diff", "--name-only", "--no-renames", mb.strip(), ref])
    files = [f for f in files.splitlines() if f] if ok else None
    if files is None:
        return None
    if not files:
        return "patches in"  # its commits add up to no change
    same = subprocess.run(["git", "diff", "--quiet", "--no-renames", base, ref, "--", *files]).returncode == 0
    return "squash-merged" if same else None


def delete(branch, sha, dry_run):
    if dry_run:
        return None
    result = subprocess.run(
        ["git", "push", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{sha}", "origin", f":refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    reasons = [line.split("\t")[-1] for line in result.stdout.splitlines() if line.startswith("!")]
    reason = (reasons or result.stderr.strip().splitlines() or ["git push failed"])[0]
    if "stale info" in reason:
        return "pushed to during this run, left alone"
    return reason


def main():
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    grace_days = max(int(os.environ.get("MERGED_GRACE_DAYS", "") or 1), 0)
    protected_globs = split_globs(os.environ.get("PROTECTED_GLOBS", "main,dev,release/*,hotfix/*"))
    keep_globs = split_globs(os.environ.get("KEEP_GLOBS", ""))
    now = int(os.environ.get("STALE_BRANCHES_NOW", "") or time.time())

    default_branch = detect_default_branch(strict=True)
    pr_heads, pr_bases = open_prs(strict=True)
    base = f"origin/{default_branch}"

    already_gone = set()
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            already_gone = {b["branch"] for b in json.load(f).get("deleted", [])}

    deleted, failed = [], []
    for name, info in sorted(remote_branches().items()):
        if name in already_gone:
            continue
        if name == default_branch or matches_any(name, protected_globs) or matches_any(name, keep_globs):
            continue
        if name in pr_heads or name in pr_bases:
            continue
        age_days = (now - info["ts"]) // 86400
        if age_days < grace_days:
            continue
        how = how_landed(base, f"origin/{name}")
        if not how:
            continue
        row = {"branch": name, "sha": info["sha"], "how": how, "age_days": age_days, "last_activity_by": info["author"]}
        error = delete(name, info["sha"], dry_run)
        if error:
            log(f"Could not delete {name}: {error}")
            failed.append({**row, "error": error})
        else:
            log(f"{'Would delete' if dry_run else 'Deleted'} {name} ({info['sha'][:7]}, {how}).")
            deleted.append(row)

    json.dump({"dry_run": dry_run, "default_branch": default_branch, "deleted": deleted, "failed": failed},
              sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
