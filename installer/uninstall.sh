#!/bin/sh
# Removes Mic Switch. Run in Terminal:  sudo "/Library/Application Support/MicSwitch/uninstall.sh"
# Timelines it made are left alone.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi
rm -f "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/Mic Switch.py"
rm -rf "/Library/Application Support/MicSwitch"
pkgutil --forget org.micswitch.resolve >/dev/null 2>&1
echo "Mic Switch removed. Restart DaVinci Resolve."
