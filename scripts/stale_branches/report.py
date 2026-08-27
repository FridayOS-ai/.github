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

Usage: python report.py stale_branches.json
Env: CLICKUP_FRIDAY_TOKEN (required unless DRY_RUN=true), DRY_RUN=true|false,
     CLICKUP_CHANNEL_ID (optional, defaults to FridayOS-Dev)
"""
import json
import os
import sys
import urllib.error
import urllib.request

WORKSPACE_ID = "9018051827"  # the one FridayOS ClickUp workspace -- not configurable
DEFAULT_CHANNEL_ID = "8cr937k-450598"  # FridayOS-Dev


def format_report(branches):
    if not branches:
        return "✅ No stale branches this week.\n\n*Sent from FridayOS*"

    escalate = [b for b in branches if b["tier"] == "escalate"]
    stale = [b for b in branches if b["tier"] == "stale"]

    lines = ["**Weekly stale-branch report**", ""]

    def render_section(title, rows):
        out = [f"**{title}**", ""]
        for b in rows:
            pr_part = f" — [PR]({b['pr_url']})" if b.get("pr_url") else ""
            author = f"**{b['created_by']}**" if b["tier"] == "escalate" else b["created_by"]
            out.append(
                f"- `{b['branch']}`{pr_part} — created by {author}, "
                f"last activity {b['last_activity']} by {b['last_activity_by']} "
                f"({b['age_days']}d idle)"
            )
        out.append("")
        return out

    if escalate:
        lines += render_section("🔴 Escalate (≥60d)", escalate)
    if stale:
        lines += render_section("🟡 Stale (≥30d)", stale)

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
    if len(sys.argv) != 2:
        sys.exit("usage: report.py <stale_branches.json>")

    with open(sys.argv[1]) as f:
        branches = json.load(f)

    content = format_report(branches)
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        print("--- dry_run: true — printing report instead of posting to ClickUp ---")
        print(content)
        return

    token = os.environ.get("CLICKUP_FRIDAY_TOKEN")
    if not token:
        sys.exit("CLICKUP_FRIDAY_TOKEN not set and DRY_RUN is not true — cannot deliver.")

    channel_id = os.environ.get("CLICKUP_CHANNEL_ID") or DEFAULT_CHANNEL_ID
    status = post(content, token, channel_id)
    print(f"Posted to channel {channel_id} (HTTP {status}).")


if __name__ == "__main__":
    main()
