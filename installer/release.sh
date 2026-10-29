#!/bin/sh
# Publishes a new version on GitHub, where Mic Switch's "Check for Updates" button finds it.
#
#   1. raise the version in VERSION (e.g. 0.1.0 -> 0.2.0)
#   2. commit and push your changes
#   3. installer/release.sh "What is new in this version"
#
# It builds dist/MicSwitch-<version>.pkg and creates the release v<version> with the installer
# attached. Needs the GitHub CLI (gh) signed in to the repository's account.
set -e
cd "$(dirname "$0")/.."
repo="Ilias823/mic-switch"   # keep in step with UPDATE_REPO in resolve/Mic Switch.py
version="$(cat VERSION)"
notes="${1:?usage: installer/release.sh \"what is new in this version\"}"

if gh release view "v$version" --repo "$repo" >/dev/null 2>&1; then
    echo "v$version is already released: raise the version in VERSION first." >&2
    exit 1
fi
sh installer/build_pkg.sh
gh release create "v$version" "dist/MicSwitch-$version.pkg" --repo "$repo" \
    --title "Mic Switch $version" --notes "$notes"
echo "released v$version"
