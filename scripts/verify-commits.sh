#!/bin/sh
# Per-commit isolation check.
#
# Every commit must build and pass its own tests on its own, not only at the
# tip of the branch. A commit that depends on a later one to compile makes
# bisect useless and makes the history a lie. Each commit in the range is
# checked out into a detached worktree and the full verification suite is run
# there, so nothing in the working tree can mask a missing file.
#
# Usage: scripts/verify-commits.sh [base]      default base is main
#
# See docs/DESIGN.md section 6.8 for the failure that made this mandatory.

set -eu

base="${1:-main}"
root="$(git rev-parse --show-toplevel)"
scratch="${TMPDIR:-/tmp}/coroner-verify-$$"
mkdir -p "$scratch"

cleanup() {
  for wt in "$scratch"/*; do
    [ -d "$wt" ] && git -C "$root" worktree remove --force "$wt" >/dev/null 2>&1 || true
  done
  rm -rf "$scratch"
}
trap cleanup EXIT INT TERM

commits="$(git -C "$root" rev-list --reverse "$base"..HEAD)"
if [ -z "$commits" ]; then
  echo "verify-commits: no commits between $base and HEAD" >&2
  exit 0
fi

failed=0
for sha in $commits; do
  short="$(git -C "$root" rev-parse --short "$sha")"
  subject="$(git -C "$root" log -1 --format=%s "$sha")"
  wt="$scratch/$short"
  git -C "$root" worktree add --detach "$wt" "$sha" >/dev/null 2>&1

  # The brain's virtualenv is per worktree; sync it fresh so a dependency
  # added in a later commit cannot leak backwards.
  if (cd "$wt" && make brain-sync >/dev/null 2>&1 && make verify >"$wt/verify.log" 2>&1); then
    printf 'ok    %s %s\n' "$short" "$subject"
  else
    printf 'FAIL  %s %s\n' "$short" "$subject"
    sed 's/^/      /' "$wt/verify.log" | tail -40
    failed=1
  fi
  git -C "$root" worktree remove --force "$wt" >/dev/null 2>&1 || true
done

exit "$failed"
