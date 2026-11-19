#!/usr/bin/env bash
# Drive the action's entry point the way a runner does, without a runner:
# install qemu-tool into a scratch directory and run two commands through
# it. Boots nothing. Usage: tests/smoke.sh [qemu-tool-source]
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
for f in out env path state summary; do : > "$scratch/$f"; done

act() {
  env GITHUB_OUTPUT="$scratch/out" GITHUB_ENV="$scratch/env" \
      GITHUB_PATH="$scratch/path" GITHUB_STATE="$scratch/state" \
      GITHUB_STEP_SUMMARY="$scratch/summary" RUNNER_TEMP="$scratch" \
      GITHUB_RUN_ID=1 GITHUB_JOB=smoke \
      "INPUT_WORK-DIR=$scratch/work" "INPUT_SETUP-HOST=false" \
      "INPUT_QEMU-TOOL-SOURCE=${1:-}" "INPUT_OPERATION=$2" "INPUT_ARGS=$3" \
      python3 "$here/qemu_tool_action.py" main
}

act "${1:-}" command "--version"
act "${1:-}" command "run-vm --vm-name smoke --dry-run --no-kvm" > "$scratch/dry"
tail -n 3 "$scratch/dry"
act "${1:-}" command "--version" > "$scratch/again"
grep -q "reusing the install" "$scratch/again" \
  || { echo "FAIL: the second step reinstalled"; exit 1; }
grep -q "^qemu-tool-version<<" "$scratch/out" \
  || { echo "FAIL: no qemu-tool-version output"; exit 1; }

py="$scratch/work/action/venv/bin/python"
for flavor in default mi355x helios; do
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1])
import qemu_tool_action as q; print(q.packaged_fleet(sys.argv[2], sys.argv[3]))' \
    "$here" "$py" "$flavor"
done
echo "smoke OK"
