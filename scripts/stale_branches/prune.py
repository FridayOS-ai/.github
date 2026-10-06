#!/usr/bin/env python3
"""Delete the branches the previous weekly report announced, archiving each as a tag first.

Two runs per deletion, so nobody loses a branch without a week's notice in ClickUp:

  run N    scan.py puts a branch in the "delete" tier, report.py posts it under
           "deleted on the next weekly run", and the workflow saves this script's
           output as the run's announcement artifact.
  run N+1  this script deletes a branch only if ALL of these hold:
             - the most recent successful *scheduled* run announced it (scheduled
               runs are the ones that post; a manual dispatch never counts as notice),
             - that announcement is at least MIN_NOTICE_DAYS old,
             - this run's scan still has it in the "delete" tier (still idle, no open
               PR, not merged, not protected, not on the keep list), and
             - its tip is the exact commit that was announced.
           A push, a new PR, a merge or a keep-list entry in between spares it. With no announcement to
           act on (the first run after enabling, or the artifact expired) it deletes
           nothing and only announces.

Nothing is lost: each branch is copied to an `archive/<branch>` tag and deleted in
one atomic push, with a lease on the announced commit, so both happen or neither
does, and a branch that moved in the meantime is left alone. Restore with:
    git fetch origin tag archive/<branch>
    git push origin archive/<branch>:refs/heads/<branch>

Usage: python prune.py stale_branches.json > deletions.json
Env: DRY_RUN=true|false, GITHUB_REPOSITORY, GITHUB_RUN_ID, GH_TOKEN (needs actions:read
     to find the previous run, contents:write to push), STALE_BRANCHES_NOW (testing).
     PREVIOUS_ANNOUNCEMENT=<path> reads that file instead of the previous run's artifact.
Output: {"announced_at", "dry_run", "deleted", "failed", "announced"}; "announced" is
        what report.py lists for the next run, and what run N+1 reads back.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ARTIFACT = "stale-branch-deletions"  # must match the upload step in stale-branches.yml
MIN_NOTICE_DAYS = 6  # weekly cadence minus slack for cron jitter


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def log(msg):
    sys.stderr.write(msg + "\n")


def previous_announcement():
    path = os.environ.get("PREVIOUS_ANNOUNCEMENT")
    if path:
        with open(path) as f:
            return json.load(f)

    # Failures here raise on purpose: a token without actions:read must fail the run,
    # not read as "nothing was announced" and silently never delete.
    repo = os.environ["GITHUB_REPOSITORY"]
    workflow_id = run(
        ["gh", "api", f"repos/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}", "--jq", ".workflow_id"]
    ).strip()
    prev_run = run(
        [
            "gh", "api",
            f"repos/{repo}/actions/workflows/{workflow_id}/runs?event=schedule&status=success&per_page=1",
            "--jq", ".workflow_runs[0].id // empty",
        ]
    ).strip()
    if not prev_run:
        log("No earlier successful scheduled run; announcing only.")
        return None
    artifact = run(
        [
            "gh", "api", f"repos/{repo}/actions/runs/{prev_run}/artifacts",
            "--jq", f'.artifacts[] | select(.name == "{ARTIFACT}" and (.expired | not)) | .id',
        ]
    ).strip()
    if not artifact:
        log(f"Run {prev_run} left no {ARTIFACT} artifact (predates deletion, or expired); announcing only.")
        return None
    with tempfile.TemporaryDirectory() as d:
        run(["gh", "run", "download", prev_run, "-R", repo, "-n", ARTIFACT, "-D", d])
        with open(os.path.join(d, "deletions.json")) as f:
            return json.load(f)


def due_for_deletion(previous, current, now):
    """Announced last time, still in the delete tier now, and not moved since."""
    if not previous:
        return []
    notice_days = (now - previous["announced_at"]) / 86400
    if notice_days < MIN_NOTICE_DAYS:
        log(f"Last announcement is only {notice_days:.1f}d old (< {MIN_NOTICE_DAYS}d); deleting nothing.")
        return []
    now_doomed = {b["branch"]: b for b in current if b["tier"] == "delete"}
    due = []
    for old in previous["announced"]:
        new = now_doomed.get(old["branch"])
        if new is None:
            log(f"Spared {old['branch']}: gone, merged, kept, has an open PR, or no longer idle enough.")
        elif new["sha"] != old["sha"]:
            log(f"Spared {old['branch']}: tip moved {old['sha'][:7]} -> {new['sha'][:7]} since the announcement.")
        else:
            due.append(new)
    return due


def remote_tag_sha(tag):
    out = run(["git", "ls-remote", "origin", f"refs/tags/{tag}"]).split()
    return out[0] if out else None


def archive_and_delete(branch, sha, dry_run):
    """Returns (archive_tag, error). error is None on success."""
    tag = f"archive/{branch}"
    existing = remote_tag_sha(tag)
    if existing not in (None, sha):  # an older branch of the same name was archived before
        tag = f"archive/{branch}-{sha[:7]}"
        existing = remote_tag_sha(tag)
    if existing not in (None, sha):
        return tag, f"tag {tag} already exists at {existing[:7]}"
    if dry_run:
        return tag, None

    refspecs = [] if existing == sha else [f"{sha}:refs/tags/{tag}"]
    refspecs.append(f":refs/heads/{branch}")
    result = subprocess.run(
        ["git", "push", "--atomic", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{sha}", "origin", *refspecs],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # --porcelain prints each refused ref as "!<TAB>src:dst<TAB>reason"; the atomic
        # partner of the real refusal only says "atomic push failed".
        reasons = [
            line.split("\t")[-1]
            for line in result.stdout.splitlines()
            if line.startswith("!") and "atomic push failed" not in line
        ]
        reason = (reasons or result.stderr.strip().splitlines() or ["git push failed"])[0]
        if "stale info" in reason:
            reason = "pushed to during this run, left alone"
        return tag, reason
    return tag, None


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: prune.py <stale_branches.json>")
    with open(sys.argv[1]) as f:
        current = json.load(f)

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    now = int(os.environ.get("STALE_BRANCHES_NOW", "") or time.time())

    deleted, failed = [], []
    for b in due_for_deletion(previous_announcement(), current, now):
        tag, error = archive_and_delete(b["branch"], b["sha"], dry_run)
        if error:
            log(f"Could not delete {b['branch']}: {error}")
            failed.append({**b, "archive_tag": tag, "error": error})
        else:
            log(f"{'Would delete' if dry_run else 'Deleted'} {b['branch']} ({b['sha'][:7]}), archived as {tag}.")
            deleted.append({**b, "archive_tag": tag})

    # A failed deletion stays announced: the next run retries it if its tip is unchanged.
    gone = {b["branch"] for b in deleted}
    announced = [
        {k: b[k] for k in ("branch", "sha")}
        for b in current
        if b["tier"] == "delete" and b["branch"] not in gone
    ]

    json.dump(
        {"announced_at": now, "dry_run": dry_run, "deleted": deleted, "failed": failed, "announced": announced},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
