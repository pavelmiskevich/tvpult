#!/bin/sh
# Prints the CHANGELOG.md section of one version (without its heading): the text of the GitHub release.
# Fails when there is no such section, so a release can't go out without its notes.
# Usage: scripts/release-notes.sh 0.1.0
set -eu

version=$1
awk -v head="## [$version]" '
    index($0, head) == 1 { found = 1; next }
    found && /^## \[/ { exit }
    found { print }
    END { if (!found) exit 1 }
' CHANGELOG.md || { echo "::error::CHANGELOG.md has no section \"## [$version]\"" >&2; exit 1; }
