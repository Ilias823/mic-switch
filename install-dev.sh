#!/bin/sh
# Development: builds the analyzer and puts the script in your own Scripts menu, using build/.
# The .pkg installer removes this copy when it installs the real one.
set -e
cd "$(dirname "$0")"
make
dir="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
mkdir -p "$dir"
sed -e "s|@ANALYZER@|$PWD/build/mic_switch_analyze|" -e "s|@VERSION@|$(cat VERSION)-dev|" \
    "resolve/Mic Switch.py" > "$dir/Mic Switch.py"
echo "installed: $dir/Mic Switch.py"
