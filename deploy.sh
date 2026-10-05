#!/usr/bin/env bash
# deploy.sh — keep the live checkout on origin/main. run by lifeplanner-deploy.timer.
# new commits are tested in a throwaway worktree first; the live tree only
# fast-forwards when they pass. anything off → exit non-zero + ntfy, live tree untouched.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/lifeplanner-deploy.lock"
flock -n 9 || exit 0

py="$PWD/.venv/bin/python"
fail() {
  echo "deploy: $1" >&2
  "$py" -c 'import sys, notify; notify.send("lifeplanner deploy failed", sys.argv[1])' "$1" || true
  exit 1
}

git fetch -q origin main || fail "git fetch failed"
head=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
[ "$head" = "$new" ] && exit 0

[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || fail "live checkout is not on main"
git diff --quiet HEAD || fail "live checkout has uncommitted edits"
git merge-base --is-ancestor HEAD "$new" || fail "live checkout has commits not on origin/main"

tmp=$(mktemp -d)
trap 'git worktree remove --force "$tmp" 2>/dev/null || true; rm -rf "$tmp"' EXIT
git worktree add -q --detach "$tmp" "$new"

git diff --quiet "$head" "$new" -- requirements.txt ||
  "$PWD/.venv/bin/pip" install -q -r "$tmp/requirements.txt" || fail "dependency install failed"

if ! out=$(cd "$tmp" && "$py" -m pytest -q tests 2>&1); then
  fail "tests failed on ${new:0:7}, not deployed: $(tail -n 3 <<<"$out")"
fi

git merge -q --ff-only "$new"
systemctl --user restart lifeplanner.service
sleep 3
systemctl --user is-active -q lifeplanner.service || fail "web app did not come back after ${new:0:7}"
echo "deploy: ${head:0:7} → ${new:0:7}"
