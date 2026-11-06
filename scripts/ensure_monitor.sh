#!/usr/bin/env bash
# Start a new monitor run unless one is already covering the schedule.
#   ensure_monitor.sh watchdog   skip if a run is in progress OR waiting
#   ensure_monitor.sh handover   skip only if a run is already waiting (we are the one in progress)
# Needs GH_TOKEN (and GH_REPO or a checkout) and the "actions: write" permission.
set -euo pipefail

mode="${1:-watchdog}"
workflow="monitor.yml"
busy=0

count() {
  gh run list --workflow "$workflow" --status "$1" --limit 30 --json databaseId --jq 'length'
}

statuses="queued pending waiting"
[ "$mode" = "watchdog" ] && statuses="$statuses in_progress"

for s in $statuses; do
  n=$(count "$s")
  busy=$((busy + n))
done

if [ "$busy" -gt 0 ]; then
  echo "Monitor already covered ($busy run(s) active or waiting) - nothing to start."
else
  gh workflow run "$workflow"
  echo "Started a new monitor run."
fi
