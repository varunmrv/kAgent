#!/usr/bin/env bash
# Persist state.json on an orphan "state" branch (keeps main clean and secret-free).
#   state_branch.sh pull   restore state.json from origin/state (if it exists)
#   state_branch.sh push   publish state.json as the single commit on origin/state
set -euo pipefail

BRANCH="state"
FILE="state.json"

case "${1:-}" in
  pull)
    if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
      git fetch --quiet --depth=1 origin "$BRANCH"
      git show "FETCH_HEAD:$FILE" > "$FILE" || { rm -f "$FILE"; echo "no $FILE on $BRANCH"; }
    else
      echo "no $BRANCH branch yet - starting with fresh state"
    fi
    ;;
  push)
    [ -f "$FILE" ] || { echo "no $FILE to push"; exit 0; }
    export GIT_AUTHOR_NAME="kAgent" GIT_COMMITTER_NAME="kAgent"
    export GIT_AUTHOR_EMAIL="kagent@users.noreply.github.com" GIT_COMMITTER_EMAIL="kagent@users.noreply.github.com"
    blob=$(git hash-object -w "$FILE")
    tree=$(printf '100644 blob %s\t%s\n' "$blob" "$FILE" | git mktree)
    commit=$(git commit-tree "$tree" -m "kAgent state $(date -u +%Y-%m-%dT%H:%M:%SZ)")
    for attempt in 1 2 3; do
      if git push --quiet --force origin "$commit:refs/heads/$BRANCH"; then
        echo "state pushed ($commit)"; exit 0
      fi
      sleep $((attempt * 3))
    done
    echo "state push FAILED" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 pull|push" >&2; exit 2 ;;
esac
