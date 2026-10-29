"""Mic Switch: turns each interview mic off while the other person talks.

Workspace > Scripts > Mic Switch, with the interview timeline open. Pick the mic tracks (one lav
per person), press Analyze to see how well the mics are told apart, then Create Timeline.

The open timeline is never changed. Mic Switch makes a copy, "<name> (mic switch)", where every
clip on the chosen tracks is cut into pieces at the speaker changes. The pieces where that mic
should be off are disabled, not deleted: select one and press D to bring it back.

The audio is measured by a small helper program (mic_switch_analyze) that ships with this script,
so nothing needs to be installed besides DaVinci Resolve. @ANALYZER@ is filled in at install.
"""
import json
import os
import re
import subprocess
import tempfile

ANALYZER = "@ANALYZER@"
VERSION = "@VERSION@"
SETTINGS_FILE = os.path.expanduser("~/Library/Application Support/MicSwitch/settings.json")
UPDATE_REPO = "Ilias823/mic-switch"      # GitHub repository whose releases "Check for Updates" reads
DONATE_URL = "https://ko-fi.com/ilias823"

# (key, label, default, minimum, maximum, tooltip). The keys are the analyzer's "set" names.
SETTINGS = [
    ("focus_db", "Switch sensitivity (dB)", 3, 1, 15,
     "How much louder (above its own noise) one mic must be than the others to take over. "
     "Higher: fewer switches, overlapping talk stays with the current speaker."),
    ("speech_db", "Speech above noise (dB)", 10, 3, 30,
     "How far above its noise floor a mic must be to count as someone talking."),
    ("min_turn", "Shortest turn (frames)", 12, 1, 120,
     "Shorter speaker turns are merged into the turn around them, so the mics don't flicker."),
    ("pre", "Open early (frames)", 4, 0, 30,
     "The mic opens this much before its speaker starts, so first syllables are not cut."),
    ("post", "Stay open after (frames)", 8, 0, 60,
     "The mic stays open this long after its speaker stops, so word endings are not cut."),
    ("min_off", "Shortest off gap (frames)", 15, 0, 120,
     "A mic is never switched off for less than this (no tiny dropouts)."),
    ("fade", "Fade (frames)", 2, 0, 15,
     "Short fade where a mic switches on or off, so the switch is not heard as a click."),
]
FRAME_RATES = {23.976: 24000 / 1001, 29.97: 30000 / 1001, 47.952: 48000 / 1001,
               59.94: 60000 / 1001, 119.88: 120000 / 1001}


# ---------------------------------------------------------------------------------------------
# Resolve and the helper

def get_resolve():
    g = globals()
    if g.get("resolve"):
        return g["resolve"]
    if g.get("app"):
        try:
            return g["app"].GetResolve()
        except Exception:
            pass
    import DaVinciResolveScript as dvr
    return dvr.scriptapp("Resolve")


def get_bmd():
    if globals().get("bmd"):
        return globals()["bmd"]
    try:
        import fusionscript
        return fusionscript
    except ImportError:
        import DaVinciResolveScript
        return DaVinciResolveScript


def analyzer_path():
    if not ANALYZER.startswith("@") and os.path.exists(ANALYZER):
        return ANALYZER
    # Development: the script run from the project folder, next to build/.
    here = os.path.dirname(os.path.abspath(globals().get("__file__", ".")))
    dev = os.path.join(here, "..", "build", "mic_switch_analyze")
    return dev if os.path.exists(dev) else None


def timeline_fps(tl):
    fps = float(tl.GetSetting("timelineFrameRate"))
    for nominal, exact in FRAME_RATES.items():
        if abs(fps - nominal) < 0.01:
            return exact
    return fps


def load_settings():
    values = {key: default for key, _, default, _, _, _ in SETTINGS}
    values["reactions"] = False
    try:
        with open(SETTINGS_FILE) as f:
            values.update(json.load(f))
    except Exception:
        pass
    return values


def save_settings(values):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(values, f, indent=1)
    except Exception:
        pass


def audio_tracks(tl):
    """[(index, name, number of clips)] of every audio track."""
    return [(k, tl.GetTrackName("audio", k), len(tl.GetItemListInTrack("audio", k) or []))
            for k in range(1, tl.GetTrackCount("audio") + 1)]


def default_tracks(tracks):
    """The lav or mic tracks by name, else the first two tracks that have clips."""
    named = [k for k, name, n in tracks if n and any(w in name.lower() for w in ("lav", "mic", "host", "guest"))]
    if len(named) >= 2:
        return named
    return [k for k, _, n in tracks if n][:2]


def source_channel(item):
    try:
        mapping = json.loads(item.GetSourceAudioChannelMapping())
        first = mapping["track_mapping"]["1"]["channel_idx"]
        return max(0, int(first[0]) - 1)
    except Exception:
        return 0


def read_layout(tl, track_indexes):
    """The enabled clips of each chosen track. Disabled clips are left as they are."""
    layout = {}
    for k in track_indexes:
        clips = []
        for it in tl.GetItemListInTrack("audio", k) or []:
            if not it.GetClipEnabled():
                continue
            mpi = it.GetMediaPoolItem()
            path = mpi.GetClipProperty("File Path") if mpi else ""
            if not path:
                raise RuntimeError(f"A{k} has a clip that is not a media file (\"{it.GetName()}\"), "
                                   "for example a compound clip. Mic Switch works on plain audio clips.")
            clips.append({"start": int(it.GetStart()), "end": int(it.GetEnd()),
                          "src_sec": it.GetSourceStartTime(), "left": int(round(it.GetLeftOffset(True))),
                          "channel": source_channel(it), "path": path, "name": it.GetName()})
        layout[k] = clips
    return layout


def run_analyzer(tl, track_indexes, values, progress=lambda fraction, text: None):
    """Returns (layout, result) where result has 'on' {track: [(start, end)]}, 'hist', 'stat'."""
    exe = analyzer_path()
    if not exe:
        raise RuntimeError("The Mic Switch helper program is missing. Reinstall Mic Switch.")
    layout = read_layout(tl, track_indexes)
    for k, clips in layout.items():
        if not clips:
            raise RuntimeError(f"A{k} has no enabled clips.")
    fps = timeline_fps(tl)
    work = tempfile.mkdtemp(prefix="micswitch")
    job, res = os.path.join(work, "job.txt"), os.path.join(work, "result.txt")
    with open(job, "w") as f:
        f.write(f"fps\t{fps!r}\nframes\t{tl.GetStartFrame()}\t{tl.GetEndFrame() + 1}\n")
        for key, _, _, _, _, _ in SETTINGS:
            f.write(f"set\t{key}\t{values[key]}\n")
        f.write(f"set\treactions\t{1 if values.get('reactions') else 0}\n")
        for k in track_indexes:
            f.write(f"track\tA{k} {tl.GetTrackName('audio', k)}\n")
            for c in layout[k]:
                f.write(f"clip\t{c['start']}\t{c['end']}\t{c['src_sec']!r}\t{c['channel']}\t{c['path']}\n")
    # The analyzer reports "progress <fraction> <text>" lines on stderr while it reads the audio.
    proc = subprocess.Popen([exe, job, res], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    other = []
    for line in proc.stderr:
        t = line.rstrip("\n").split("\t")
        if t[0] == "progress" and len(t) >= 3:
            progress(float(t[1]), t[2])
        else:
            other.append(line)
    proc.wait()
    errors = "".join(other).strip()
    result = {"on": {k: [] for k in track_indexes}, "hist": [], "stat": {}}
    lines = []
    if os.path.exists(res):
        with open(res) as f:
            lines = [l.rstrip("\n").split("\t") for l in f]
    for f_ in (job, res):
        try:
            os.remove(f_)
        except OSError:
            pass
    try:
        os.rmdir(work)
    except OSError:
        pass
    for t in lines:
        if t[0] == "error":
            if len(t) >= 3 and t[1] == "BLOCKED":
                raise RuntimeError(
                    "macOS did not let DaVinci Resolve read the audio file:\n" + t[2] + "\n\n"
                    "Open System Settings > Privacy & Security > Files and Folders (or Full Disk Access), "
                    "allow DaVinci Resolve to use that drive, restart Resolve and try again.")
            raise RuntimeError(t[1] if len(t) > 1 else "the analysis failed")
        if t[0] == "on":
            result["on"][track_indexes[int(t[1])]].append((int(t[2]), int(t[3])))
        elif t[0] == "hist":
            result["hist"].append((int(t[1]), int(t[2])))
        elif t[0] == "stat":
            result["stat"][t[1]] = int(t[2])
    if proc.returncode != 0 or not lines:
        raise RuntimeError("The analysis stopped unexpectedly. " + errors)
    return layout, result


# ---------------------------------------------------------------------------------------------
# The plan: pieces of every clip

def plan_pieces(layout, result, t0, t1):
    """{track: [(clip, [(start, end, on), ...])]} and per-track masks for fades."""
    plan, masks, covered = {}, {}, {}
    n = t1 - t0
    for k, clips in layout.items():
        mask = bytearray(n)
        for a, b in result["on"][k]:
            mask[max(0, a - t0):max(0, b - t0)] = b"\x01" * (max(0, b - t0) - max(0, a - t0))
        cov = bytearray(n)
        rows = []
        for c in clips:
            cov[c["start"] - t0:c["end"] - t0] = b"\x01" * (c["end"] - c["start"])
            pieces, s = [], c["start"]
            for f in range(c["start"] + 1, c["end"] + 1):
                if f == c["end"] or mask[f - t0] != mask[s - t0]:
                    pieces.append((s, f, bool(mask[s - t0])))
                    s = f
            rows.append((c, pieces))
        plan[k], masks[k], covered[k] = rows, mask, cov
    return plan, masks, covered


def wanted_fades(k, pieces, index, clip_fades, fade, masks, covered, t0):
    """Fade in/out for one piece: `fade` where the mic switches, else the clip's own fades."""
    a, b, on = pieces[index]
    mask, cov = masks[k], covered[k]
    fin = clip_fades.get("FadeIn", 0) if index == 0 else 0
    fout = clip_fades.get("FadeOut", 0) if index == len(pieces) - 1 else 0
    if on and fade:
        limit = max(0, (b - a) // 2)
        if a - 1 - t0 >= 0 and cov[a - 1 - t0] and not mask[a - 1 - t0]:
            fin = min(fade, limit)
        if b - t0 < len(mask) and cov[b - t0] and not mask[b - t0]:
            fout = min(fade, limit)
    return int(round(fin)), int(round(fout))


def trim_end(item, end):
    """Moves the clip's end to `end` (the right offset has a fractional part, so check and retry)."""
    offset = item.GetRightOffset() + (item.GetEnd() - end)
    for _ in range(4):
        item.SetRightOffset(offset, False, False)
        diff = int(item.GetEnd()) - end
        if diff == 0:
            return True
        offset += diff
    return False


def unique_name(project, base):
    names = {project.GetTimelineByIndex(i).GetName() for i in range(1, project.GetTimelineCount() + 1)}
    if base not in names:
        return base
    n = 2
    while f"{base} {n}" in names:
        n += 1
    return f"{base} {n}"


def apply_plan(project, src, layout, result, values, progress=lambda fraction, text: None):
    """Copies the timeline and rebuilds the chosen tracks as pieces. Returns (name, report)."""
    mp = project.GetMediaPool()
    t0, t1 = src.GetStartFrame(), src.GetEndFrame() + 1
    plan, masks, covered = plan_pieces(layout, result, t0, t1)
    fade = int(values.get("fade", 0))

    # Progress counts Resolve calls: a cut per clip, then an add, a settings copy and a switch per piece.
    total = sum(len(rows) + 3 * sum(len(p) for _, p in rows) for rows in plan.values()) + 1
    done = [0]

    def step(text, n=1):
        done[0] += n
        progress(done[0] / total, text)

    name = unique_name(project, src.GetName() + " (mic switch)")
    progress(0.0, f"Copying the timeline to \"{name}\"")
    tl = src.DuplicateTimeline(name)
    if not tl:
        raise RuntimeError("Resolve could not copy the timeline.")
    project.SetCurrentTimeline(tl)
    step("Timeline copied")

    report = []
    for k, rows in plan.items():
        items = {int(it.GetStart()): it for it in tl.GetItemListInTrack("audio", k) or []}
        appends, owners = [], []          # pieces 2.. of every split clip, and their original item
        targets = []                      # [item or None until appended, wanted on, fade in, fade out]
        for n, (c, pieces) in enumerate(rows, 1):
            it = items.get(c["start"])
            if it is None:
                raise RuntimeError(f"A{k}: the copied timeline has no clip at frame {c['start']}.")
            clip_fades = it.GetFades() or {}
            if len(pieces) > 1:
                if not trim_end(it, pieces[0][1]):
                    raise RuntimeError(f"A{k}: could not cut the clip at frame {pieces[0][1]}.")
            for i, (a, b, on) in enumerate(pieces):
                fin, fout = wanted_fades(k, pieces, i, clip_fades, fade, masks, covered, t0)
                if i == 0:
                    targets.append([it, on, fin, fout])
                else:
                    appends.append({"mediaPoolItem": it.GetMediaPoolItem(),
                                    "startFrame": c["left"] + (a - c["start"]),
                                    "endFrame": c["left"] + (b - c["start"]),
                                    "recordFrame": a, "trackIndex": k, "mediaType": 2})
                    owners.append(it)
                    targets.append([None, on, fin, fout])
            step(f"Cutting A{k} (clip {n} of {len(rows)})")
        # The first piece of every clip is the original clip, trimmed: it needs no adding.
        step(f"Cutting A{k}", len(rows))

        added = []
        for s in range(0, len(appends), 40):
            batch = appends[s:s + 40]
            added += mp.AppendToTimeline(batch) or []
            step(f"Adding pieces on A{k} ({min(s + 40, len(appends))} of {len(appends)})", len(batch))
        if len(added) != len(appends):
            raise RuntimeError(f"A{k}: Resolve added {len(added)} of {len(appends)} pieces.")

        # Pieces take everything from their original clip: volume, plugins, EQ, channel mapping.
        groups = {}
        for owner, new in zip(owners, added):
            groups.setdefault(id(owner), (owner, []))[1].append(new)
        copied = 0
        for owner, news in groups.values():
            owner.CopyAttributes(news, {"allAudioAttributes": True, "clipColor": True})
            mapping = owner.GetSourceAudioChannelMapping()
            for new in news:
                if new.GetSourceAudioChannelMapping() != mapping:
                    new.SetSourceAudioChannelMapping(mapping)
                if abs((new.GetProperty("AudioVolume") or 0) - (owner.GetProperty("AudioVolume") or 0)) > 0.01:
                    new.SetProperty("AudioVolume", owner.GetProperty("AudioVolume"))
            copied += len(news)
            step(f"Copying clip settings on A{k} ({copied} of {len(added)})", len(news))
        step(f"Copying clip settings on A{k}", len(targets) - len(added))
        it_added = iter(added)
        for tgt in targets:
            if tgt[0] is None:
                tgt[0] = next(it_added)

        for n, (item, on, fin, fout) in enumerate(targets, 1):
            if item.GetClipEnabled() != on:
                item.SetClipEnabled(on)
            if on:
                cur = item.GetFades() or {}
                if int(round(cur.get("FadeIn", 0))) != fin or int(round(cur.get("FadeOut", 0))) != fout:
                    item.SetFades({"FadeIn": fin, "FadeOut": fout})
            if n % 10 == 0 or n == len(targets):
                step(f"Switching mics on A{k} ({n} of {len(targets)})", 10 if n % 10 == 0 else n % 10)

        # Check what Resolve now has against the plan, and fix anything that did not stick.
        progress(done[0] / total, f"Checking A{k}")
        wanted = [(a, b, on) for _, pieces in rows for a, b, on in pieces]
        by_pos = {(int(i.GetStart()), int(i.GetEnd())): i for i in tl.GetItemListInTrack("audio", k) or []}
        missing = 0
        for a, b, on in wanted:
            i = by_pos.get((a, b))
            if i is None:
                missing += 1
            elif i.GetClipEnabled() != on:
                i.SetClipEnabled(on)
        off = sum(1 for _, _, on in wanted if not on)
        line = f"A{k}: {len(rows)} clips -> {len(wanted)} pieces, {off} switched off"
        if missing:
            line += f"  (WARNING: {missing} pieces are not where they should be)"
        report.append(line)
    return name, "\n".join(report)


# ---------------------------------------------------------------------------------------------
# Updates and messages

def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0])[:3]) or (0,)


def latest_release():
    """{'version', 'page', 'pkg'} of the newest GitHub release. Uses curl, which every Mac has."""
    out = subprocess.run(["curl", "-sSL", "--max-time", "15", "-H", "Accept: application/vnd.github+json",
                          "-w", "\n%{http_code}", f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"],
                         capture_output=True, text=True)
    body, _, code = out.stdout.rpartition("\n")
    if out.returncode != 0:
        raise RuntimeError("Could not reach GitHub. Check the internet connection.")
    if code == "404":
        raise RuntimeError("No release has been published yet.")
    if code != "200":
        raise RuntimeError(f"GitHub answered with status {code}.")
    data = json.loads(body)
    pkg = next((a["browser_download_url"] for a in data.get("assets", [])
                if a.get("name", "").lower().endswith(".pkg")), None)
    return {"version": data.get("tag_name", "").lstrip("vV"), "page": data.get("html_url", ""),
            "pkg": pkg, "notes": (data.get("body") or "")[:400]}


def dialog(text, buttons=("OK",)):
    """A native macOS dialog. Returns the label of the button pressed (None if it could not show)."""
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = (f"display dialog {q(text)} with title \"Mic Switch\" buttons {{{', '.join(q(b) for b in buttons)}}} "
              f"default button {q(buttons[-1])}")
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    m = re.search(r"button returned:(.*)", out.stdout)
    return m.group(1).strip() if m else None


def check_for_updates():
    try:
        rel = latest_release()
    except Exception as e:
        dialog(f"Could not check for updates: {e}")
        return
    if version_tuple(rel["version"]) <= version_tuple(VERSION if not VERSION.startswith("@") else "0"):
        dialog(f"You have the latest version ({VERSION}).")
        return
    text = f"Mic Switch {rel['version']} is available (you have {VERSION})."
    if rel["notes"]:
        text += "\n\n" + rel["notes"]
    if not rel["pkg"]:
        if dialog(text, ("Not Now", "Open Release Page")) == "Open Release Page":
            subprocess.run(["open", rel["page"]])
        return
    if dialog(text + "\n\nDownload and install it now?", ("Not Now", "Download")) != "Download":
        return
    target = os.path.join(os.path.expanduser("~/Downloads"), os.path.basename(rel["pkg"]))
    out = subprocess.run(["curl", "-fsSL", "--max-time", "300", "-o", target, rel["pkg"]], capture_output=True, text=True)
    if out.returncode != 0:
        dialog("The download failed. Opening the release page instead.")
        subprocess.run(["open", rel["page"]])
        return
    subprocess.run(["open", target])
    dialog("The installer is open. After installing, close Mic Switch and start it again from "
           "Workspace > Scripts (restart Resolve if the menu does not show the new version).")


# ---------------------------------------------------------------------------------------------
# Window

def describe(result, track_indexes, fps):
    out = []
    stat = result["stat"]
    out.append(f"Speaker turns: {stat.get('turns', 0)}")
    for i, k in enumerate(track_indexes):
        secs = stat.get(f"talk_{i}", 0) / fps
        out.append(f"A{k} talks {int(secs // 60)}:{int(secs % 60):02d}")
    hist = result["hist"]
    if len(track_indexes) >= 2 and hist and max(c for _, c in hist):
        a, b = track_indexes[0], track_indexes[1]
        top = max(c for _, c in hist)
        out.append("")
        out.append(f"Who is louder while someone talks (A{a} minus A{b}, dB):")
        for db, c in hist:
            if c == 0 and abs(db) > 20:
                continue
            out.append(f"{db:+4d} {'#' * int(round(36 * c / top))}")
        out.append("")
        out.append(f"Two humps, one on each side of 0, mean the mics are told apart well "
                   f"(left: A{b} talking, right: A{a} talking).")
    return "\n".join(out)


BAR_STYLE = ("QLabel {{ border: 1px solid #555; border-radius: 4px; padding: 4px; color: #f2f2f2; "
             "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {fill}, stop:{a:.4f} {fill}, "
             "stop:{b:.4f} #262626, stop:1 #262626); }}")


def main():
    resolve = get_resolve()
    bmd = get_bmd()
    project = resolve.GetProjectManager().GetCurrentProject()
    src = project.GetCurrentTimeline() if project else None

    if not src:
        dialog("Open the interview timeline first, then start Mic Switch again.")
        return
    tracks = audio_tracks(src)
    if len([t for t in tracks if t[2]]) < 2:
        dialog("Mic Switch needs at least two audio tracks with clips: one mic per person.")
        return

    ui = resolve.Fusion().UIManager
    disp = bmd.UIDispatcher(ui)
    values = load_settings()
    chosen = default_tracks(tracks)
    fps = timeline_fps(src)

    track_boxes = [ui.CheckBox({"ID": f"Track{k}", "Text": f"A{k}  {name}   ({n} clips)",
                                "Checked": k in chosen, "Enabled": n > 0}) for k, name, n in tracks]
    rows = []
    for key, label, default, lo, hi, tip in SETTINGS:
        rows.append(ui.HGroup({"Weight": 0}, [
            ui.Label({"Text": label, "ToolTip": tip, "Weight": 1}),
            ui.SpinBox({"ID": key, "Minimum": lo, "Maximum": hi, "Value": int(values.get(key, default)),
                        "ToolTip": tip, "Weight": 0, "MinimumSize": [70, 0]}),
        ]))
    rows.append(ui.CheckBox({"ID": "reactions", "Text": "Keep short reactions (laughs, \"mm-hmm\")",
                             "Checked": bool(values.get("reactions")),
                             "ToolTip": "Short reactions under \"Shortest turn\" open that mic anyway. "
                                        "Off: they stay with the person who is talking."}))

    win = disp.AddWindow(
        {"ID": "MicSwitch", "WindowTitle": f"Mic Switch {'' if VERSION.startswith('@') else VERSION}".strip(),
         "Geometry": [260, 140, 560, 800]},
        ui.VGroup([
            ui.Label({"Text": f"<b>Timeline:</b> {src.GetName()}", "Weight": 0}),
            ui.Label({"Text": "<b>Mic tracks</b> (one lav per person)", "Weight": 0}),
            ui.VGroup({"Weight": 0}, track_boxes),
            ui.VGap(6, 0),
            ui.Label({"Text": "<b>Settings</b>", "Weight": 0}),
            ui.VGroup({"Weight": 0}, rows),
            ui.HGroup({"Weight": 0}, [ui.HGap(), ui.Button({"ID": "Defaults", "Text": "Reset Settings"})]),
            ui.TextEdit({"ID": "Report", "ReadOnly": True, "Weight": 1,
                         "Font": ui.Font({"Family": "Menlo", "PixelSize": 11}),
                         "PlainText": "Press Analyze to measure who is speaking, or Create Timeline to do it all.\n\n"
                                      "Your timeline is not changed: Mic Switch makes a copy. On the copy, the "
                                      "pieces where a mic is off are disabled; select one and press D to bring it back."}),
            ui.Label({"ID": "Progress", "Text": "Ready", "Weight": 0, "MinimumSize": [0, 26],
                      "Alignment": {"AlignHCenter": True, "AlignVCenter": True}}),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Analyze", "Text": "Analyze"}),
                ui.Button({"ID": "Apply", "Text": "Create Timeline", "Default": True}),
                ui.HGap(),
                ui.Button({"ID": "Close", "Text": "Close"}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Update", "Text": "Check for Updates", "Flat": True}),
                ui.HGap(),
                ui.Button({"ID": "Donate", "Text": "Support on Ko-fi", "Flat": True}),
            ]),
        ]))
    items = win.GetItems()
    buttons = ["Analyze", "Apply", "Close", "Defaults", "Update", "Donate"]

    def bar(fraction, text, fill="#c4552c"):
        f = max(0.0, min(1.0, fraction))
        items["Progress"].StyleSheet = BAR_STYLE.format(fill=fill, a=max(0.0001, f - 0.0001), b=max(0.0002, f))
        items["Progress"].Text = f"{int(f * 100)}%   {text}"

    def idle(text="Ready", fill=None):
        items["Progress"].StyleSheet = BAR_STYLE.format(fill=fill, a=0.9998, b=0.9999) if fill else ""
        items["Progress"].Text = text

    def busy(on):
        for b in buttons:
            items[b].Enabled = not on

    def current_values():
        v = {key: int(items[key].Value) for key, *_ in SETTINGS}
        v["reactions"] = bool(items["reactions"].Checked)
        return v

    def current_tracks():
        return [k for k, _, n in tracks if n and items[f"Track{k}"].Checked]

    def analyze(lo, hi):
        """Runs the analysis; the bar goes from lo to hi."""
        ks = current_tracks()
        if len(ks) < 2:
            raise RuntimeError("Choose at least two mic tracks.")
        v = current_values()
        save_settings(v)
        bar(lo, "Reading the clips")
        layout, result = run_analyzer(src, ks, v, lambda f, t: bar(lo + (hi - lo) * f, t))
        items["Report"].PlainText = describe(result, ks, fps)
        return ks, v, layout, result

    def run(job):
        busy(True)
        try:
            job()
        except Exception as e:
            idle("Stopped: see the message", fill="#7a2a2a")
            dialog(str(e))
        finally:
            busy(False)

    def on_analyze(ev):
        def job():
            analyze(0.0, 1.0)
            idle("Analysis done", fill="#2f6b3a")
        run(job)

    def on_apply(ev):
        def job():
            ks, v, layout, result = analyze(0.0, 0.2)
            summary = items["Report"].PlainText
            name, report = apply_plan(project, src, layout, result, v,
                                      progress=lambda f, t: bar(0.2 + 0.8 * f, t))
            items["Report"].PlainText = (f"Created \"{name}\" (now open).\n{report}\n\n"
                                         "Listen through it. To bring back an off piece, select it and press D.\n\n"
                                         + summary)
            idle(f"Done: \"{name}\" is open", fill="#2f6b3a")
        run(job)

    def on_defaults(ev):
        for key, _, default, *_ in SETTINGS:
            items[key].Value = default
        items["reactions"].Checked = False

    def on_update(ev):
        idle("Checking for updates...")
        run(check_for_updates)
        idle()

    def on_donate(ev):
        subprocess.run(["open", DONATE_URL])

    def on_close(ev):
        save_settings(current_values())
        disp.ExitLoop()

    win.On.Analyze.Clicked = on_analyze
    win.On.Apply.Clicked = on_apply
    win.On.Defaults.Clicked = on_defaults
    win.On.Update.Clicked = on_update
    win.On.Donate.Clicked = on_donate
    win.On.Close.Clicked = on_close
    win.On.MicSwitch.Close = on_close
    win.Show()
    disp.RunLoop()
    win.Hide()


if not os.environ.get("MIC_SWITCH_NO_UI"):
    main()
