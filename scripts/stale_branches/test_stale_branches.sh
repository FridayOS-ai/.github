#!/usr/bin/env bash
# End-to-end test of scan.py -> prune.py -> report.py against a throwaway bare
# repo standing in for origin, two weekly runs apart. `gh` is stubbed: the
# open-PR listing answers from $PR_BRANCHES (PR heads) and $PR_BASES (PR bases),
# GH_FAIL=1 makes it fail, GH_GARBAGE=1 makes it print non-JSON, and any
# `gh api` call fails.
# The previous run's announcement is passed as a file (PREVIOUS_ANNOUNCEMENT),
# so the artifact lookup itself is not exercised here.
#
# Usage: bash scripts/stale_branches/test_stale_branches.sh   (needs git, jq, python3)
set -uo pipefail

TOOLS=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
DAY=86400
NOW=1791169200  # 2026-10-05T03:00:00Z, a Monday run
W2=$((NOW + 7 * DAY))
failures=0

check() { # check <description> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected [$2], got [$3]"; failures=$((failures + 1)); fi
}

mkdir -p "$T/bin"
cat > "$T/bin/gh" <<'EOF'
#!/usr/bin/env bash
if [ "$1 $2" = "pr list" ]; then
  [ "${GH_FAIL:-}" = 1 ] && { echo "HTTP 403" >&2; exit 1; }
  [ "${GH_GARBAGE:-}" = 1 ] && { echo "A new release of gh is available"; exit 0; }
  sep= && printf '['
  for p in ${PR_BRANCHES:-}; do
    printf '%s{"url":"https://example.test/pr/7","author":{"login":"pr-author"},"headRefName":"%s","baseRefName":"dev","isCrossRepository":false}' "$sep" "$p"; sep=,
  done
  for b in ${PR_BASES:-}; do
    printf '%s{"url":"https://example.test/pr/8","author":{"login":"stacker"},"headRefName":"onto-%s","baseRefName":"%s","isCrossRepository":false}' "$sep" "$b" "$b"; sep=,
  done
  echo ']'; exit 0
fi
echo "unexpected gh $*" >&2; exit 1
EOF
chmod +x "$T/bin/gh"
export PATH="$T/bin:$PATH" STALE_DAYS=30 ESCALATE_DAYS=60 DELETE_DAYS=70 PROTECTED_GLOBS=main,dev KEEP_GLOBS='backup/*' PR_BASES=stack-base
unset GITHUB_REPOSITORY GITHUB_EVENT_NAME

commit() { GIT_AUTHOR_DATE="@$1" GIT_COMMITTER_DATE="@$1" git commit -q --allow-empty -m "$2"; }
names() { jq -r "[$1] | join(\" \")" "$2"; }
remote_ref() { git --git-dir="$T/origin.git" rev-parse --verify -q "$1" || echo none; }

# --- origin: one branch per case, aged relative to NOW ---------------------
git init -q --bare -b dev "$T/origin.git"
git clone -q "$T/origin.git" "$T/work" 2>/dev/null
cd "$T/work" || exit 1
git config user.name Tester && git config user.email tester@example.test
git checkout -q -b dev && commit $((NOW - 200 * DAY)) root && git push -q origin dev
#  plain      100d, deleted in week 2
#  with-pr     80d, open PR throughout: never in the delete tier
#  moved       75d, gets an old-dated commit between the runs: spared
#  collides    90d, archive/collides already exists elsewhere: suffixed tag
#  gets-pr     85d, PR opened between the runs: spared
#  races       95d, pushed between week 2's scan and its prune: lease refuses
#  ages-in     65d, escalate in week 1, announced (not deleted) in week 2
#  young       40d, stale
#  merged     120d, merged into dev: never listed
#  backup/old 110d, on the keep list: reported as kept, never announced
#  kept-later  88d, announced in week 1, added to the keep list before week 2: spared
#  stack-base  92d, the base of an open PR: deleting it would close that PR, never announced
for spec in plain:100 with-pr:80 moved:75 collides:90 gets-pr:85 races:95 ages-in:65 young:40 merged:120 backup/old:110 kept-later:88 stack-base:92; do
  b=${spec%%:*}
  git checkout -q -b "$b" dev && commit $((NOW - ${spec##*:} * DAY)) "work on $b" && git push -q origin "$b"
done
git checkout -q dev && git merge -q --no-ff merged -m "merge" && git push -q origin dev && git push -q origin dev:main
git tag archive/collides dev~1 && git push -q origin archive/collides

# --- week 1: nothing announced before, so nothing is deleted ----------------
git clone -q "$T/origin.git" "$T/run1" && cd "$T/run1" || exit 1
PR_BRANCHES="with-pr" STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > scan.json
check "week 1 delete tier" "plain races collides kept-later gets-pr moved" "$(names '.[] | select(.tier == "delete") | .branch' scan.json)"
check "open PR stays escalate" "escalate" "$(jq -r '.[] | select(.branch == "with-pr") | .tier' scan.json)"
check "kept branch stays escalate, marked kept" "escalate true" "$(jq -r '.[] | select(.branch == "backup/old") | "\(.tier) \(.kept)"' scan.json)"
check "base of an open PR stays escalate" "escalate https://example.test/pr/8" "$(jq -r '.[] | select(.branch == "stack-base") | "\(.tier) \(.base_of_pr)"' scan.json)"
check "negative delete days is off" "0" "$(DELETE_DAYS=-1 PR_BRANCHES="with-pr" STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" | jq '[.[] | select(.tier == "delete")] | length')"
check "merged and protected never listed" "0" "$(jq '[.[] | select(.branch == "merged" or .branch == "dev" or .branch == "main")] | length' scan.json)"
echo null > "$T/none.json"
STALE_BRANCHES_NOW=$NOW PREVIOUS_ANNOUNCEMENT="$T/none.json" python3 "$TOOLS/prune.py" scan.json > "$T/week1.json" 2>/dev/null
check "week 1 deletes nothing" "" "$(names '.deleted[].branch' "$T/week1.json")"
check "week 1 announces the delete tier" "plain races collides kept-later gets-pr moved" "$(names '.announced[].branch' "$T/week1.json")"

PR_BRANCHES="with-pr" GH_FAIL=1 STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "failed PR lookup aborts when deletion is on" "1" "$?"
PR_BRANCHES="with-pr" GH_FAIL=1 DELETE_DAYS=0 STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "failed PR lookup tolerated when report-only" "0" "$?"
PR_BRANCHES="with-pr" GH_GARBAGE=1 STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "unreadable PR listing aborts when deletion is on" "1" "$?"
GITHUB_REPOSITORY=org/repo PR_BRANCHES="with-pr" STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "failed default-branch lookup aborts when deletion is on" "1" "$?"
GITHUB_REPOSITORY=org/repo PR_BRANCHES="with-pr" DELETE_DAYS=0 STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "failed default-branch lookup tolerated when report-only" "0" "$?"

check "3 days' notice deletes nothing" "" "$(STALE_BRANCHES_NOW=$((NOW + 3 * DAY)) PREVIOUS_ANNOUNCEMENT="$T/week1.json" \
  python3 "$TOOLS/prune.py" scan.json 2>/dev/null | jq -r '[.deleted[].branch] | join(" ")')"
check "3 days' notice touched no ref" "$(git rev-parse origin/plain)" "$(remote_ref refs/heads/plain)"

# --- between the runs -------------------------------------------------------
cd "$T/work" || exit 1
git checkout -q moved && commit $((NOW - 74 * DAY)) "old-dated rework" && git push -q origin moved

# --- week 2 -----------------------------------------------------------------
git clone -q "$T/origin.git" "$T/run2" && cd "$T/run2" || exit 1
KEEP_GLOBS=$'backup/*\nkept-later' PR_BRANCHES="with-pr gets-pr" STALE_BRANCHES_NOW=$W2 python3 "$TOOLS/scan.py" > scan.json  # one pattern per line
(cd "$T/work" && git checkout -q races && commit $((W2 - DAY)) "late push" && git push -q origin races)
check "manual run deletes nothing" "" "$(GITHUB_EVENT_NAME=workflow_dispatch STALE_BRANCHES_NOW=$W2 PREVIOUS_ANNOUNCEMENT="$T/week1.json" \
  python3 "$TOOLS/prune.py" scan.json 2>/dev/null | jq -r '[.deleted[].branch] | join(" ")')"
check "manual run touched no ref" "$(git rev-parse origin/plain)" "$(remote_ref refs/heads/plain)"
PR_BRANCHES="with-pr gets-pr" STALE_BRANCHES_NOW=$W2 PREVIOUS_ANNOUNCEMENT="$T/week1.json" \
  python3 "$TOOLS/prune.py" scan.json > "$T/week2.json" 2>/dev/null

check "week 2 deletes the unchanged announced branches" "plain collides" "$(names '.deleted[].branch' "$T/week2.json")"
check "plain is gone" "none" "$(remote_ref refs/heads/plain)"
check "archive tag holds the announced tip" "$(jq -r '.announced[] | select(.branch == "plain") | .sha' "$T/week1.json")" "$(remote_ref refs/tags/archive/plain)"
check "colliding name gets a suffixed tag" "archive/collides-$(jq -r '.announced[] | select(.branch == "collides") | .sha[0:7]' "$T/week1.json")" \
  "$(jq -r '.deleted[] | select(.branch == "collides") | .archive_tag' "$T/week2.json")"
check "existing archive tag untouched" "$(git -C "$T/work" rev-parse dev~1)" "$(remote_ref refs/tags/archive/collides)"
check "moved branch spared" "$(git -C "$T/work" rev-parse moved)" "$(remote_ref refs/heads/moved)"
check "branch with a new PR spared" "$(git -C "$T/work" rev-parse gets-pr)" "$(remote_ref refs/heads/gets-pr)"
check "branch added to the keep list spared" "$(git -C "$T/work" rev-parse kept-later)" "$(remote_ref refs/heads/kept-later)"
check "kept branch never archived" "none" "$(remote_ref refs/tags/archive/backup/old)"
check "mid-run push refused" "races pushed to during this run, left alone" "$(jq -r '.failed[] | "\(.branch) \(.error)"' "$T/week2.json")"
check "mid-run push kept its new tip" "$(git -C "$T/work" rev-parse races)" "$(remote_ref refs/heads/races)"
check "refused deletion created no tag (atomic)" "none" "$(remote_ref refs/tags/archive/races)"
check "week 2 announces the rest" "races moved ages-in" "$(names '.announced[].branch' "$T/week2.json")"

# --- report -----------------------------------------------------------------
report=$(GITHUB_REPOSITORY=org/repo DRY_RUN=true python3 "$TOOLS/report.py" scan.json "$T/week2.json")
check "report: deleted row shows its tag" "1" "$(grep -c '`plain`.*→ `archive/plain`' <<< "$report")"
check "report: kept branches listed together under Kept" "backup/old kept-later" \
  "$(sed -n '/Kept, never deleted/,/Remove a pattern/p' <<< "$report" | grep -o '^- `[^`]*`' | sed 's/^- //; s/`//g' | paste -sd ' ')"
check "report: Escalate lists the PR branches and no kept one" "stack-base gets-pr with-pr" \
  "$(awk '/Escalate/{f=1; next} /^\*\*/{f=0} f' <<< "$report" | grep -o '^- `[^`]*`' | sed 's/^- //; s/`//g' | paste -sd ' ')"
check "report: failed rows say they are retried" "1" "$(grep -c 'Not deleted this run.*retried on the next weekly run' <<< "$report")"
check "report: restore names the row's own tag" "1" "$(grep -c 'using the tag on its row' <<< "$report")"
check "report: refused row shows no tag" "0" "$(grep '`races`' <<< "$report" | grep -c 'archive/')"
check "report: next-run list leaves out the refused branch" "moved ages-in" \
  "$(sed -n '/Deleted on the next weekly run/,/To keep one/p' <<< "$report" | grep -o '^- `[^`]*`' | sed 's/^- //; s/`//g' | paste -sd ' ')"

# --- restore, with the command the report prints ------------------------------
git clone -q "$T/origin.git" "$T/restorer" && cd "$T/restorer" || exit 1
git fetch -q origin tag archive/plain && git push -q origin archive/plain:refs/heads/plain
check "restore brings the branch back at its tip" "$(remote_ref refs/tags/archive/plain)" "$(remote_ref refs/heads/plain)"

echo
[ "$failures" -eq 0 ] && echo "all passed" || { echo "$failures failed"; exit 1; }
