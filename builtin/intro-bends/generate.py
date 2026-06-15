"""Generate the three starter sloppaks for the "Intro to Bends" tutorial pack.

Outputs (under the directory this script lives in unless --out given):
  sloppaks/Tutorial_Intro_to_Bends_L1.sloppak  half-step bends
  sloppaks/Tutorial_Intro_to_Bends_L2.sloppak  full-step bends
  sloppaks/Tutorial_Intro_to_Bends_L3.sloppak  bend + vibrato

Each sloppak contains:
  - manifest.yaml             title/artist/duration/stems/arrangements
  - stems/audio.mp3           fluidsynth-rendered guitar+bass+drums backing
  - arrangements/lead.json    note chart in the sloppak v1 JSON shape

Format reference: ~/Repositories/slopsmith/lib/sloppak.py + a real sloppak's
manifest + arrangement JSON. String indexing follows the RS convention per
lib/gp2rs.py:459 — s=0 is the LOW E (thickest string), s=5 is the HIGH E
(thinnest string). This is the opposite of GP string numbering.

Audio: a 3-track MIDI file (overdriven lead guitar with pitch-bent bend
notes, electric bass holding the tonic, drum kit playing a basic backbeat)
is rendered through FluidR3_GM.sf2 via fluidsynth, then encoded to MP3.
This keeps the backing musically aligned with the highway notes — the lead
plays the exact bend sequence the player is asked to perform, so the
player has a reference for both timing and target pitch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import yaml
import zipfile
from pathlib import Path

# ── Musical helpers ──────────────────────────────────────────────────────────

# Rocksmith string index: 0 = LOW E, 5 = HIGH E (per lib/gp2rs.py:459 —
# "Convert GP string number (1=high) to RS string index (0=low)."). The
# .sloppak arrangement JSON `s` field follows this convention, so the
# highway renderer reads (s, f) using this open-pitch table. Match it
# exactly here so the MIDI lead voices the same pitches the highway
# draws.
OPEN_MIDI = {
    0: 40,  # E2 — low E (RS string 0)
    1: 45,  # A2
    2: 50,  # D3
    3: 55,  # G3
    4: 59,  # B3
    5: 64,  # E4 — high E (RS string 5)
}


def midi_of(string: int, fret: int) -> int:
    """MIDI pitch of a fretted note in standard tuning."""
    return OPEN_MIDI[string] + fret


def hz_of(midi: int) -> float:
    """Frequency of a MIDI pitch (A4 = 440 Hz)."""
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


# ── Note primitive ───────────────────────────────────────────────────────────

# The sloppak v1 note schema (every field must be present or the loader is
# unhappy). Defaults match what real sloppaks ship.
def note(t: float, s: int, f: int, sus: float = 0.0, bn: float = 0.0,
         tr: bool = False, pm: bool = False, mt: bool = False) -> dict:
    return {
        "t":   round(t, 3),
        "s":   s,
        "f":   f,
        "sus": round(sus, 3),
        "sl":  -1,
        "slu": -1,
        "bn":  bn,
        "ho":  False,
        "po":  False,
        "hm":  False,
        "hp":  False,
        "pm":  pm,
        "mt":  mt,
        "tr":  tr,
        "ac":  False,
        "tp":  False,
    }


# ── Lesson definitions ───────────────────────────────────────────────────────

# All three lessons share:
#   - 80 BPM, 4/4 time
#   - 4-bar count-in (silent on the player side, but the click + drone are
#     audible so the player can lock in to the tempo)
#   - 16 bars total (the 4-bar count-in + 12 bars of exercise = ~48 s)
BPM = 80.0
BEATS_PER_BAR = 4
BAR_DURATION = BEATS_PER_BAR * (60.0 / BPM)   # 3.0 s at 80 BPM
LESSON_BARS = 16
LESSON_DURATION = LESSON_BARS * BAR_DURATION  # 48.0 s
COUNT_IN_BARS = 4
EXERCISE_START_BAR = COUNT_IN_BARS            # exercise begins at bar 4 (zero-indexed)


def bar_time(bar: int, beat: float = 0.0) -> float:
    """Convert a (bar, beat) pair to seconds. Bars and beats are zero-indexed."""
    return (bar + beat / BEATS_PER_BAR) * BAR_DURATION


def lesson_1_notes() -> list[dict]:
    """Half-step bends. Each phrase is a 1-beat bend → 1-beat sustain → 2-beat
    rest, on a different note each 4-bar block. Positions chosen because
    they're the canonical intro-bend frets every guitar method covers.

        Bars  4– 7: G string (RS s=3) fret 7  = D4  bent +1 → Eb4
        Bars  8–11: B string (RS s=4) fret 8  = G4  bent +1 → Ab4
        Bars 12–15: high E (RS s=5) fret 7    = B4  bent +1 → C5
    """
    notes: list[dict] = []
    blocks = [
        (3, 7),   # G string fret 7  → D4
        (4, 8),   # B string fret 8  → G4
        (5, 7),   # high E fret 7    → B4
    ]
    bend_amt = 1.0  # half step
    for block_idx, (string, fret) in enumerate(blocks):
        start_bar = EXERCISE_START_BAR + block_idx * 4
        for bar in range(start_bar, start_bar + 4):
            # Beat 0: pluck the source note
            # Beat 1-2: bend held (modelled with a single sustain note whose
            #           `bn` is the peak bend amount; the player holds for 2
            #           beats then releases)
            notes.append(note(
                t=bar_time(bar, 0.0),
                s=string, f=fret,
                sus=2.0 * 60.0 / BPM,   # 2 beats of sustain
                bn=bend_amt,
            ))
    return notes


def lesson_2_notes() -> list[dict]:
    """Full-step bends. Same canonical positions as L1, but +2 semitones and
    two bends per bar (on beats 1 and 3)."""
    notes: list[dict] = []
    blocks = [
        (3, 7),   # G fret 7  → D4 bent to E4
        (4, 8),   # B fret 8  → G4 bent to A4
        (5, 7),   # high E fret 7 → B4 bent to C#5
    ]
    bend_amt = 2.0  # full step
    for block_idx, (string, fret) in enumerate(blocks):
        start_bar = EXERCISE_START_BAR + block_idx * 4
        for bar in range(start_bar, start_bar + 4):
            for beat in (0.0, 2.0):
                notes.append(note(
                    t=bar_time(bar, beat),
                    s=string, f=fret,
                    sus=1.5 * 60.0 / BPM,  # 1.5 beats — small gap between bends
                    bn=bend_amt,
                ))
    return notes


def lesson_3_notes() -> list[dict]:
    """Bend + vibrato. Slow sustained full-step bends (4 beats each), one per
    bar. The player adds vibrato while holding the bend; the sloppak schema
    has no explicit vibrato flag, but the pitch tracker scores the held
    target pitch so vibrato counts as the player keeping the bend in pocket.
    """
    notes: list[dict] = []
    pattern = [
        (3, 7),   # D4 → E4 (full-step)
        (4, 8),   # G4 → A4
        (5, 7),   # B4 → C#5
        (3, 7),   # back to D4 → E4
    ]
    bend_amt = 2.0
    for i in range(LESSON_BARS - EXERCISE_START_BAR):
        bar = EXERCISE_START_BAR + i
        string, fret = pattern[i % len(pattern)]
        notes.append(note(
            t=bar_time(bar, 0.0),
            s=string, f=fret,
            sus=3.5 * 60.0 / BPM,  # almost a full bar of sustain
            bn=bend_amt,
        ))
    return notes


LESSONS = [
    {
        "id":          "l1",
        "filename":    "Tutorial_Intro_to_Bends_L1",
        "title":       "Intro to Bends — L1 Half-step bends",
        "tonic_midi":  50,                  # D3 — bass holds this on beat 1
        "build_notes": lesson_1_notes,
    },
    {
        "id":          "l2",
        "filename":    "Tutorial_Intro_to_Bends_L2",
        "title":       "Intro to Bends — L2 Full-step bends",
        "tonic_midi":  50,
        "build_notes": lesson_2_notes,
    },
    {
        "id":          "l3",
        "filename":    "Tutorial_Intro_to_Bends_L3",
        "title":       "Intro to Bends — L3 Bend + vibrato",
        "tonic_midi":  50,
        "build_notes": lesson_3_notes,
    },
]


# ── MIDI rendering ───────────────────────────────────────────────────────────

# General MIDI program numbers (1-based in spec, 0-based in mido).
GM_OVERDRIVEN_GUITAR = 29  # program 30 in GM tables
GM_ELECTRIC_BASS     = 33  # program 34 — Electric Bass (finger)

# Pitch-bend hardware uses a 14-bit value; mido's `pitchwheel` takes a
# signed int in [-8192, 8191] where 0 = no bend. The default pitch-bend
# range is ±2 semitones, so one full-step bend = 8191 and one half-step =
# 4096.  We send a quick ramp (≈250 ms) into the bend and another quick
# ramp out before the next pluck so the listener hears the bend journey,
# not an instant pitch jump.
BEND_RAMP_MS = 250
SOUNDFONT_PATH = "/usr/share/soundfonts/FluidR3_GM.sf2"
TICKS_PER_BEAT = 480  # MIDI division (PPQN)


def _sec_to_ticks(seconds: float) -> int:
    """Convert wall-clock seconds to MIDI ticks at the lesson tempo."""
    return int(round(seconds * BPM / 60.0 * TICKS_PER_BEAT))


def _midi_for_note(arr_note: dict) -> int:
    """MIDI pitch of an arrangement note (pre-bend — the note-on pitch)."""
    return midi_of(arr_note["s"], arr_note["f"])


def _pitch_bend_value(semitones: float) -> int:
    """Map a desired semitone offset to mido's pitchwheel int.
    Pitch-bend range is ±2 semis, so the scale factor is 8192 / 2 = 4096."""
    return max(-8192, min(8191, int(round(semitones * 4096))))


def build_midi(notes: list[dict], tonic_midi: int):
    """Assemble a 3-track MIDI file: lead with pitch-bent bends, bass on the
    downbeat, drums providing the backbeat through the whole lesson
    (including the count-in)."""
    import mido

    mid = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)

    # ── Tempo track ─────────────────────────────────────────────────────
    tempo_track = mido.MidiTrack()
    mid.tracks.append(tempo_track)
    tempo_track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(BPM), time=0))
    tempo_track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))

    # Each track builds a list of (abs_tick, message) tuples; at the end we
    # sort by tick and convert to delta-time messages.
    def _commit(track: "mido.MidiTrack", events: list[tuple[int, "mido.Message"]]) -> None:
        events.sort(key=lambda ev: (ev[0], 0 if ev[1].type.startswith("note_off") else 1))
        prev = 0
        for tick, msg in events:
            msg.time = max(0, tick - prev)
            track.append(msg)
            prev = tick

    # ── Drums (channel 9 in zero-based mido; "channel 10" in spec terms) ───
    drum_track = mido.MidiTrack()
    mid.tracks.append(drum_track)
    drum_events: list[tuple[int, "mido.Message"]] = []
    KICK, SNARE, HAT = 36, 38, 42
    for bar in range(LESSON_BARS):
        for beat in range(BEATS_PER_BAR):
            t = _sec_to_ticks(bar_time(bar, beat))
            # Hi-hat on every beat (with extra accent on the downbeat).
            hat_vel = 80 if beat == 0 else 55
            drum_events.append((t,                  mido.Message("note_on",  channel=9, note=HAT,   velocity=hat_vel)))
            drum_events.append((t + TICKS_PER_BEAT // 2,
                                                    mido.Message("note_off", channel=9, note=HAT,   velocity=0)))
            # Kick on 1 and 3, snare on 2 and 4 — the standard rock backbeat.
            if beat in (0, 2):
                drum_events.append((t,              mido.Message("note_on",  channel=9, note=KICK,  velocity=100)))
                drum_events.append((t + TICKS_PER_BEAT // 4,
                                                    mido.Message("note_off", channel=9, note=KICK,  velocity=0)))
            else:
                drum_events.append((t,              mido.Message("note_on",  channel=9, note=SNARE, velocity=90)))
                drum_events.append((t + TICKS_PER_BEAT // 4,
                                                    mido.Message("note_off", channel=9, note=SNARE, velocity=0)))
    _commit(drum_track, drum_events)

    # ── Bass: tonic root on beat 1 of every bar ───────────────────────────
    bass_track = mido.MidiTrack()
    mid.tracks.append(bass_track)
    bass_track.append(mido.Message("program_change", channel=1, program=GM_ELECTRIC_BASS, time=0))
    bass_events: list[tuple[int, "mido.Message"]] = []
    for bar in range(LESSON_BARS):
        t = _sec_to_ticks(bar_time(bar, 0.0))
        bass_events.append((t,                          mido.Message("note_on",  channel=1, note=tonic_midi - 12, velocity=85)))
        bass_events.append((t + 2 * TICKS_PER_BEAT,     mido.Message("note_off", channel=1, note=tonic_midi - 12, velocity=0)))
    _commit(bass_track, bass_events)

    # ── Lead: bend notes from the highway arrangement ─────────────────────
    lead_track = mido.MidiTrack()
    mid.tracks.append(lead_track)
    lead_track.append(mido.Message("program_change", channel=0, program=GM_OVERDRIVEN_GUITAR, time=0))
    lead_events: list[tuple[int, "mido.Message"]] = []
    # Reset pitch wheel to center before the first note in case any host
    # remembers a stale value.
    lead_events.append((0, mido.Message("pitchwheel", channel=0, pitch=0)))

    ramp_ticks = _sec_to_ticks(BEND_RAMP_MS / 1000.0)

    for n in notes:
        t_on    = _sec_to_ticks(n["t"])
        sus_t   = _sec_to_ticks(max(0.05, n["sus"]))
        t_off   = t_on + sus_t
        pitch   = _midi_for_note(n)
        velocity = 100

        # Pre-emptive pitch-wheel reset just before the note-on (helps when
        # consecutive notes have bends and we're not exactly at center).
        lead_events.append((max(0, t_on - 1),
                            mido.Message("pitchwheel", channel=0, pitch=0)))
        lead_events.append((t_on, mido.Message("note_on", channel=0, note=pitch, velocity=velocity)))

        bend_amt = n.get("bn", 0.0)
        if bend_amt and bend_amt > 0:
            # Ramp up over BEND_RAMP_MS; ramp down in the last BEND_RAMP_MS
            # of the sustain. Use 8 intermediate steps for a smooth curve.
            steps = 8
            peak  = _pitch_bend_value(bend_amt)
            up_end = min(t_on + ramp_ticks, t_off)
            for i in range(1, steps + 1):
                frac = i / steps
                tick = int(t_on + frac * (up_end - t_on))
                value = int(peak * frac)
                lead_events.append((tick, mido.Message("pitchwheel", channel=0, pitch=value)))
            down_start = max(up_end, t_off - ramp_ticks)
            for i in range(1, steps + 1):
                frac = i / steps
                tick = int(down_start + frac * (t_off - down_start))
                value = int(peak * (1 - frac))
                lead_events.append((tick, mido.Message("pitchwheel", channel=0, pitch=value)))

        lead_events.append((t_off, mido.Message("note_off", channel=0, note=pitch, velocity=0)))
        # Snap back to center after the release.
        lead_events.append((t_off + 1, mido.Message("pitchwheel", channel=0, pitch=0)))

    _commit(lead_track, lead_events)

    return mid


# ── Audio rendering ──────────────────────────────────────────────────────────

def render_audio(out_mp3: Path, tonic_midi: int, notes: list[dict]) -> None:
    """Render the lesson backing as a MIDI arrangement (lead with pitch-bent
    bends, bass on the downbeat, drums on the backbeat) routed through
    fluidsynth + FluidR3_GM.sf2 and encoded to MP3."""
    if not Path(SOUNDFONT_PATH).is_file():
        raise SystemExit(f"Soundfont not found at {SOUNDFONT_PATH} — install FluidR3 or edit SOUNDFONT_PATH.")

    mid = build_midi(notes, tonic_midi)
    with tempfile.TemporaryDirectory(prefix="tut-audio-") as tmp:
        tmp_dir   = Path(tmp)
        midi_path = tmp_dir / "lesson.mid"
        wav_path  = tmp_dir / "lesson.wav"
        mid.save(str(midi_path))

        # fluidsynth: render the MIDI to a stereo 44.1 kHz wav.
        # -F = output file, -r = sample rate, -g = master gain.
        subprocess.run([
            "fluidsynth",
            "-ni",  # non-interactive, no shell
            "-F", str(wav_path),
            "-r", "44100",
            "-g", "0.8",
            SOUNDFONT_PATH,
            str(midi_path),
        ], check=True, capture_output=True)

        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-q:a", "4",
            "-t", f"{LESSON_DURATION:.3f}",   # cap to declared duration
            str(out_mp3),
        ], check=True)


# ── Sloppak assembly ─────────────────────────────────────────────────────────

def build_arrangement(notes: list[dict]) -> dict:
    """The minimal-but-complete arrangement document. Beats/sections drive UI
    counters; templates is empty because we emit pure single notes (no chord
    shapes)."""
    # Sloppak schema uses {"time": <s>, "measure": <int>} for beats —
    # measure is the 1-based downbeat index, -1 for subdivisions. Same
    # for sections — `time` (not `t`). Wrong keys make sloppak.py default
    # everything to time=0 / measure=-1, which the tabview renderer turns
    # into an all-rests chart.
    beats = []
    measure_num = 0
    for b in range(LESSON_BARS):
        for beat in range(BEATS_PER_BAR):
            if beat == 0:
                measure_num += 1
            beats.append({
                "time":    round(bar_time(b, beat), 3),
                "measure": measure_num if beat == 0 else -1,
            })
    sections = [
        {"name": "Count-in", "number": 1, "time": 0.0},
        {"name": "Exercise", "number": 1, "time": bar_time(EXERCISE_START_BAR)},
    ]
    return {
        "name":       "Lead",
        "tuning":     [0, 0, 0, 0, 0, 0],
        "capo":       0,
        "notes":      notes,
        "chords":     [],
        "anchors":    [],
        "handshapes": [],
        "templates":  [],
        "beats":      beats,
        "sections":   sections,
    }


def build_manifest(title: str) -> dict:
    return {
        "title":    title,
        "artist":   "Slopsmith Tutorials",
        "album":    "Intro to Bends",
        "year":     2026,
        "duration": LESSON_DURATION,
        "stems": [
            {"id": "audio", "file": "stems/audio.mp3", "default": "on"},
        ],
        "arrangements": [
            {
                "id":     "lead",
                "name":   "Lead",
                "file":   "arrangements/lead.json",
                "tuning": [0, 0, 0, 0, 0, 0],
                "capo":   0,
            },
        ],
    }


def write_sloppak(out_path: Path, manifest: dict, arrangement: dict, mp3_path: Path) -> None:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        zf.writestr("arrangements/lead.json", json.dumps(arrangement, indent=2))
        zf.write(mp3_path, arcname="stems/audio.mp3")


# ── Entry point ──────────────────────────────────────────────────────────────

def _render_panel(
    out_path: Path,
    title: str,
    subtitle: str,
    gradient_top: tuple[int, int, int],
    gradient_bot: tuple[int, int, int],
    title_pt: int,
    subtitle_pt: int,
    size: tuple[int, int] = (1280, 720),
) -> None:
    """Internal helper: render a title panel with a vertical gradient and six
    decorative 'string' lines. Used for the pack cover and per-lesson thumbs.
    Falls back to the default font when DejaVu isn't installed."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = size
    img = Image.new("RGB", (W, H), gradient_top)
    draw = ImageDraw.Draw(img)

    for y in range(H):
        f = y / H
        r = int(gradient_top[0] + (gradient_bot[0] - gradient_top[0]) * f)
        g = int(gradient_top[1] + (gradient_bot[1] - gradient_top[1]) * f)
        b = int(gradient_top[2] + (gradient_bot[2] - gradient_top[2]) * f)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    for i in range(6):
        y = int(H * (0.55 + i * 0.06))
        alpha = 0.7 - i * 0.1
        col = tuple(int(122 + (255 - 122) * alpha) for _ in range(3))
        draw.line([(W * 0.06, y), (W * 0.94, y)], fill=col, width=max(1, 4 - i // 2))

    try:
        font_path = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
        title_font    = ImageFont.truetype(font_path, title_pt)
        subtitle_font = ImageFont.truetype(font_path.replace("-Bold", ""), subtitle_pt)
    except (OSError, IOError):
        title_font    = ImageFont.load_default()
        subtitle_font = ImageFont.load_default()

    pad_x = int(W * 0.07)
    draw.text((pad_x,     int(H * 0.30)), title,    fill=(230, 234, 242), font=title_font)
    draw.text((pad_x + 2, int(H * 0.46)), subtitle, fill=(140, 147, 163), font=subtitle_font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)


def render_cover(out_path: Path, title: str, subtitle: str) -> None:
    """Pack-level cover at 1280×720."""
    _render_panel(
        out_path, title, subtitle,
        gradient_top=(20, 28, 50), gradient_bot=(60, 40, 90),
        title_pt=96, subtitle_pt=42,
    )


# Per-lesson thumb palettes — hue-shifted variants of the pack cover so each
# lesson is visually distinct on the lesson list while still feeling like a
# matching set.
LESSON_PALETTES = [
    {"top": (24, 36, 56), "bot": (70, 52, 100)},   # indigo → violet
    {"top": (40, 30, 50), "bot": (100, 60, 80)},   # plum → mauve
    {"top": (20, 44, 50), "bot": (54, 90, 90)},    # teal → cyan
]


def render_lesson_thumb(out_path: Path, lesson_index: int, title: str, subtitle: str) -> None:
    """Per-lesson 800×450 thumb. Palette cycles through LESSON_PALETTES so
    each row in the lesson list reads as its own card."""
    palette = LESSON_PALETTES[lesson_index % len(LESSON_PALETTES)]
    _render_panel(
        out_path,
        f"L{lesson_index + 1} · {title}",
        subtitle,
        gradient_top=palette["top"], gradient_bot=palette["bot"],
        title_pt=56, subtitle_pt=28,
        size=(800, 450),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "sloppaks",
                        help="Directory to write the .sloppak files into.")
    parser.add_argument("--dlc", type=Path, default=Path.home() / ".local/share/Steam/steamapps/common/Rocksmith2014/dlc",
                        help="Library / DLC directory to also copy the sloppaks into (set to '-' to skip).")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    builtin_dir = Path(__file__).resolve().parent
    runtime_pack_dir = Path.home() / ".config/slopsmith/tutorials/packs/intro-bends"

    # Pack-level cover (one per pack). Written into the builtin pack dir so
    # _seed_builtin_packs picks it up on first run, and into the already-
    # seeded runtime pack dir so the change is visible without a re-seed.
    cover_path = builtin_dir / "cover.png"
    try:
        render_cover(cover_path, "Intro to Bends", "3 short lessons · half-step → vibrato")
        print(f"  wrote pack cover at {cover_path}")
        if runtime_pack_dir.is_dir():
            shutil.copy2(cover_path, runtime_pack_dir / "cover.png")
            print(f"  copied cover to runtime: {runtime_pack_dir / 'cover.png'}")
    except Exception as e:
        print(f"  (skipped cover render: {e})")

    # Per-lesson thumbs.
    thumbs_builtin = builtin_dir / "thumbs"
    thumbs_builtin.mkdir(parents=True, exist_ok=True)
    runtime_thumbs = runtime_pack_dir / "thumbs"
    if runtime_pack_dir.is_dir():
        runtime_thumbs.mkdir(parents=True, exist_ok=True)
    for idx, lesson in enumerate(LESSONS):
        thumb_path = thumbs_builtin / f"{lesson['id']}.png"
        try:
            render_lesson_thumb(
                thumb_path, idx,
                # Strip the "Intro to Bends — Lx " prefix so the thumb shows
                # just the lesson-specific phrase.
                lesson["title"].split("—", 1)[-1].strip().split(" ", 1)[-1] if " " in lesson["title"] else lesson["title"],
                f"{int(LESSON_DURATION)}s · 80 BPM",
            )
            print(f"  wrote thumb {thumb_path}")
            if runtime_thumbs.is_dir():
                shutil.copy2(thumb_path, runtime_thumbs / f"{lesson['id']}.png")
        except Exception as e:
            print(f"  (skipped thumb for {lesson['id']}: {e})")

    for lesson in LESSONS:
        sloppak_name = f"{lesson['filename']}.sloppak"
        out_path = args.out / sloppak_name
        print(f"Building {sloppak_name}...")

        # Build the note chart first so the audio renderer can hear what
        # the player is asked to play — the lead guitar track mirrors it.
        notes = lesson["build_notes"]()
        arrangement = build_arrangement(notes)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
            tmp_mp3_path = Path(tmp_mp3.name)
        try:
            render_audio(tmp_mp3_path, lesson["tonic_midi"], notes)

            manifest = build_manifest(lesson["title"])
            write_sloppak(out_path, manifest, arrangement, tmp_mp3_path)
            print(f"  wrote {out_path} ({out_path.stat().st_size} bytes)")
        finally:
            try:
                tmp_mp3_path.unlink()
            except OSError:
                pass

        # Copy into the tutorials-builtin subdirectory so the regenerated
        # sloppak is immediately playable via playSong.  The manifest uses
        # DLC-relative paths of the form tutorials-builtin/intro-bends/<file>,
        # so the copy target must match that subdirectory structure.
        if str(args.dlc) != "-" and args.dlc.is_dir():
            target_dir = args.dlc / "tutorials-builtin" / "intro-bends"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / sloppak_name
            shutil.copy2(out_path, target)
            print(f"  copied to {target}")
        else:
            print(f"  skipping DLC copy (dlc={args.dlc})")


if __name__ == "__main__":
    main()
