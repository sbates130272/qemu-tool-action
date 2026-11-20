#!/usr/bin/env bash
# Drive the action's entry point the way a runner does, without a runner:
# install qemu-tool into a scratch directory and run commands through it,
# then check the two things the action assumes about qemu-tool that only a
# real install can show. Boots nothing.
#
#   tests/smoke.sh                 qemu-tool from PyPI
#   tests/smoke.sh SOURCE_DIR      from the qemu/ directory of a checkout
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
for f in out env path state summary; do : > "$scratch/$f"; done
source_dir=${1:-}

act() {
  env GITHUB_OUTPUT="$scratch/out" GITHUB_ENV="$scratch/env" \
      GITHUB_PATH="$scratch/path" GITHUB_STATE="$scratch/state" \
      GITHUB_STEP_SUMMARY="$scratch/summary" RUNNER_TEMP="$scratch" \
      GITHUB_RUN_ID=1 GITHUB_JOB=smoke \
      "INPUT_WORK-DIR=$scratch/work" "INPUT_SETUP-HOST=false" \
      "INPUT_QEMU-TOOL-SOURCE=$source_dir" "INPUT_OPERATION=$1" \
      "INPUT_ARGS=$2" \
      python3 "$here/qemu_tool_action.py" main
}
fail() { echo "FAIL: $*"; exit 1; }

act command "--version"
act command "run-vm --vm-name smoke --dry-run --no-kvm" > "$scratch/dry"
tail -n 3 "$scratch/dry"
act command "--version" > "$scratch/again"
grep -q "reusing the install" "$scratch/again" || fail "the second step reinstalled"
grep -q "^qemu-tool-version<<" "$scratch/out" || fail "no qemu-tool-version output"

tool="$scratch/work/action/.tool/bin/qemu-tool"
py="$scratch/work/action/.tool/venv/bin/python"
version=$("$tool" --version)

# Every flavor this qemu-tool ships: its fleet takes the action's overlay,
# renders, and yields an inventory the action can read.
python3 "$here/tests/render_flavors.py" "$tool" "$py" "$scratch/render" \
  "$here/examples/fleet-minimal.xml"

# What the stack's containers do with the tree the action places for them.
# These are the lines gen-compose renders into each qemu service, run here
# against that tree; the version they produce has to be the runner's.
python3 - "$here" "$py" "$scratch/stack" <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import qemu_tool_action as qta
print("placed", qta.container_source(sys.argv[2], Path(sys.argv[3]))[0])
EOF
[ -f "$scratch/stack/qemu/pyproject.toml" ] || fail "no source tree was placed"
cp -r "$scratch/stack/qemu" "$scratch/qemu-tool-build"
rm -rf "$scratch/qemu-tool-build/build" "$scratch"/qemu-tool-build/*.egg-info
python3 -m venv "$scratch/qemu-tool-venv"
"$scratch/qemu-tool-venv/bin/pip" install -q setuptools wheel
"$scratch/qemu-tool-venv/bin/pip" install -q --no-build-isolation -e "$scratch/qemu-tool-build"
inside=$(cd / && "$scratch/qemu-tool-venv/bin/qemu-tool" --version)
[ "$inside" = "$version" ] || fail "containers would run '$inside', the runner has '$version'"
(cd / && "$scratch/qemu-tool-venv/bin/qemu-tool" run-vm --vm-name smoke --dry-run --no-kvm) > /dev/null \
  || fail "the placed tree cannot run-vm"
echo "containers would run: $inside"

echo "smoke OK"
