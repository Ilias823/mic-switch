#!/bin/sh
# Builds the double-click installer: dist/MicSwitch-<version>.pkg
#
# It installs, for all users:
#   /Library/Application Support/MicSwitch/mic_switch_analyze     the audio analyzer (universal)
#   /Library/Application Support/MicSwitch/uninstall.sh
#   .../DaVinci Resolve/Fusion/Scripts/Utility/Mic Switch.py        the Workspace > Scripts entry
# Nothing else is needed on the target Mac: the script runs in Resolve's own Python.
set -e
cd "$(dirname "$0")"
here="$(pwd)"
project="$(cd .. && pwd)"
version="$(cat "$project/VERSION")"
support="/Library/Application Support/MicSwitch"
scripts_dir="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"

make -C "$project" clean test

stage="$here/build"
rm -rf "${stage:?}"
root="$stage/root"
mkdir -p "$root$support" "$root$scripts_dir"
cp "$project/build/mic_switch_analyze" "$root$support/"
cp "$here/uninstall.sh" "$root$support/uninstall.sh"
chmod 755 "$root$support/mic_switch_analyze" "$root$support/uninstall.sh"
sed -e "s|@ANALYZER@|$support/mic_switch_analyze|" -e "s|@VERSION@|$version|" \
    "$project/resolve/Mic Switch.py" > "$root$scripts_dir/Mic Switch.py"
# Fail here rather than on the user's Mac if the script does not even compile.
resolve_python="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolvePython"
check_python="$( [ -x "$resolve_python" ] && echo "$resolve_python" || command -v python3 )"
"$check_python" -I -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$root$scripts_dir/Mic Switch.py"

xattr -cr "$root"
export COPYFILE_DISABLE=1

pkgbuild --root "$root" --scripts "$here/scripts" --identifier org.micswitch.resolve --version "$version" \
    --install-location / --ownership recommended "$stage/MicSwitch.pkg"

# macOS tags every new file with a com.apple.provenance attribute that cannot be removed, and
# pkgbuild stores it as "._" files, which would be installed too (one would sit next to the
# script in Resolve's Scripts folder). Repack the payload with cpio, which leaves them out.
expanded="$stage/expanded"
pkgutil --expand "$stage/MicSwitch.pkg" "$expanded"
(cd "$root" && find . | sort | cpio -o --format odc -R 0:0 2>/dev/null | gzip -9 -c) > "$expanded/Payload"
lsbom "$expanded/Bom" | grep -v '/\._' > "$stage/bom.txt"
rm -f "$expanded/Bom"
mkbom -i "$stage/bom.txt" "$expanded/Bom"
files="$(cd "$root" && find . | wc -l | tr -d ' ')"
sed -i '' -e "s/numberOfFiles=\"[0-9]*\"/numberOfFiles=\"$files\"/" "$expanded/PackageInfo"
rm "$stage/MicSwitch.pkg"
pkgutil --flatten "$expanded" "$stage/MicSwitch.pkg"
if gunzip -dc "$expanded/Payload" | cpio -it 2>/dev/null | grep -q '/\._'; then
    echo "the payload still has ._ files" >&2
    exit 1
fi
sed -e "s|@VERSION@|$version|g" "$here/distribution.xml" > "$stage/distribution.xml"
mkdir -p "$project/dist"
productbuild --distribution "$stage/distribution.xml" --resources "$here/resources" --package-path "$stage" \
    "$project/dist/MicSwitch-$version.pkg"
echo "built: $project/dist/MicSwitch-$version.pkg"
