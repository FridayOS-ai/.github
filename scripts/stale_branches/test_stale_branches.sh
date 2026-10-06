#!/usr/bin/env bash
# End-to-end test of scan.py -> prune.py -> report.py against a throwaway bare
# repo standing in for origin, two weekly runs apart. `gh` is stubbed: the
# open-PR lookup answers from $PR_BRANCHES, and GH_FAIL=1 makes it fail.
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
  for a in "$@"; do [ "${prev:-}" = "--head" ] && head=$a; prev=$a; done
  for p in ${PR_BRANCHES:-}; do
    [ "$head" = "$p" ] && { echo '[{"number":7,"url":"https://example.test/pr/7","author":{"login":"pr-author"}}]'; exit 0; }
  done
  echo '[]'; exit 0
fi
echo "unexpected gh $*" >&2; exit 1
EOF
chmod +x "$T/bin/gh"
export PATH="$T/bin:$PATH" STALE_DAYS=30 ESCALATE_DAYS=60 DELETE_DAYS=70 PROTECTED_GLOBS=main,dev
unset GITHUB_REPOSITORY

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
for spec in plain:100 with-pr:80 moved:75 collides:90 gets-pr:85 races:95 ages-in:65 young:40 merged:120; do
  b=${spec%%:*}
  git checkout -q -b "$b" dev && commit $((NOW - ${spec##*:} * DAY)) "work on $b" && git push -q origin "$b"
done
git checkout -q dev && git merge -q --no-ff merged -m "merge" && git push -q origin dev && git push -q origin dev:main
git tag archive/collides dev~1 && git push -q origin archive/collides

# --- week 1: nothing announced before, so nothing is deleted ----------------
git clone -q "$T/origin.git" "$T/run1" && cd "$T/run1" || exit 1
PR_BRANCHES="with-pr" STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > scan.json
check "week 1 delete tier" "plain races collides gets-pr moved" "$(names '.[] | select(.tier == "delete") | .branch' scan.json)"
check "open PR stays escalate" "escalate" "$(jq -r '.[] | select(.branch == "with-pr") | .tier' scan.json)"
check "merged and protected never listed" "0" "$(jq '[.[] | select(.branch == "merged" or .branch == "dev" or .branch == "main")] | length' scan.json)"
echo null > "$T/none.json"
STALE_BRANCHES_NOW=$NOW PREVIOUS_ANNOUNCEMENT="$T/none.json" python3 "$TOOLS/prune.py" scan.json > "$T/week1.json" 2>/dev/null
check "week 1 deletes nothing" "" "$(names '.deleted[].branch' "$T/week1.json")"
check "week 1 announces the delete tier" "plain races collides gets-pr moved" "$(names '.announced[].branch' "$T/week1.json")"

PR_BRANCHES="with-pr" GH_FAIL=1 STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "failed PR lookup aborts when deletion is on" "1" "$?"
PR_BRANCHES="with-pr" GH_FAIL=1 DELETE_DAYS=0 STALE_BRANCHES_NOW=$NOW python3 "$TOOLS/scan.py" > /dev/null 2>&1
check "failed PR lookup tolerated when report-only" "0" "$?"

check "3 days' notice deletes nothing" "" "$(STALE_BRANCHES_NOW=$((NOW + 3 * DAY)) PREVIOUS_ANNOUNCEMENT="$T/week1.json" \
  python3 "$TOOLS/prune.py" scan.json 2>/dev/null | jq -r '[.deleted[].branch] | join(" ")')"
check "3 days' notice touched no ref" "$(git rev-parse origin/plain)" "$(remote_ref refs/heads/plain)"

# --- between the runs -------------------------------------------------------
cd "$T/work" || exit 1
git checkout -q moved && commit $((NOW - 74 * DAY)) "old-dated rework" && git push -q origin moved

# --- week 2 -----------------------------------------------------------------
git clone -q "$T/origin.git" "$T/run2" && cd "$T/run2" || exit 1
PR_BRANCHES="with-pr gets-pr" STALE_BRANCHES_NOW=$W2 python3 "$TOOLS/scan.py" > scan.json
(cd "$T/work" && git checkout -q races && commit $((W2 - DAY)) "late push" && git push -q origin races)
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
check "mid-run push refused" "races pushed to during this run, left alone" "$(jq -r '.failed[] | "\(.branch) \(.error)"' "$T/week2.json")"
check "mid-run push kept its new tip" "$(git -C "$T/work" rev-parse races)" "$(remote_ref refs/heads/races)"
check "refused deletion created no tag (atomic)" "none" "$(remote_ref refs/tags/archive/races)"
check "week 2 announces the rest" "races moved ages-in" "$(names '.announced[].branch' "$T/week2.json")"

# --- report -----------------------------------------------------------------
report=$(GITHUB_REPOSITORY=org/repo DRY_RUN=true python3 "$TOOLS/report.py" scan.json "$T/week2.json")
check "report: deleted row shows its tag" "1" "$(grep -c '`plain`.*→ `archive/plain`' <<< "$report")"
check "report: refused row shows no tag" "0" "$(grep '`races`' <<< "$report" | grep -c 'archive/')"
check "report: next-run list leaves out the refused branch" "moved ages-in" \
  "$(sed -n '/Deleted on the next weekly run/,/To keep one/p' <<< "$report" | grep -o '^- `[^`]*`' | sed 's/^- //; s/`//g' | paste -sd ' ')"

# --- restore, with the command the report prints ------------------------------
git clone -q "$T/origin.git" "$T/restorer" && cd "$T/restorer" || exit 1
git fetch -q origin tag archive/plain && git push -q origin archive/plain:refs/heads/plain
check "restore brings the branch back at its tip" "$(remote_ref refs/tags/archive/plain)" "$(remote_ref refs/heads/plain)"

echo
[ "$failures" -eq 0 ] && echo "all passed" || { echo "$failures failed"; exit 1; }
