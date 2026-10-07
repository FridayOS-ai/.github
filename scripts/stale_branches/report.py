#!/usr/bin/env python3
"""Format scan.py's JSON into a ClickUp message and post it to FridayOS-Dev.

Same ClickUp v3 Chat API shape already proven in production by
companyos-ff's hubstaff-weekly-clickup.yml: POST .../chat/channels/{id}/messages,
Authorization header carries the raw token (NOT "Bearer "), content_format text/md.
Workspace id + FridayOS-Dev channel id below are non-secret config (see
fridayos-hub/tools/clickup/channels.sh, which documents them as safe to commit).
The channel id is overridable via CLICKUP_CHANNEL_ID (defaults to FridayOS-Dev)
so a future caller can route to a different channel without a code change;
the workspace id is not exposed as a variable -- there is only one FridayOS
ClickUp workspace, so making it configurable would just add an unused knob.

Token comes from 1Password, not a static GitHub secret: the calling workflow
resolves op://Friday/ClickUP-WL-API/credential via 1password/load-secrets-action
and exports it as CLICKUP_FRIDAY_TOKEN -- the same pattern already used by
qa-nightly.yml and fridayos-hub/tools/clickup/send.sh (which checks this exact
env var first). Single source of truth for rotation; no static copy of the
token lives in any repo's secret store.

KNOWN GAP: the approved decision (2026-08-27) calls for @-mentioning the author of
each escalated (>=60d) branch. ClickUp mentions need a structured reference to a
ClickUp *user id*, and nothing in this org's tooling maps a git/GitHub author name
to a ClickUp user id today (fridayos-hub/tools/clickup/send.sh's own DM resolver
only handles ClickUp workspace members by name/email, not arbitrary git authors).
Faking a plain "@name" string would not trigger a real ClickUp notification, so
this script bolds the author's name instead and does not claim to mention them.
Flagged in WO-1536's Notes as a follow-up, not silently shipped as "done".

Usage: python report.py stale_branches.json [deletions.json]
       (deletions.json is prune.py's output; pass it when deletion is on)
Env: CLICKUP_FRIDAY_TOKEN (required unless DRY_RUN=true), DRY_RUN=true|false,
     CLICKUP_CHANNEL_ID (optional, defaults to FridayOS-Dev),
     STALE_DAYS / ESCALATE_DAYS / DELETE_DAYS (section headings only)
"""
import json
import os
import sys
import urllib.error
import urllib.request

WORKSPACE_ID = "9018051827"  # the one FridayOS ClickUp workspace -- not configurable
DEFAULT_CHANNEL_ID = "8cr937k-450598"  # FridayOS-Dev


def format_report(branches, deletions=None):
    # GITHUB_REPOSITORY is "owner/repo"; show just the repo name so a channel
    # receiving reports from several repos can tell them apart.
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    suffix = f" — {repo}" if repo else ""
    stale_days = os.environ.get("STALE_DAYS") or "30"
    escalate_days = os.environ.get("ESCALATE_DAYS") or "60"
    delete_days = os.environ.get("DELETE_DAYS") or "70"

    if not branches:
        return f"✅ No stale branches this week{suffix}.\n\n*Sent from FridayOS*"

    deletions = deletions or {"deleted": [], "failed": [], "announced": []}
    # A failed deletion stays announced so prune.py can retry it, but this report
    # already lists it under "Not deleted"; don't promise it for next week too.
    failed = {b["branch"] for b in deletions["failed"]}
    announced = {b["branch"] for b in deletions["announced"]} - failed
    next_run = [b for b in branches if b["branch"] in announced]
    # A kept branch (KEEP_GLOBS) is listed once, in its own section, so the keep
    # list can be read at a glance; it never enters the delete tier.
    kept = [b for b in branches if b.get("kept")]
    escalate = [b for b in branches if b["tier"] == "escalate" and not b.get("kept")]
    stale = [b for b in branches if b["tier"] == "stale" and not b.get("kept")]

    lines = [f"**Weekly stale-branch report{suffix}**", ""]

    def render_section(title, rows, footer=None):
        out = [f"**{title}**", ""]
        for b in rows:
            pr_part = f" — [PR]({b['pr_url']})" if b.get("pr_url") else ""
            if b.get("base_of_pr"):
                pr_part += f" — [PR into it]({b['base_of_pr']})"
            author = b["created_by"] if b["tier"] == "stale" else f"**{b['created_by']}**"
            row = (
                f"- `{b['branch']}`{pr_part} — created by {author}, "
                f"last activity {b['last_activity']} by {b['last_activity_by']} "
                f"({b['age_days']}d idle)"
            )
            if b.get("error"):
                row += f" — {b['error']}"
            elif b.get("archive_tag"):
                row += f" → `{b['archive_tag']}`"
            out.append(row)
        if footer:
            out += ["", footer]
        out.append("")
        return out

    if deletions["deleted"]:
        verb = "Would delete (dry run)" if deletions.get("dry_run") else "Deleted this run"
        lines += render_section(
            f"🗑️ {verb}, archived as tags",
            deletions["deleted"],
            "Restore one with `git fetch origin tag <tag>` then `git push origin <tag>:refs/heads/<branch>`, "
            "using the tag on its row. A restored branch is as idle as before: push a commit to it, open a PR "
            "from it, or add it to `STALE_BRANCH_KEEP`, or it is announced again.",
        )
    if deletions["failed"]:
        lines += render_section(
            "⚠️ Not deleted this run (see the run log); retried on the next weekly run if unchanged",
            deletions["failed"],
        )
    if next_run:
        lines += render_section(
            f"⏳ Deleted on the next weekly run (≥{delete_days}d, no open PR)",
            next_run,
            "To keep one, push a commit to it, open a PR from it, or add it to the repo's "
            "`STALE_BRANCH_KEEP` variable before then.",
        )
    if escalate:
        lines += render_section(f"🔴 Escalate (≥{escalate_days}d)", escalate)
    if stale:
        lines += render_section(f"🟡 Stale (≥{stale_days}d)", stale)
    if kept:
        lines += render_section(
            "🛡️ Kept, never deleted (`STALE_BRANCH_KEEP`)",
            kept,
            "Remove a pattern from the repo's `STALE_BRANCH_KEEP` variable to let its branches be cleaned up again.",
        )

    lines.append("*Sent from FridayOS*")
    return "\n".join(lines)


def post(content, token, channel_id):
    api_url = f"https://api.clickup.com/api/v3/workspaces/{WORKSPACE_ID}/chat/channels/{channel_id}/messages"
    payload = json.dumps({"type": "message", "content": content, "content_format": "text/md"}).encode()
    req = urllib.request.Request(
        api_url,
        data=payload,
        method="POST",
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ClickUp delivery failed: HTTP {e.code} — {e.read().decode(errors='replace')}\n")
        raise


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: report.py <stale_branches.json> [deletions.json]")

    with open(sys.argv[1]) as f:
        branches = json.load(f)
    deletions = None
    if len(sys.argv) == 3:
        with open(sys.argv[2]) as f:
            deletions = json.load(f)

    content = format_report(branches, deletions)
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        print("--- dry_run: true — printing report instead of posting to ClickUp ---")
        print(content)
        return

    token = os.environ.get("CLICKUP_FRIDAY_TOKEN")
    if not token:
        sys.exit("CLICKUP_FRIDAY_TOKEN not set and DRY_RUN is not true — cannot deliver.")

    channel_id = os.environ.get("CLICKUP_CHANNEL_ID") or DEFAULT_CHANNEL_ID
    # Printed first so the run summary keeps the report, deletions included, even if
    # the post below fails.
    print(content)
    status = post(content, token, channel_id)
    print(f"Posted to channel {channel_id} (HTTP {status}).")


if __name__ == "__main__":
    main()
