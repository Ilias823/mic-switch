"""Checks the analyzer on made-up interviews (standard library only).

    python3 -I tests/test_analyzer.py build/mic_switch_analyze

Two lavs, each hearing the other person 12 dB quieter (bleed), recorded at very different gains,
take turns talking. The analyzer must switch mics at the turns, without any tuning.
"""
import math
import os
import random
import struct
import subprocess
import sys
import tempfile

SR = 48000
FPS = 25


def write_wav(path, samples, fmt):
    """fmt: 'f32', 'i24', 'i16'. Mono."""
    if fmt == "f32":
        data = struct.pack("<%df" % len(samples), *samples)
        tag, bits = 3, 32
    elif fmt == "i16":
        data = struct.pack("<%dh" % len(samples), *(max(-32768, min(32767, int(s * 32767))) for s in samples))
        tag, bits = 1, 16
    else:
        out = bytearray()
        for s in samples:
            v = max(-8388608, min(8388607, int(s * 8388607))) & 0xFFFFFF
            out += bytes((v & 255, (v >> 8) & 255, v >> 16))
        data = bytes(out)
        tag, bits = 1, 24
    fmt_chunk = struct.pack("<HHIIHH", tag, 1, SR, SR * bits // 8, bits // 8, bits)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + 8 + 16 + 8 + 4 + 8 + len(data)) + b"WAVE")
        f.write(b"bext" + struct.pack("<I", 4) + b"\0\0\0\0")  # an extra chunk, as recorders write
        f.write(b"fmt " + struct.pack("<I", 16) + fmt_chunk)
        f.write(b"data" + struct.pack("<I", len(data)) + data)


def speech(rng, n):
    """Noise bursts shaped like syllables (~4 per second) with short pauses."""
    out = []
    phase = 0.0
    for i in range(n):
        phase += 2 * math.pi * 4.3 / SR
        env = max(0.0, math.sin(phase)) ** 2
        out.append(env * rng.gauss(0, 1))
    return out


def main(exe):
    rng = random.Random(1)
    # turns in seconds: host, guest, host, guest ...
    turns = [(0, 3, "h"), (3, 7, "g"), (7, 9, "h"), (9, 14, "g"), (14, 16, "h")]
    total = 16 * SR
    host_voice = [0.0] * total
    guest_voice = [0.0] * total
    for a, b, who in turns:
        v = speech(rng, (b - a) * SR - SR // 5) + [0.0] * (SR // 5)  # 0.2 s pause at the end
        target = host_voice if who == "h" else guest_voice
        target[a * SR:b * SR] = v
    bleed = 10 ** (-12 / 20)
    host_gain, guest_gain = 0.3, 0.03  # 20 dB apart: the lav gains differ a lot
    host = [host_gain * (h + bleed * g) + rng.gauss(0, 0.0005) for h, g in zip(host_voice, guest_voice)]
    guest = [guest_gain * (g + bleed * h) + rng.gauss(0, 0.00005) for h, g in zip(host_voice, guest_voice)]

    tmp = tempfile.mkdtemp()
    hp, gp = os.path.join(tmp, "host.wav"), os.path.join(tmp, "guest.wav")
    write_wav(hp, host, "f32")
    write_wav(gp, guest, "i24")
    # The host clip starts 2 s into its file, to check source offsets.
    write_wav(os.path.join(tmp, "host_late.wav"), [0.0] * (2 * SR) + host, "i16")

    job = os.path.join(tmp, "job.txt")
    res = os.path.join(tmp, "res.txt")
    t0 = 1000
    with open(job, "w") as f:
        f.write(f"fps\t{FPS}\nframes\t{t0}\t{t0 + 16 * FPS}\n")
        f.write("track\tHost\n")
        f.write(f"clip\t{t0}\t{t0 + 8 * FPS}\t0\t0\t{hp}\n")
        f.write(f"clip\t{t0 + 8 * FPS}\t{t0 + 16 * FPS}\t10\t0\t{os.path.join(tmp, 'host_late.wav')}\n")
        f.write("track\tGuest\n")
        f.write(f"clip\t{t0}\t{t0 + 16 * FPS}\t0\t0\t{gp}\n")
    r = subprocess.run([exe, job, res])
    lines = [l.rstrip("\n").split("\t") for l in open(res)]
    errors = [l for l in lines if l[0] == "error"]
    assert r.returncode == 0 and not errors, errors

    on = {0: [], 1: []}
    for l in lines:
        if l[0] == "on":
            on[int(l[1])].append((int(l[2]) - t0, int(l[3]) - t0))
    print("host on:", on[0])
    print("guest on:", on[1])

    def is_on(t, frame):
        return any(a <= frame < b for a, b in on[t])

    bad = 0
    for a, b, who in turns:
        speaker, other = (0, 1) if who == "h" else (1, 0)
        # well inside each turn (skip 0.5 s at both ends): speaker on, other off
        for frame in range(a * FPS + FPS // 2, b * FPS - FPS // 2):
            if not is_on(speaker, frame) or is_on(other, frame):
                bad += 1
    print("wrong frames inside turns:", bad)
    assert bad == 0
    turns_stat = [l for l in lines if l[0] == "stat" and l[1] == "turns"][0]
    assert int(turns_stat[2]) == len(turns), turns_stat

    # A missing file is reported, not a crash.
    with open(job, "a") as f:
        f.write(f"clip\t{t0}\t{t0 + 10}\t0\t0\t{tmp}/missing.wav\n")
    subprocess.run([exe, job, res])
    first = open(res).readline()
    assert first.startswith("error\t") and "missing.wav" in first, first
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1])
