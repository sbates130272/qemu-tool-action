#!/usr/bin/env bash
# What the release workflow decides, without a release: which tags it
# accepts, which floating tags a release may move, and which part of the
# changelog becomes the release notes.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
rt="$here/scripts/release-tags.sh"
fail() { echo "FAIL: $*"; exit 1; }

[ "$("$rt" parse v2.0.0 | tr '\n' ' ')" = "VERSION=2.0.0 MAJOR=v2 MINOR=v2.0 " ] \
  || fail "parse v2.0.0"
[ "$("$rt" parse v10.2.3 | tr '\n' ' ')" = "VERSION=10.2.3 MAJOR=v10 MINOR=v10.2 " ] \
  || fail "parse v10.2.3"
for bad in v2.0.0-rc1 v2.0 v02.0.0 v2.0.0.1 vx.y.z 2.0.0 v2; do
  if "$rt" parse "$bad" >/dev/null; then fail "accepted $bad"; fi
done

tags=$'v1.0.0\nv1\nv1.0\nv2.0.0\nv2.0.1\nv2.1.0\nv2\nv2.0\nv2.1.0-rc1\nv10.0.0'
newest() { printf '%s\n' "$tags" | "$rt" newest "$1"; }
# A patch on an older line must not move the major alias back...
[ "$(newest v2.)" = v2.1.0 ]   || fail "newest v2. is $(newest v2.)"
# ...but does move its own minor alias.
[ "$(newest v2.0.)" = v2.0.1 ] || fail "newest v2.0. is $(newest v2.0.)"
# v1. must not match v10.
[ "$(newest v1.)" = v1.0.0 ]   || fail "newest v1. is $(newest v1.)"
[ "$(newest v)" = v10.0.0 ]    || fail "newest overall is $(newest v)"
[ -z "$(newest v3.)" ]         || fail "newest v3. is not empty"

# The notes are the section, without its heading and without the link list.
notes=$("$rt" notes 2.0.0 "$here/CHANGELOG.md")
grep -q '^### ' <<<"$notes"              || fail "notes carry no subsection"
if grep -q '^## \[' <<<"$notes"; then fail "notes carry a version heading"; fi
if grep -q '^\[2.0.0\]: ' <<<"$notes"; then fail "notes carry the link list"; fi
if grep -qi 'initial release' <<<"$notes"; then fail "notes ran into 1.0.0"; fi
if "$rt" notes 9.9.9 "$here/CHANGELOG.md" >/dev/null; then
  fail "notes for a version the changelog does not have"
fi

echo "release logic OK"
