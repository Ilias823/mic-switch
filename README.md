# Mic Switch for DaVinci Resolve

Turns each interview mic off while the other person talks, so you hear only the speaker's own lav and none of the bleed.

Mic Switch works on a **copy** of your timeline. On that copy, every clip on the mic tracks is cut at the speaker changes, and the pieces where a mic should be off are **disabled**, not deleted. To bring one back, select it and press **D**.

## Install
1. Download the latest `MicSwitch-<version>.pkg` from [Releases](https://github.com/Ilias823/mic-switch/releases).
2. Double-click the installer.
3. The installer is not signed with an Apple developer certificate yet, so macOS blocks it the first time. Open **System Settings > Privacy & Security** and click **Open Anyway**. (On macOS 14 and earlier you can also right-click the file and choose Open.)
4. Restart DaVinci Resolve.

Later versions: press **Check for Updates** in the Mic Switch window.

## Use
1. Open the interview timeline. Each person needs their own audio track (one lav each), recorded as `.wav`.
2. Choose **Workspace → Scripts → Mic Switch**.
3. Tick the mic tracks. Tracks named lav, mic, host or guest are ticked for you.
4. Optional: press **Analyze** to see how clearly the mics can be told apart (two humps, one on each side of 0, is good).
5. Press **Create Timeline**. The bar at the bottom shows what it is doing. When it's done you get "<name> (mic switch)", and it opens.

A 39-minute two-person interview takes about 3 s to analyze and about 40 s to build.

## How it decides
Each lav also picks up the other person, just quieter. Mic Switch measures every mic against **its own noise floor** (signal-to-noise ratio). Because of that, lavs recorded at very different gains still compete fairly, and nothing needs tuning per interview. The noise floor is found the same way as in the Automixer: the quietest moment of a smoothed level within a few seconds.

Frame by frame, the mic that is clearest above its own noise, by at least *Switch sensitivity* dB, is the speaker. Otherwise the previous speaker is kept. After that:
- turns shorter than *Shortest turn* are merged into the turn around them,
- each mic opens *Open early* frames before its turn and stays open *Stay open after* frames after it,
- off gaps shorter than *Shortest off gap* stay on,
- each switch gets a short *Fade*.

It works with more than two mics. The settings are remembered between sessions.

## What the copy keeps
Every piece gets its original clip's volume, clip plugins, EQ, clip colour and channel mapping (via Resolve's Paste Attributes). Fades at the start and end of the original clips are kept too. Volume keyframes inside a clip are not carried over to the new pieces. Clips that were already disabled are left alone. Video and the other tracks are untouched.

## Needs
- macOS 13 or later (Apple silicon or Intel)
- DaVinci Resolve 21.1.2 or later, which added the trim and Paste Attributes scripting calls this uses
- `.wav` audio: 16, 24 or 32-bit, or float (BWF and RF64 included)

If it says macOS blocked an audio file, go to System Settings → Privacy & Security → Files and Folders (or Full Disk Access) and allow DaVinci Resolve to use that drive.

## Build
```
make test                      # analyzer + its test
sh installer/build_pkg.sh      # dist/MicSwitch-<version>.pkg
./install-dev.sh               # development copy in your own Scripts menu
```

| Path | What |
|---|---|
| `analyzer/mic_switch_analyze.cpp` | Reads the WAVs, decides who is speaking, writes the on/off ranges. No dependencies. |
| `resolve/Mic Switch.py` | The Scripts-menu window. Reads the clip layout, runs the analyzer, builds the copy, then checks every piece. |
| `tests/test_analyzer.py` | Made-up interview: two lavs 20 dB apart in gain, with bleed, in float, 24-bit and 16-bit files. |
| `installer/` | `.pkg` installer, plus `uninstall.sh`. |

Uninstall: `sudo "/Library/Application Support/MicSwitch/uninstall.sh"`

## Releasing
1. Raise the version in `VERSION`.
2. Commit and push.
3. Run `installer/release.sh "What is new"`. It builds the installer and publishes the GitHub release that Check for Updates finds.

## Mic Switch Pro
Want the **cameras** to follow the speaker too? [Mic Switch Pro](https://ko-fi.com/s/79a9ccd6d9) adds the following:
- each person's camera is shown while they talk,
- the wide camera is shown when people talk at the same time,
- no shot is shorter than a set length.

It works the same way, on a copy of your timeline. It's sold in the Ko-fi shop, and buying it supports this free version. In Mic Switch, press **Get Mic Switch Pro** to open the shop.

## Support and licence
Mic Switch is free and open source, licensed under the [GNU General Public License v3.0](LICENSE). If it saves you time, you can [support it on Ko-fi](https://ko-fi.com/ilias823).
