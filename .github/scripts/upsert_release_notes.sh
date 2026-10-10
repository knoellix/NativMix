#!/usr/bin/env bash
# Upsert Changelog (once) + one channel section onto the GitHub Release for TAG.
# Usage: upsert_release_notes.sh TAG VERSION CHANNEL
# CHANNEL: aur | windows | flatpak
# Requires: python3, GITHUB_TOKEN or GH_TOKEN (no gh CLI — works in Flatpak container)
set -euo pipefail

TAG="${1:?tag}"
VERSION="${2:?version}"
CHANNEL="${3:?channel}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
python3 "${ROOT}/.github/scripts/release_notes.py" apply "$TAG" "$VERSION" "$CHANNEL"
