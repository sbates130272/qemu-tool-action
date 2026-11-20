#!/usr/bin/env bash
# The tag and changelog arithmetic of the release workflow, kept out of the
# workflow so that tests/test_release.sh can run it.
#
#   release-tags.sh parse TAG            print VERSION=, MAJOR= and MINOR=
#                                        lines; exit 1 unless TAG is vX.Y.Z
#   release-tags.sh newest PREFIX        read tag names on stdin; print the
#                                        highest vX.Y.Z that starts with
#                                        PREFIX ("v2." or "v2.0."; "v" = any)
#   release-tags.sh notes VERSION FILE   print the "## [VERSION]" section of
#                                        a changelog; exit 1 if it is empty
set -euo pipefail

semver='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

case "${1:-}" in
  parse)
    [[ "${2:-}" =~ $semver ]] || exit 1
    echo "VERSION=${2#v}"
    echo "MAJOR=v${BASH_REMATCH[1]}"
    echo "MINOR=v${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
    ;;
  newest)
    prefix=${2:?}
    while IFS= read -r tag; do
      [[ "$tag" =~ $semver ]] || continue
      [[ "$tag" == "$prefix"* ]] || continue
      echo "$tag"
    done | sort -V | tail -n1
    ;;
  notes)
    awk -v v="${2:?}" '
      /^## \[/ { if (on) exit; on = (index($0, "## [" v "]") == 1); next }
      /^\[[^]]+\]: / { if (on) exit }
      on' "${3:?}" > "${TMPDIR:-/tmp}/release-notes.$$"
    trap 'rm -f "${TMPDIR:-/tmp}/release-notes.$$"' EXIT
    grep -q '[^[:space:]]' "${TMPDIR:-/tmp}/release-notes.$$" || exit 1
    cat "${TMPDIR:-/tmp}/release-notes.$$"
    ;;
  *)
    echo "usage: $0 parse TAG | newest PREFIX | notes VERSION FILE" >&2
    exit 2
    ;;
esac
