// mic_switch_analyze: decides, frame by frame, which mic of an interview should be on.
//
//   mic_switch_analyze <job.txt> <result.txt>
//
// The Resolve script writes the job (the clip layout of the mic tracks plus the settings) and
// reads the result (for every track, the timeline frame ranges where that mic stays on).
// Plain text both ways, so the script needs nothing beyond Resolve's own Python.
//
// Job lines (tab separated):
//   fps     <frames per second>
//   frames  <first timeline frame> <end frame, exclusive>
//   set     <name> <value>                 settings, see Settings below
//   track   <name>                         starts a mic track
//   clip    <start> <end> <source in, seconds> <channel> <path>
//
// Result lines:
//   on      <track index> <start> <end>    the mic is on for these frames (end exclusive)
//   hist    <dB> <count>                   SNR of track 0 minus track 1 while someone talks
//   stat    <name> <value>
//   error   <message>                      nothing else is written when this appears
//
// While it works it prints "progress <0..1> <what it is doing>" lines on stderr, for the
// script's progress bar.
//
// How it decides: every mic is measured against its own noise floor (signal-to-noise ratio),
// so lavs recorded at different gains compete fairly without any tuning. The loudest mic, if it
// beats the others by `focus` dB while someone talks, is the speaker; otherwise the previous
// speaker is kept. Short turns are merged, each mic opens a little early and stays open a little
// late, and short off gaps are filled in. The noise floor is found by minimum statistics, as in
// the Automixer: the quietest moment of a smoothed level over a few seconds.

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace
{

struct Settings
{
    double speechDb = 10.0;   // a mic this far above its noise floor means someone is talking
    double focusDb = 3.0;     // the loudest mic must beat the others by this much to take over
    int smooth = 5;           // frames of level smoothing before deciding
    int minTurn = 12;         // shorter speaker turns are merged into the neighbouring turn
    int pre = 4;              // frames a mic opens before its turn
    int post = 8;             // frames a mic stays open after its turn
    int minOff = 15;          // shorter off gaps stay on
    int reactions = 0;        // 1: short reactions (laugh, "mm-hmm") open that mic anyway
    int minReaction = 6;      // frames: shortest reaction that opens a mic
    double floorSeconds = 6.0; // window for the noise floor (centred)
};

struct Clip
{
    int64_t start = 0, end = 0;
    double sourceIn = 0;
    int channel = 0;
    std::string path;
};

struct Track
{
    std::string name;
    std::vector<Clip> clips;
};

struct Job
{
    double fps = 25;
    int64_t t0 = 0, t1 = 0;
    Settings s;
    std::vector<Track> tracks;
};

std::vector<std::string> splitTabs (const std::string& line)
{
    std::vector<std::string> out;
    size_t a = 0;
    for (;;)
    {
        const size_t b = line.find ('\t', a);
        out.push_back (line.substr (a, b == std::string::npos ? std::string::npos : b - a));
        if (b == std::string::npos)
            return out;
        a = b + 1;
    }
}

Job readJob (const char* path)
{
    FILE* f = std::fopen (path, "r");
    if (! f)
        throw std::runtime_error (std::string ("cannot read the job file: ") + std::strerror (errno));
    Job job;
    char* buf = nullptr;
    size_t cap = 0;
    ssize_t len;
    while ((len = getline (&buf, &cap, f)) > 0)
    {
        std::string line (buf, (size_t) len);
        while (! line.empty() && (line.back() == '\n' || line.back() == '\r'))
            line.pop_back();
        const auto t = splitTabs (line);
        if (t[0] == "fps" && t.size() >= 2)
            job.fps = std::stod (t[1]);
        else if (t[0] == "frames" && t.size() >= 3)
            job.t0 = std::stoll (t[1]), job.t1 = std::stoll (t[2]);
        else if (t[0] == "track" && t.size() >= 2)
            job.tracks.push_back ({ t[1], {} });
        else if (t[0] == "clip" && t.size() >= 6 && ! job.tracks.empty())
            job.tracks.back().clips.push_back ({ std::stoll (t[1]), std::stoll (t[2]), std::stod (t[3]),
                                                 std::stoi (t[4]), t[5] });
        else if (t[0] == "set" && t.size() >= 3)
        {
            const double v = std::stod (t[2]);
            auto& s = job.s;
            if (t[1] == "speech_db") s.speechDb = v;
            else if (t[1] == "focus_db") s.focusDb = v;
            else if (t[1] == "smooth") s.smooth = std::max (1, (int) v);
            else if (t[1] == "min_turn") s.minTurn = std::max (1, (int) v);
            else if (t[1] == "pre") s.pre = std::max (0, (int) v);
            else if (t[1] == "post") s.post = std::max (0, (int) v);
            else if (t[1] == "min_off") s.minOff = std::max (0, (int) v);
            else if (t[1] == "reactions") s.reactions = (int) v;
            else if (t[1] == "min_reaction") s.minReaction = std::max (1, (int) v);
            else if (t[1] == "floor_seconds") s.floorSeconds = std::max (1.0, v);
        }
    }
    std::free (buf);
    std::fclose (f);
    if (job.tracks.size() < 2)
        throw std::runtime_error ("choose at least two mic tracks");
    if (job.t1 <= job.t0 || job.fps <= 0)
        throw std::runtime_error ("the timeline is empty");
    return job;
}

// ---------------------------------------------------------------------------------------------
// WAV reading (memory-mapped). PCM 16/24/32-bit, float 32/64, WAVE_FORMAT_EXTENSIBLE, RF64.

struct Wav
{
    const uint8_t* map = nullptr;
    size_t mapSize = 0;
    const uint8_t* data = nullptr;
    uint64_t frames = 0;
    int channels = 1, bits = 16, sampleRate = 48000;
    bool isFloat = false;

    double sample (uint64_t i, int ch) const
    {
        const uint8_t* p = data + (i * (uint64_t) channels + (uint64_t) ch) * (uint64_t) (bits / 8);
        if (isFloat)
        {
            if (bits == 32) { float v; std::memcpy (&v, p, 4); return v; }
            double v; std::memcpy (&v, p, 8); return v;
        }
        switch (bits)
        {
            case 16: return int16_t (p[0] | (p[1] << 8)) / 32768.0;
            case 24: return int32_t (uint32_t (p[0] << 8 | p[1] << 16 | p[2] << 24)) / 2147483648.0;
            case 32: { int32_t v; std::memcpy (&v, p, 4); return v / 2147483648.0; }
            default: return 0.0; // 8-bit and others are rejected when opening
        }
    }
};

uint32_t le32 (const uint8_t* p) { return uint32_t (p[0]) | uint32_t (p[1]) << 8 | uint32_t (p[2]) << 16 | uint32_t (p[3]) << 24; }
uint16_t le16 (const uint8_t* p) { return uint16_t (p[0] | p[1] << 8); }
uint64_t le64 (const uint8_t* p) { return uint64_t (le32 (p)) | uint64_t (le32 (p + 4)) << 32; }

Wav openWav (const std::string& path)
{
    const int fd = open (path.c_str(), O_RDONLY);
    if (fd < 0)
    {
        if (errno == EPERM || errno == EACCES)
            throw std::runtime_error ("BLOCKED\t" + path);
        throw std::runtime_error ("cannot open " + path + ": " + std::strerror (errno));
    }
    struct stat st {};
    fstat (fd, &st);
    void* m = mmap (nullptr, (size_t) st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    close (fd);
    if (m == MAP_FAILED)
        throw std::runtime_error ("cannot read " + path);
    Wav w;
    w.map = (const uint8_t*) m;
    w.mapSize = (size_t) st.st_size;
    const uint8_t* p = w.map;
    const uint8_t* end = w.map + w.mapSize;
    if (w.mapSize < 12 || (std::memcmp (p, "RIFF", 4) && std::memcmp (p, "RF64", 4)) || std::memcmp (p + 8, "WAVE", 4))
        throw std::runtime_error ("not a WAV file (Mic Switch reads .wav audio): " + path);
    uint64_t ds64Data = 0;
    bool haveFmt = false;
    uint16_t tag = 0;
    p += 12;
    while (p + 8 <= end)
    {
        const uint64_t sz = le32 (p + 4);
        const uint8_t* body = p + 8;
        if (! std::memcmp (p, "ds64", 4) && body + 16 <= end)
            ds64Data = le64 (body + 8);
        else if (! std::memcmp (p, "fmt ", 4) && body + 16 <= end)
        {
            tag = le16 (body);
            w.channels = le16 (body + 2);
            w.sampleRate = (int) le32 (body + 4);
            w.bits = le16 (body + 14);
            if (tag == 0xFFFE && sz >= 26 && body + 26 <= end)
                tag = le16 (body + 24); // sub-format GUID starts with the format tag
            haveFmt = true;
        }
        else if (! std::memcmp (p, "data", 4))
        {
            uint64_t bytes = (sz == 0xFFFFFFFFu && ds64Data) ? ds64Data : sz;
            bytes = std::min<uint64_t> (bytes, uint64_t (end - body));
            if (! haveFmt || w.channels < 1)
                throw std::runtime_error ("damaged WAV file: " + path);
            w.isFloat = tag == 3;
            if (! ((tag == 1 && (w.bits == 16 || w.bits == 24 || w.bits == 32)) || (tag == 3 && (w.bits == 32 || w.bits == 64))))
                throw std::runtime_error ("unsupported WAV format (" + std::to_string (w.bits) + "-bit) in " + path);
            w.data = body;
            w.frames = bytes / (uint64_t (w.channels) * uint64_t (w.bits / 8));
            return w;
        }
        p = body + sz + (sz & 1);
    }
    throw std::runtime_error ("damaged WAV file (no audio data): " + path);
}

// ---------------------------------------------------------------------------------------------

constexpr double kPi = 3.14159265358979323846;
constexpr double kSilence = 1e-12; // digital silence or no clip
constexpr double kMinFloor = 1e-10;

struct Biquad
{
    double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0, z1 = 0, z2 = 0;
    void set (double sr, double f, bool high)
    {
        const double w = 2 * kPi * f / sr, c = std::cos (w), a = std::sin (w) / std::sqrt (2.0), a0 = 1 + a;
        b0 = (high ? (1 + c) : (1 - c)) / 2 / a0;
        b1 = (high ? -(1 + c) : (1 - c)) / a0;
        b2 = b0;
        a1 = -2 * c / a0;
        a2 = (1 - a) / a0;
        z1 = z2 = 0;
    }
    double tick (double x)
    {
        const double y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        return y;
    }
};

// Progress over all clips of all tracks, by timeline frames measured.
struct Progress
{
    int64_t total = 1, done = 0;
    void report (const std::string& what)
    {
        std::fprintf (stderr, "progress\t%.3f\t%s\n", double (done) / double (std::max<int64_t> (1, total)), what.c_str());
        std::fflush (stderr);
    }
};

// Speech-band power of every timeline frame of one track; kSilence where it has no clip.
std::vector<double> framePower (const Track& tr, const Job& job, std::map<std::string, Wav>& files, Progress& progress)
{
    const int64_t n = job.t1 - job.t0;
    std::vector<double> pw ((size_t) n, kSilence);
    for (size_t ci = 0; ci < tr.clips.size(); ++ci)
    {
        const auto& c = tr.clips[ci];
        progress.report ("Measuring " + tr.name + " (clip " + std::to_string (ci + 1) + " of " + std::to_string (tr.clips.size()) + ")");
        progress.done += c.end - c.start;
        auto it = files.find (c.path);
        if (it == files.end())
            it = files.emplace (c.path, openWav (c.path)).first;
        const Wav& w = it->second;
        const int ch = std::clamp (c.channel, 0, w.channels - 1);
        Biquad hp, lp;
        hp.set (w.sampleRate, 100.0, true);
        lp.set (w.sampleRate, 4000.0, false);

        const int64_t f0 = std::max (c.start, job.t0), f1 = std::min (c.end, job.t1);
        if (f1 <= f0)
            continue;
        auto sampleAt = [&] (int64_t frame) {
            return (int64_t) std::llround ((c.sourceIn + double (frame - c.start) / job.fps) * w.sampleRate);
        };
        // Run the filters from 50 ms earlier so they have settled by the first frame.
        int64_t s = std::max<int64_t> (0, sampleAt (f0) - w.sampleRate / 20);
        for (; s < sampleAt (f0) && s < (int64_t) w.frames; ++s)
            lp.tick (hp.tick (w.sample ((uint64_t) s, ch)));
        for (int64_t f = f0; f < f1; ++f)
        {
            const int64_t a = sampleAt (f), b = sampleAt (f + 1);
            double sum = 0;
            int64_t cnt = 0;
            for (s = std::max<int64_t> (a, 0); s < b && s < (int64_t) w.frames; ++s, ++cnt)
            {
                const double y = lp.tick (hp.tick (w.sample ((uint64_t) s, ch)));
                sum += y * y;
            }
            if (cnt > 0)
                pw[(size_t) (f - job.t0)] = std::max (sum / double (cnt), kSilence);
        }
    }
    return pw;
}

std::vector<double> movingAverage (const std::vector<double>& x, int len)
{
    const size_t n = x.size();
    std::vector<double> out (n);
    std::vector<double> pre (n + 1, 0.0);
    for (size_t i = 0; i < n; ++i)
        pre[i + 1] = pre[i] + x[i];
    const int h = len / 2;
    for (size_t i = 0; i < n; ++i)
    {
        const size_t a = (size_t) std::max<int64_t> (0, (int64_t) i - h);
        const size_t b = std::min (n, i + (size_t) (len - h));
        out[i] = (pre[b] - pre[a]) / double (b - a);
    }
    return out;
}

// Noise floor by minimum statistics: the minimum of a ~100 ms smoothed level over a centred window,
// ignoring frames with no audio. Sliding minimum with a monotonic deque.
std::vector<double> noiseFloor (const std::vector<double>& pw, double fps, double seconds)
{
    const auto sm = movingAverage (pw, std::max (1, (int) std::lround (fps * 0.1)));
    const int64_t n = (int64_t) sm.size(), half = std::max<int64_t> (1, (int64_t) std::lround (fps * seconds / 2));
    std::vector<double> out ((size_t) n, -1.0);
    std::vector<int64_t> dq ((size_t) n + 1);
    size_t head = 0, tail = 0;
    int64_t next = 0;
    for (int64_t i = 0; i < n; ++i)
    {
        for (; next < n && next <= i + half; ++next)
        {
            if (pw[(size_t) next] <= kSilence)
                continue;
            while (tail > head && sm[(size_t) dq[tail - 1]] >= sm[(size_t) next])
                --tail;
            dq[tail++] = next;
        }
        while (tail > head && dq[head] < i - half)
            ++head;
        if (tail > head)
            out[(size_t) i] = std::max (sm[(size_t) dq[head]], kMinFloor);
    }
    return out;
}

struct Run { int64_t s, e; int v; };

template <typename T>
std::vector<Run> runs (const std::vector<T>& a)
{
    std::vector<Run> r;
    int64_t s = 0;
    const int64_t n = (int64_t) a.size();
    for (int64_t i = 1; i <= n; ++i)
        if (i == n || a[(size_t) i] != a[(size_t) s])
        {
            r.push_back ({ s, i, (int) a[(size_t) s] });
            s = i;
        }
    return r;
}

void analyze (const Job& job, FILE* out)
{
    const auto& s = job.s;
    const size_t k = job.tracks.size();
    const int64_t n = job.t1 - job.t0;
    std::map<std::string, Wav> files;

    // SNR in dB per track and frame; -inf where the track has no audio.
    std::vector<std::vector<double>> snr (k);
    std::vector<std::vector<bool>> present (k);
    Progress progress;
    progress.total = 0;
    for (const auto& tr : job.tracks)
        for (const auto& c : tr.clips)
            progress.total += c.end - c.start;
    for (size_t t = 0; t < k; ++t)
    {
        const auto pw = framePower (job.tracks[t], job, files, progress);
        const auto floor = noiseFloor (pw, job.fps, s.floorSeconds);
        const auto sm = movingAverage (pw, s.smooth);
        snr[t].assign ((size_t) n, -INFINITY);
        present[t].assign ((size_t) n, false);
        for (int64_t i = 0; i < n; ++i)
        {
            present[t][(size_t) i] = pw[(size_t) i] > kSilence;
            if (present[t][(size_t) i] && floor[(size_t) i] > 0)
                snr[t][(size_t) i] = 10.0 * std::log10 (sm[(size_t) i] / floor[(size_t) i]);
        }
    }

    progress.report ("Working out who is speaking");

    // Who is speaking, per frame (track index). Starts with whoever speaks first.
    std::vector<int> raw ((size_t) n, -1);
    std::vector<int> hist (31, 0);
    int prev = -1;
    for (int64_t i = 0; i < n; ++i)
    {
        int best = -1;
        double b1 = -INFINITY, b2 = -INFINITY;
        for (size_t t = 0; t < k; ++t)
        {
            const double v = snr[t][(size_t) i];
            if (v > b1) { b2 = b1; b1 = v; best = (int) t; }
            else if (v > b2) b2 = v;
        }
        if (b1 > s.speechDb)
        {
            if (b1 - b2 > s.focusDb)
                prev = best;
            if (std::isfinite (snr[0][(size_t) i]) && std::isfinite (snr[1][(size_t) i]))
            {
                const int bin = (int) std::floor ((snr[0][(size_t) i] - snr[1][(size_t) i] + 30.0) / 2.0);
                if (bin >= 0 && bin < (int) hist.size())
                    ++hist[(size_t) bin];
            }
        }
        raw[(size_t) i] = prev;
    }
    // Before the first speaker: give those frames to the first speaker.
    int first = -1;
    for (int v : raw)
        if (v >= 0) { first = v; break; }
    if (first < 0)
        throw std::runtime_error ("no speech found: the mics never rise above their noise floor. "
                                  "Lower \"Speech above noise\" or check the tracks.");
    for (auto& v : raw)
    {
        if (v >= 0) break;
        v = first;
    }

    // Merge short turns into the neighbouring turn (no flicker).
    std::vector<int> lab = raw;
    for (bool changed = true; changed;)
    {
        changed = false;
        const auto r = runs (lab);
        if (r.size() < 2)
            break;
        for (size_t j = 0; j < r.size(); ++j)
            if (r[j].e - r[j].s < s.minTurn)
            {
                const int v = j > 0 ? r[j - 1].v : r[j + 1].v;
                std::fill (lab.begin() + r[j].s, lab.begin() + r[j].e, v);
                changed = true;
                break;
            }
    }

    for (size_t t = 0; t < k; ++t)
    {
        std::vector<char> on ((size_t) n, 0), mask ((size_t) n, 0);
        for (int64_t i = 0; i < n; ++i)
            on[(size_t) i] = lab[(size_t) i] == (int) t;
        if (s.reactions)
            for (const auto& r : runs (raw))
                if (r.v == (int) t && r.e - r.s >= s.minReaction)
                    std::fill (on.begin() + r.s, on.begin() + r.e, 1);
        for (const auto& r : runs (on))
            if (r.v)
                std::fill (mask.begin() + std::max<int64_t> (0, r.s - s.pre), mask.begin() + std::min (n, r.e + s.post), 1);
        for (const auto& r : runs (mask))
            if (! r.v && r.e - r.s < s.minOff)
                std::fill (mask.begin() + r.s, mask.begin() + r.e, 1);
        int64_t talk = 0;
        for (int64_t i = 0; i < n; ++i)
            talk += lab[(size_t) i] == (int) t;
        for (const auto& r : runs (mask))
            if (r.v)
                std::fprintf (out, "on\t%zu\t%lld\t%lld\n", t, (long long) (job.t0 + r.s), (long long) (job.t0 + r.e));
        std::fprintf (out, "stat\ttalk_%zu\t%lld\n", t, (long long) talk);
    }
    for (size_t b = 0; b < hist.size(); ++b)
        std::fprintf (out, "hist\t%d\t%d\n", -30 + 2 * (int) b, hist[b]);
    std::fprintf (out, "stat\tturns\t%zu\n", runs (lab).size());

    for (auto& [path, w] : files)
        munmap ((void*) w.map, w.mapSize);
}

} // namespace

int main (int argc, char** argv)
{
    if (argc != 3)
    {
        std::fprintf (stderr, "usage: %s <job.txt> <result.txt>\n", argv[0]);
        return 2;
    }
    FILE* out = std::fopen (argv[2], "w");
    if (! out)
    {
        std::fprintf (stderr, "cannot write %s\n", argv[2]);
        return 2;
    }
    try
    {
        analyze (readJob (argv[1]), out);
    }
    catch (const std::exception& e)
    {
        std::fclose (out);
        out = std::fopen (argv[2], "w");
        if (out)
        {
            std::fprintf (out, "error\t%s\n", e.what());
            std::fclose (out);
        }
        return 1;
    }
    std::fclose (out);
    return 0;
}
