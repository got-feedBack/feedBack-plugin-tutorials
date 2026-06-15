"""Generate the "Reading the Highway" tutorial pack — 10 lessons that teach
the sloppak/highway notation, one notation type per lesson, plus a finale.

Each lesson is built from:
  - A SongTemplate (BPM, key, chord progression, drum pattern, GM programs)
  - A `build_notes(template)` callable that emits the lesson's note chart
    using the chord progression as harmonic context.

build_backing_midi(template, total_bars) renders the drums / bass /
rhythm guitar tracks from the template; the lead track is the lesson
notes. The two MIDI streams are merged into one file, rendered through
fluidsynth + FluidR3_GM.sf2, then encoded to MP3.

Format references (same as the intro-bends pack):
  - ~/Repositories/slopsmith/lib/sloppak.py  sloppak layout
  - ~/Repositories/slopsmith/lib/gp2rs.py    RS string index convention:
        0 = LOW E, 5 = HIGH E (per gp2rs.py:459).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import yaml
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ── Musical primitives ───────────────────────────────────────────────────────

OPEN_MIDI = {
    0: 40,  # E2 — low E (RS string 0)
    1: 45,  # A2
    2: 50,  # D3
    3: 55,  # G3
    4: 59,  # B3
    5: 64,  # E4 — high E (RS string 5)
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_of(string: int, fret: int) -> int:
    return OPEN_MIDI[string] + fret


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


# ── Note primitive (sloppak v1 schema) ───────────────────────────────────────

def note(t: float, s: int, f: int, sus: float = 0.0, bn: float = 0.0,
         sl: int = -1, ho: bool = False, po: bool = False,
         hm: bool = False, hp: bool = False, pm: bool = False,
         mt: bool = False, tr: bool = False, ac: bool = False,
         tp: bool = False) -> dict:
    """Build a single arrangement note. Every field present (the
    consumer is strict on missing keys for some flags)."""
    return {
        "t":   round(t, 3),
        "s":   s,
        "f":   f,
        "sus": round(sus, 3),
        "sl":  sl,
        "slu": -1,
        "bn":  bn,
        "ho":  ho,
        "po":  po,
        "hm":  hm,
        "hp":  hp,
        "pm":  pm,
        "mt":  mt,
        "tr":  tr,
        "ac":  ac,
        "tp":  tp,
    }


def chord_note(s: int, f: int, sus: float = 0.0, **kwargs) -> dict:
    """A note inside a chord event — same schema as note() but with no `t`
    field (the chord provides timing)."""
    n = note(0.0, s, f, sus=sus, **kwargs)
    n.pop("t", None)
    return n


def chord_event(t: float, template_id: int, notes_list: list[dict],
                hd: bool = False) -> dict:
    """A chord event drawn as a single grouped 'box' on the highway.
    `template_id` indexes into the arrangement's `templates` array."""
    return {"t": round(t, 3), "id": int(template_id), "hd": hd, "notes": notes_list}


def chord_template(frets: list[int], *, name: str = "",
                   fingers: list[int] | None = None) -> dict:
    """A chord-shape template. `frets[i]` is the fret on RS string i
    (-1 = unplayed). `fingers[i]` mirrors that for fingering (-1 = none),
    1..4 = index..pinky, 0 = thumb."""
    if fingers is None:
        fingers = [-1] * 6
    return {"name": name, "frets": frets, "fingers": fingers}


# ── Chord library ────────────────────────────────────────────────────────────
#
# Each chord maps a name → (root_midi, voicing_midis).  Voicings are written
# in standard guitar register so the rhythm guitar plays them in a believable
# range. Root midi is what the bass anchors on. Names follow common chord
# nomenclature: "Am" = A minor triad, "E5" = E power chord, "Cmaj7" = C major 7.

CHORD_LIBRARY = {
    # Major triads
    "A":  (45, [45, 49, 52, 57, 61, 64]),
    "B":  (47, [47, 51, 54, 59, 63, 66]),
    "C":  (48, [48, 52, 55, 60, 64, 67]),
    "D":  (50, [50, 54, 57, 62, 66, 69]),
    "E":  (40, [40, 44, 47, 52, 56, 59]),
    "F":  (41, [41, 45, 48, 53, 57, 60]),
    "G":  (43, [43, 47, 50, 55, 59, 62]),

    # Minor triads
    "Am": (45, [45, 48, 52, 57, 60, 64]),
    "Bm": (47, [47, 50, 54, 59, 62, 66]),
    "Cm": (48, [48, 51, 55, 60, 63, 67]),
    "Dm": (50, [50, 53, 57, 62, 65, 69]),
    "Em": (40, [40, 43, 47, 52, 55, 59]),
    "Fm": (41, [41, 44, 48, 53, 56, 60]),
    "Gm": (43, [43, 46, 50, 55, 58, 62]),

    # Dominant 7ths (blues / shuffles)
    "A7":  (45, [45, 49, 52, 55, 61, 64]),
    "D7":  (50, [50, 54, 57, 60, 66, 69]),
    "E7":  (40, [40, 44, 47, 50, 56, 59]),
    "G7":  (43, [43, 47, 50, 53, 59, 62]),
    "Am7": (45, [45, 48, 52, 55, 60, 64]),
    "Dm7": (50, [50, 53, 57, 60, 65, 69]),
    "Em7": (40, [40, 43, 47, 50, 55, 59]),
    "Gm7": (43, [43, 46, 50, 53, 58, 62]),

    # Maj7 (mellow funk / ambient)
    "Cmaj7": (48, [48, 52, 55, 59, 64, 67]),
    "Gmaj7": (43, [43, 47, 50, 54, 59, 62]),

    # Add9 / sus (ambient)
    "Cadd9": (48, [48, 52, 55, 60, 62, 67]),
    "G":     (43, [43, 47, 50, 55, 59, 62]),  # repeats — fine

    # Power chords (rock / metal)
    "A5": (45, [45, 52, 57]),
    "B5": (47, [47, 54, 59]),
    "C5": (48, [48, 55, 60]),
    "C#5":(49, [49, 56, 61]),
    "D5": (50, [50, 57, 62]),
    "E5": (40, [40, 47, 52]),
    "F5": (41, [41, 48, 53]),
    "G5": (43, [43, 50, 55]),

    # Diminished (just one used)
    "G#m": (44, [44, 47, 51, 56, 59, 63]),
}


# ── SongTemplate ─────────────────────────────────────────────────────────────

# Drum kit GM notes (channel 9 / MIDI channel 10 in spec terms).
KICK         = 36
SNARE        = 38
SIDESTICK    = 37
CLOSED_HAT   = 42
PEDAL_HAT    = 44
OPEN_HAT     = 46
RIDE         = 51
CRASH        = 49
TOM_LOW      = 45
TOM_MID      = 47
TOM_HIGH     = 50


@dataclass
class SongTemplate:
    """A complete musical context for one lesson's backing track."""
    bpm: float
    progression: list[str]       # bar-by-bar chord names from CHORD_LIBRARY
    drum_pattern: str            # name of a function in DRUM_PATTERNS
    rhythm_program: int          # GM program number (0-127)
    bass_program: int = 33       # Electric Bass (finger) by default
    lead_program: int = 29       # Overdriven Guitar by default
    bass_style: str  = "root_pulse"  # 'root_pulse' | 'walking' | 'sparse_root'
    rhythm_style: str = "strum_quarters"  # 'strum_quarters' | 'arpeggio' | 'stab_2_4' | 'palm_8ths'
    count_in_bars: int = 4       # silent-lead count-in
    exercise_bars: int = 12      # bars of lead activity

    @property
    def total_bars(self) -> int:
        return self.count_in_bars + self.exercise_bars

    @property
    def bar_duration(self) -> float:
        return 4 * (60.0 / self.bpm)  # 4/4 only for v1

    @property
    def total_duration(self) -> float:
        return self.total_bars * self.bar_duration


# ── Helpers ──────────────────────────────────────────────────────────────────

TICKS_PER_BEAT = 480


def sec_to_ticks(seconds: float, bpm: float) -> int:
    return int(round(seconds * bpm / 60.0 * TICKS_PER_BEAT))


def bar_time(bar: int, beat: float, bar_duration: float) -> float:
    return (bar + beat / 4) * bar_duration


def chord_for_bar(template: SongTemplate, bar: int) -> tuple[str, tuple[int, list[int]]]:
    """Return the (name, (root, voicing)) of the chord active in `bar`.
    The progression loops over the exercise; bars in the count-in use the
    progression's first chord so the band is established before the lead
    starts.  v1 keeps it as 1 chord per bar."""
    if bar < template.count_in_bars:
        idx = 0
    else:
        idx = (bar - template.count_in_bars) % len(template.progression)
    name = template.progression[idx]
    return name, CHORD_LIBRARY[name]


def humanise(velocity: int, rng: random.Random, spread: int = 8) -> int:
    """±spread velocity jitter, clamped to MIDI range. Keeps stiff
    quantised patterns from sounding like a step sequencer."""
    return max(1, min(127, velocity + rng.randint(-spread, spread)))


# ── Drum patterns ────────────────────────────────────────────────────────────
#
# Each pattern emits drum hits for ONE bar, given a list-of-events accumulator
# and the bar's start tick. The pattern is a stylistic preset — combining
# kick/snare/hat placement to evoke a feel.

def _emit(events: list, tick: int, msg: "mido.Message") -> None:
    events.append((tick, msg))


def drum_rock_backbeat(events: list, bar_start: int, rng: random.Random) -> None:
    """Standard rock: kick 1+3, snare 2+4, closed hat 8ths."""
    import mido
    bar_ticks = TICKS_PER_BEAT * 4
    eighth = TICKS_PER_BEAT // 2
    for i in range(8):
        t = bar_start + i * eighth
        _emit(events, t, mido.Message("note_on",  channel=9, note=CLOSED_HAT,
                                      velocity=humanise(55 + (15 if i % 2 == 0 else 0), rng)))
        _emit(events, t + eighth // 2, mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    for beat in (0, 2):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on",  channel=9, note=KICK,  velocity=humanise(100, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=KICK, velocity=0))
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on",  channel=9, note=SNARE, velocity=humanise(95, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_ballad_gentle(events: list, bar_start: int, rng: random.Random) -> None:
    """Slow ballad — kick on 1, snare on 3, hat quarters only. Soft."""
    import mido
    for beat in range(4):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on",  channel=9, note=CLOSED_HAT,
                                      velocity=humanise(45, rng, 5)))
        _emit(events, t + TICKS_PER_BEAT // 3, mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    _emit(events, bar_start, mido.Message("note_on", channel=9, note=KICK, velocity=humanise(80, rng)))
    _emit(events, bar_start + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=KICK, velocity=0))
    _emit(events, bar_start + 2 * TICKS_PER_BEAT, mido.Message("note_on",  channel=9, note=SNARE,
                                                                 velocity=humanise(70, rng)))
    _emit(events, bar_start + 2 * TICKS_PER_BEAT + TICKS_PER_BEAT // 4,
          mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_shuffle(events: list, bar_start: int, rng: random.Random) -> None:
    """Triplet-feel shuffle (blues). Hat plays the swung 'doo-da' pattern."""
    import mido
    # Triplet 8th = TICKS_PER_BEAT / 3
    tt = TICKS_PER_BEAT // 3
    for beat in range(4):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t,             mido.Message("note_on", channel=9, note=CLOSED_HAT, velocity=humanise(70, rng)))
        _emit(events, t + tt,        mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
        _emit(events, t + 2 * tt,    mido.Message("note_on", channel=9, note=CLOSED_HAT, velocity=humanise(50, rng)))
        _emit(events, t + 3 * tt,    mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    for beat in (0, 2):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=KICK, velocity=humanise(100, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=KICK, velocity=0))
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=SNARE, velocity=humanise(90, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_train_beat(events: list, bar_start: int, rng: random.Random) -> None:
    """Country/folk train beat — kick on every beat, snare on 2&4."""
    import mido
    eighth = TICKS_PER_BEAT // 2
    for i in range(8):
        t = bar_start + i * eighth
        _emit(events, t, mido.Message("note_on", channel=9, note=CLOSED_HAT, velocity=humanise(55, rng)))
        _emit(events, t + eighth // 2, mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    for beat in range(4):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=KICK, velocity=humanise(85, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=KICK, velocity=0))
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=SNARE, velocity=humanise(85, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_funk_16(events: list, bar_start: int, rng: random.Random) -> None:
    """16th-note hat with snare 2/4 and syncopated kick. Add ghost snares."""
    import mido
    sixteenth = TICKS_PER_BEAT // 4
    for i in range(16):
        t = bar_start + i * sixteenth
        vel = humanise(60 if i % 4 == 0 else 40, rng)
        _emit(events, t, mido.Message("note_on", channel=9, note=CLOSED_HAT, velocity=vel))
        _emit(events, t + sixteenth // 2, mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    # Kick: 1, 1+&, 3
    kick_positions = [0, 6, 8]  # in 16th-note slots
    for slot in kick_positions:
        t = bar_start + slot * sixteenth
        _emit(events, t, mido.Message("note_on", channel=9, note=KICK, velocity=humanise(100, rng)))
        _emit(events, t + sixteenth // 2, mido.Message("note_off", channel=9, note=KICK, velocity=0))
    # Snare on 2 and 4
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=SNARE, velocity=humanise(95, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SNARE, velocity=0))
    # Ghost snares
    for slot in (3, 11):
        t = bar_start + slot * sixteenth
        _emit(events, t, mido.Message("note_on", channel=9, note=SNARE, velocity=humanise(30, rng, 4)))
        _emit(events, t + sixteenth // 2, mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_ambient(events: list, bar_start: int, rng: random.Random) -> None:
    """Sparse ambient — light cymbal swell + brushed snare on 2 and 4 only."""
    import mido
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=SIDESTICK, velocity=humanise(40, rng, 3)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SIDESTICK, velocity=0))
    # Ride on 1
    _emit(events, bar_start, mido.Message("note_on", channel=9, note=RIDE, velocity=humanise(45, rng)))
    _emit(events, bar_start + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=RIDE, velocity=0))


def drum_metal_8ths(events: list, bar_start: int, rng: random.Random) -> None:
    """Driving metal: kick 8ths, snare 2/4, ride/closed-hat 8ths."""
    import mido
    eighth = TICKS_PER_BEAT // 2
    for i in range(8):
        t = bar_start + i * eighth
        _emit(events, t, mido.Message("note_on", channel=9, note=KICK, velocity=humanise(105, rng)))
        _emit(events, t + eighth // 3, mido.Message("note_off", channel=9, note=KICK, velocity=0))
        _emit(events, t, mido.Message("note_on", channel=9, note=CLOSED_HAT, velocity=humanise(65, rng)))
        _emit(events, t + eighth // 3, mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=SNARE, velocity=humanise(110, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_surf(events: list, bar_start: int, rng: random.Random) -> None:
    """Surf-rock: snare driven, kick on 1, hat 8ths, occasional tom fills."""
    import mido
    eighth = TICKS_PER_BEAT // 2
    for i in range(8):
        t = bar_start + i * eighth
        _emit(events, t, mido.Message("note_on", channel=9, note=CLOSED_HAT, velocity=humanise(60, rng)))
        _emit(events, t + eighth // 2, mido.Message("note_off", channel=9, note=CLOSED_HAT, velocity=0))
    _emit(events, bar_start, mido.Message("note_on", channel=9, note=KICK, velocity=humanise(100, rng)))
    _emit(events, bar_start + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=KICK, velocity=0))
    # Driving snare on 2,3,4
    for beat in (1, 2, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=9, note=SNARE, velocity=humanise(95, rng)))
        _emit(events, t + TICKS_PER_BEAT // 4, mido.Message("note_off", channel=9, note=SNARE, velocity=0))


def drum_half_full_alt(events: list, bar_start: int, rng: random.Random, *, bar_index: int = 0) -> None:
    """Alternates half-time feel and full-time backbeat across bars. Used
    for the finale — feels like a tune that builds. Caller passes the
    bar index via the `bar_index` keyword."""
    if bar_index % 4 < 2:
        drum_ballad_gentle(events, bar_start, rng)
    else:
        drum_rock_backbeat(events, bar_start, rng)


DRUM_PATTERNS: dict[str, Callable] = {
    "rock_backbeat":  drum_rock_backbeat,
    "ballad_gentle":  drum_ballad_gentle,
    "shuffle":        drum_shuffle,
    "train_beat":     drum_train_beat,
    "funk_16":        drum_funk_16,
    "ambient":        drum_ambient,
    "metal_8ths":     drum_metal_8ths,
    "surf":           drum_surf,
    "half_full_alt":  drum_half_full_alt,
}


# ── Bass patterns ────────────────────────────────────────────────────────────

def bass_root_pulse(events: list, bar_start: int, root: int, rng: random.Random) -> None:
    """Root on beat 1, sustained 2 beats."""
    import mido
    _emit(events, bar_start, mido.Message("note_on", channel=1, note=root - 12, velocity=humanise(85, rng)))
    _emit(events, bar_start + 2 * TICKS_PER_BEAT,
          mido.Message("note_off", channel=1, note=root - 12, velocity=0))


def bass_quarter_notes(events: list, bar_start: int, root: int, rng: random.Random) -> None:
    """Root on every beat. Steady eighths feel."""
    import mido
    for beat in range(4):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=1, note=root - 12, velocity=humanise(80, rng)))
        _emit(events, t + int(TICKS_PER_BEAT * 0.85),
              mido.Message("note_off", channel=1, note=root - 12, velocity=0))


def bass_walking(events: list, bar_start: int, root: int, rng: random.Random) -> None:
    """Walking bass — root, 5th, octave, 6th. Works over both major and
    minor chords because we steer clear of the chord 3rd (which differs
    between qualities). Bluesy enough for shuffles, country, etc."""
    import mido
    walk = [root - 12, root - 12 + 7, root, root - 12 + 9]
    for beat, n in enumerate(walk):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=1, note=n, velocity=humanise(80, rng)))
        _emit(events, t + int(TICKS_PER_BEAT * 0.8),
              mido.Message("note_off", channel=1, note=n, velocity=0))


def bass_sparse_root(events: list, bar_start: int, root: int, rng: random.Random) -> None:
    """Sparse / ambient bass — root note sustained for almost the full bar."""
    import mido
    _emit(events, bar_start, mido.Message("note_on", channel=1, note=root - 12, velocity=humanise(70, rng)))
    _emit(events, bar_start + int(TICKS_PER_BEAT * 3.8),
          mido.Message("note_off", channel=1, note=root - 12, velocity=0))


def bass_metal_8ths(events: list, bar_start: int, root: int, rng: random.Random) -> None:
    """Driving 8th-note bass on root, locked to kick."""
    import mido
    eighth = TICKS_PER_BEAT // 2
    for i in range(8):
        t = bar_start + i * eighth
        _emit(events, t, mido.Message("note_on", channel=1, note=root - 12, velocity=humanise(95, rng)))
        _emit(events, t + int(eighth * 0.7),
              mido.Message("note_off", channel=1, note=root - 12, velocity=0))


def bass_funk_16(events: list, bar_start: int, root: int, rng: random.Random) -> None:
    """Funk bass — root on 1, octave on '+', root on 3, fifth on 4+."""
    import mido
    sixteenth = TICKS_PER_BEAT // 4
    hits = [(0, root - 12), (4, root - 12), (8, root - 12), (12, root - 12 + 7)]
    for slot, n in hits:
        t = bar_start + slot * sixteenth
        _emit(events, t, mido.Message("note_on", channel=1, note=n, velocity=humanise(95, rng)))
        _emit(events, t + sixteenth * 2,
              mido.Message("note_off", channel=1, note=n, velocity=0))


BASS_PATTERNS: dict[str, Callable] = {
    "root_pulse":   bass_root_pulse,
    "quarter":      bass_quarter_notes,
    "walking":      bass_walking,
    "sparse_root":  bass_sparse_root,
    "metal_8ths":   bass_metal_8ths,
    "funk_16":      bass_funk_16,
}


# ── Rhythm guitar patterns ───────────────────────────────────────────────────

def rhythm_strum_quarters(events: list, bar_start: int, voicing: list[int], rng: random.Random) -> None:
    """4 strums per bar, each ringing through to the next."""
    import mido
    for beat in range(4):
        t = bar_start + beat * TICKS_PER_BEAT
        for n in voicing:
            _emit(events, t, mido.Message("note_on", channel=2, note=n, velocity=humanise(75, rng, 6)))
            _emit(events, t + int(TICKS_PER_BEAT * 0.95),
                  mido.Message("note_off", channel=2, note=n, velocity=0))


def rhythm_arpeggio(events: list, bar_start: int, voicing: list[int], rng: random.Random) -> None:
    """Arpeggiate the voicing across the bar — one note per beat, low → high."""
    import mido
    notes = voicing[: min(len(voicing), 4)]
    for beat, n in enumerate(notes):
        t = bar_start + beat * TICKS_PER_BEAT
        _emit(events, t, mido.Message("note_on", channel=2, note=n, velocity=humanise(70, rng, 5)))
        _emit(events, t + int(TICKS_PER_BEAT * 0.95),
              mido.Message("note_off", channel=2, note=n, velocity=0))


def rhythm_stab_2_4(events: list, bar_start: int, voicing: list[int], rng: random.Random) -> None:
    """Funk chord stabs — short stabs on beats 2 and 4."""
    import mido
    for beat in (1, 3):
        t = bar_start + beat * TICKS_PER_BEAT
        for n in voicing[:4]:
            _emit(events, t, mido.Message("note_on", channel=2, note=n, velocity=humanise(85, rng, 8)))
            _emit(events, t + int(TICKS_PER_BEAT * 0.25),
                  mido.Message("note_off", channel=2, note=n, velocity=0))


def rhythm_palm_8ths(events: list, bar_start: int, voicing: list[int], rng: random.Random) -> None:
    """Palm-muted 8th-note chunks on the chord's root + fifth (power-chord
    backing for metal). Velocity high, sustain short."""
    import mido
    eighth = TICKS_PER_BEAT // 2
    chunk = voicing[:2]  # root + fifth for power-chord feel
    for i in range(8):
        t = bar_start + i * eighth
        vel = humanise(100 if i % 2 == 0 else 80, rng)
        for n in chunk:
            _emit(events, t, mido.Message("note_on", channel=2, note=n, velocity=vel))
            _emit(events, t + int(eighth * 0.45),
                  mido.Message("note_off", channel=2, note=n, velocity=0))


def rhythm_pad(events: list, bar_start: int, voicing: list[int], rng: random.Random) -> None:
    """Whole-note pad — every chord note sustained the full bar."""
    import mido
    for n in voicing:
        _emit(events, bar_start, mido.Message("note_on", channel=2, note=n, velocity=humanise(55, rng, 3)))
        _emit(events, bar_start + int(TICKS_PER_BEAT * 3.95),
              mido.Message("note_off", channel=2, note=n, velocity=0))


RHYTHM_PATTERNS: dict[str, Callable] = {
    "strum_quarters":  rhythm_strum_quarters,
    "arpeggio":        rhythm_arpeggio,
    "stab_2_4":        rhythm_stab_2_4,
    "palm_8ths":       rhythm_palm_8ths,
    "pad":             rhythm_pad,
}


# ── Backing builder ──────────────────────────────────────────────────────────

def build_backing_midi(template: SongTemplate, lead_events: list, rng_seed: int) -> "mido.MidiFile":
    """Combine drum / bass / rhythm-guitar tracks built from `template`
    with the lesson's `lead_events` (already-built (tick, message) tuples
    on channel 0) into one MidiFile."""
    import mido

    mid = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    rng = random.Random(rng_seed)

    # ── Tempo track ─────────────────────────────────────────────────────
    tempo_track = mido.MidiTrack()
    mid.tracks.append(tempo_track)
    tempo_track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(template.bpm), time=0))
    tempo_track.append(mido.MetaMessage("time_signature",
                                        numerator=4, denominator=4, time=0))

    def commit(track, events):
        # note_off must precede note_on at the same tick to release the
        # voice cleanly before the next attack.
        events.sort(key=lambda ev: (ev[0],
                                    0 if ev[1].type.startswith("note_off") or
                                          (ev[1].type == "note_on" and ev[1].velocity == 0)
                                    else 1))
        prev = 0
        for tick, msg in events:
            msg.time = max(0, tick - prev)
            track.append(msg)
            prev = tick

    # ── Drums (ch 9) ─────────────────────────────────────────────────────
    drum_track = mido.MidiTrack()
    mid.tracks.append(drum_track)
    drum_events: list = []
    drum_fn = DRUM_PATTERNS[template.drum_pattern]
    for bar in range(template.total_bars):
        bar_start = sec_to_ticks(bar_time(bar, 0.0, template.bar_duration), template.bpm)
        # bar_index is consumed by patterns that vary across bars (e.g.
        # the finale's half/full alternator); plain patterns accept the
        # keyword silently because they're declared with **kwargs-friendly
        # signatures (positional-only ignored).
        try:
            drum_fn(drum_events, bar_start, rng, bar_index=bar)
        except TypeError:
            drum_fn(drum_events, bar_start, rng)
    commit(drum_track, drum_events)

    # ── Bass (ch 1) ──────────────────────────────────────────────────────
    bass_track = mido.MidiTrack()
    mid.tracks.append(bass_track)
    bass_track.append(mido.Message("program_change", channel=1,
                                   program=template.bass_program, time=0))
    bass_events: list = []
    bass_fn = BASS_PATTERNS[template.bass_style]
    for bar in range(template.total_bars):
        bar_start = sec_to_ticks(bar_time(bar, 0.0, template.bar_duration), template.bpm)
        _name, (root, _voicing) = chord_for_bar(template, bar)
        bass_fn(bass_events, bar_start, root, rng)
    commit(bass_track, bass_events)

    # ── Rhythm guitar (ch 2) ─────────────────────────────────────────────
    rhythm_track = mido.MidiTrack()
    mid.tracks.append(rhythm_track)
    rhythm_track.append(mido.Message("program_change", channel=2,
                                     program=template.rhythm_program, time=0))
    rhythm_events: list = []
    rhythm_fn = RHYTHM_PATTERNS[template.rhythm_style]
    for bar in range(template.total_bars):
        bar_start = sec_to_ticks(bar_time(bar, 0.0, template.bar_duration), template.bpm)
        _name, (_root, voicing) = chord_for_bar(template, bar)
        rhythm_fn(rhythm_events, bar_start, voicing, rng)
    commit(rhythm_track, rhythm_events)

    # ── Lead (ch 0) ──────────────────────────────────────────────────────
    lead_track = mido.MidiTrack()
    mid.tracks.append(lead_track)
    lead_track.append(mido.Message("program_change", channel=0,
                                   program=template.lead_program, time=0))
    commit(lead_track, list(lead_events))

    return mid


# ── Lead builder (notes → MIDI events with pitch bend support) ──────────────

def midi_notes_from_content(content: dict) -> list[dict]:
    """Flatten a lesson's notes + chord events into a single time-stamped
    note list for MIDI lead rendering. Chord notes inherit the chord's
    `t`; everything else is passed through verbatim."""
    flat = list(content.get("notes", []) or [])
    for ch in content.get("chords", []) or []:
        ch_t = ch.get("t", 0.0)
        for cn in ch.get("notes", []) or []:
            n = dict(cn)
            n["t"] = ch_t
            flat.append(n)
    flat.sort(key=lambda n: (n.get("t", 0.0), n.get("s", 0)))
    return flat


def lead_events_from_notes(notes: list[dict], template: SongTemplate) -> list:
    """Translate sloppak arrangement notes into MIDI events for the lead
    track. Articulation flags shape the audio so a listener can hear what
    each notation sounds like:

    - `bn`  → pitch-bend ramp up/down via channel pitch-wheel.
    - `pm`  → palm mute: ~40% sustain, ~80% velocity.
    - `mt`  → dead note: 60 ms sustain, ~70% velocity (short percussive).
    - `tr`  → tremolo: replace the held note with a stream of 1/32-note
              re-attacks across the sustain window.

    `sl` (slide-to), `ho`/`po` (legato), `hm`/`hp` (harmonics), `tp` (tap)
    are visual-only — they still play the note's base pitch, since
    convincingly faking those in MIDI sounds worse than not."""
    import mido
    events: list = []
    events.append((0, mido.Message("pitchwheel", channel=0, pitch=0)))
    ramp_ticks = sec_to_ticks(0.25, template.bpm)
    tremolo_subdiv = TICKS_PER_BEAT // 4  # 1/16 note — 1/32 sounded buzzy
    for n in notes:
        t_on  = sec_to_ticks(n["t"], template.bpm)
        sus_t = sec_to_ticks(max(0.05, n["sus"]), template.bpm)
        pitch = midi_of(n["s"], n["f"])

        # Articulation adjustments — applied to sustain length + velocity
        # before the events are emitted so all downstream uses agree.
        sus_factor = 1.0
        velocity   = 100
        if n.get("pm"):
            sus_factor = 0.4
            velocity   = 82
        if n.get("mt"):
            sus_t      = sec_to_ticks(0.06, template.bpm)
            sus_factor = 1.0
            velocity   = 68
        sus_t = int(sus_t * sus_factor)
        t_off = t_on + max(sus_t, sec_to_ticks(0.05, template.bpm))

        # Tremolo: chop the note into 1/32-note re-attacks. Skip the
        # standard sustain-and-off path and emit each chop ourselves.
        if n.get("tr"):
            t_cursor = t_on
            while t_cursor < t_off:
                step_off = min(t_cursor + int(tremolo_subdiv * 0.75), t_off)
                events.append((t_cursor, mido.Message("note_on",  channel=0,
                                                       note=pitch, velocity=humanise(95, _RNG))))
                events.append((step_off, mido.Message("note_off", channel=0,
                                                       note=pitch, velocity=0)))
                t_cursor += tremolo_subdiv
            continue

        events.append((max(0, t_on - 1), mido.Message("pitchwheel", channel=0, pitch=0)))
        events.append((t_on, mido.Message("note_on", channel=0, note=pitch, velocity=velocity)))

        bend_amt = float(n.get("bn") or 0.0)
        if bend_amt > 0:
            steps = 8
            peak = max(-8192, min(8191, int(round(bend_amt * 4096))))
            up_end = min(t_on + ramp_ticks, t_off)
            for i in range(1, steps + 1):
                frac = i / steps
                tick = int(t_on + frac * (up_end - t_on))
                events.append((tick, mido.Message("pitchwheel", channel=0, pitch=int(peak * frac))))
            down_start = max(up_end, t_off - ramp_ticks)
            for i in range(1, steps + 1):
                frac = i / steps
                tick = int(down_start + frac * (t_off - down_start))
                events.append((tick, mido.Message("pitchwheel", channel=0, pitch=int(peak * (1 - frac)))))

        events.append((t_off, mido.Message("note_off", channel=0, note=pitch, velocity=0)))
        events.append((t_off + 1, mido.Message("pitchwheel", channel=0, pitch=0)))
    return events


# A dedicated RNG seed for tremolo humanisation — keeps successive
# re-attacks slightly varied without affecting the per-lesson seed used
# for backing drum velocities.
_RNG = random.Random(1337)


# ── Arrangement / manifest / sloppak packaging ───────────────────────────────

def build_arrangement(content: dict, template: SongTemplate, *, name: str = "Lead") -> dict:
    """Wrap the lesson's content (notes + chords + templates) into the
    sloppak v1 arrangement document. `content` is the dict returned by
    a lesson's build_notes(): {"notes": [...], "chords": [...],
    "templates": [...]}."""
    # Real sloppaks use {"time": <s>, "measure": <int>} — downbeats carry a
    # positive measure number (1-based), subdivisions carry -1. Anything
    # else (the keys I had before — "t"/"bar"/"beat") gets silently
    # defaulted to time=0 / measure=-1 by sloppak.py:241, which collapses
    # the whole song into one measureless blob → tabview renders rests.
    beats = []
    measure_num = 0
    for bar in range(template.total_bars):
        for beat in range(4):
            if beat == 0:
                measure_num += 1
            beats.append({
                "time":    round(bar_time(bar, beat, template.bar_duration), 3),
                "measure": measure_num if beat == 0 else -1,
            })
    # Sections use {"name", "number", "time"} — same key fix as beats.
    sections = [
        {"name": "Count-in", "number": 1, "time": 0.0},
        {"name": "Exercise", "number": 1,
         "time": round(template.count_in_bars * template.bar_duration, 3)},
    ]
    return {
        "name":       name,
        "tuning":     [0, 0, 0, 0, 0, 0],
        "capo":       0,
        "notes":      list(content.get("notes", []) or []),
        "chords":     list(content.get("chords", []) or []),
        "anchors":    [],
        "handshapes": [],
        "templates":  list(content.get("templates", []) or []),
        "beats":      beats,
        "sections":   sections,
    }


def build_manifest(title: str, template: SongTemplate) -> dict:
    return {
        "title":    title,
        "artist":   "Slopsmith Tutorials",
        "album":    "Reading the Highway",
        "year":     2026,
        "duration": template.total_duration,
        "stems":    [{"id": "audio", "file": "stems/audio.mp3", "default": "on"}],
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


# ── Audio rendering pipeline ─────────────────────────────────────────────────

SOUNDFONT_PATH = "/usr/share/soundfonts/FluidR3_GM.sf2"


def render_audio(out_mp3: Path, mid: "mido.MidiFile", duration_sec: float) -> None:
    if not Path(SOUNDFONT_PATH).is_file():
        raise SystemExit(f"Soundfont not found at {SOUNDFONT_PATH}")
    with tempfile.TemporaryDirectory(prefix="rth-audio-") as tmp:
        tmp_dir   = Path(tmp)
        midi_path = tmp_dir / "lesson.mid"
        wav_path  = tmp_dir / "lesson.wav"
        mid.save(str(midi_path))
        subprocess.run([
            "fluidsynth", "-ni",
            "-F", str(wav_path),
            "-r", "44100",
            "-g", "0.7",
            SOUNDFONT_PATH,
            str(midi_path),
        ], check=True, capture_output=True)
        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-q:a", "4",
            "-t", f"{duration_sec:.3f}",
            str(out_mp3),
        ], check=True)


# ── Thumbnails ───────────────────────────────────────────────────────────────
#
# Each lesson's thumb is a mini stylised "highway frame" showing the
# notation glyph the lesson teaches. Background is the same gradient
# palette as the pack cover so the thumbs read as a matching set.

def _try_load_font(pt: int, bold: bool = False):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, pt)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _gradient_bg(img, top, bot):
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    W, H = img.size
    for y in range(H):
        f = y / H
        r = int(top[0] + (bot[0] - top[0]) * f)
        g = int(top[1] + (bot[1] - top[1]) * f)
        b = int(top[2] + (bot[2] - top[2]) * f)
        draw.line([(0, y), (W, y)], fill=(r, g, b))


def _draw_highway_strings(img, x_left: float, x_right: float, y_top: float, y_bot: float):
    """Six vertical 'strings' running top-to-bottom in the panel area."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    for i in range(6):
        x = int(x_left + (x_right - x_left) * (i / 5))
        col = (190, 195, 210) if i in (0, 5) else (150, 156, 175)
        draw.line([(x, int(y_top)), (x, int(y_bot))], fill=col, width=2)


def _draw_gem(draw, cx, cy, *, r=28, color=(122, 168, 255), text=None, text_color=(13, 15, 20)):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color, outline=(255, 255, 255), width=2)
    if text is not None:
        font = _try_load_font(int(r * 1.1), bold=True)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - 2), text, fill=text_color, font=font)


def _draw_title(img, title: str, sub: str | None):
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    W, H = img.size
    tf = _try_load_font(40, bold=True)
    sf = _try_load_font(22)
    draw.text((40, H - 90), title, fill=(232, 236, 244), font=tf)
    if sub:
        draw.text((42, H - 44), sub, fill=(160, 168, 188), font=sf)


def _render_thumb_base(notation: str, lesson_idx: int, title: str, sub: str | None,
                       palette: tuple[tuple[int, int, int], tuple[int, int, int]],
                       draw_glyph: Callable):
    from PIL import Image, ImageDraw
    W, H = 800, 450
    img = Image.new("RGB", (W, H), palette[0])
    _gradient_bg(img, palette[0], palette[1])

    # Highway frame area: top-right region where the notation lives.
    string_area = (W * 0.55, W * 0.92, H * 0.10, H * 0.85)
    _draw_highway_strings(img, *string_area)

    draw = ImageDraw.Draw(img)
    draw_glyph(draw, string_area, _try_load_font)

    _draw_title(img, title, sub)
    return img


# ── Per-notation glyph drawers ───────────────────────────────────────────────
#
# Each takes the PIL ImageDraw + the (x_left, x_right, y_top, y_bot) area
# where strings are drawn, and renders the lesson-specific symbol on top.
#
# String x-coordinates (RS convention 0=low E .. 5=high E): for the thumb we
# show strings left-to-right as low → high, mirroring the file's `s` index.

def _string_x(string_idx: int, x_left: float, x_right: float) -> int:
    return int(x_left + (x_right - x_left) * (string_idx / 5))


def glyph_notes(draw, area, font_fn):
    """Single gem + sustain trail on the G string."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(3, x_l, x_r)  # G string
    cy = int(y_t + (y_b - y_t) * 0.45)
    # Sustain trail
    draw.rounded_rectangle((x - 10, cy, x + 10, int(y_b)), radius=8,
                           fill=(122, 168, 255, 160))
    _draw_gem(draw, x, cy, r=26, text="7")


def glyph_chords(draw, area, font_fn):
    """Three gems stacked — a chord shape."""
    x_l, x_r, y_t, y_b = area
    rows = [(2, 0.30, "5"), (3, 0.45, "5"), (4, 0.60, "5")]
    for s, frac, fret in rows:
        x = _string_x(s, x_l, x_r)
        cy = int(y_t + (y_b - y_t) * frac)
        _draw_gem(draw, x, cy, r=24, text=fret)


def glyph_bends(draw, area, font_fn):
    """Gem with a curved upward arrow."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(3, x_l, x_r)
    cy = int(y_t + (y_b - y_t) * 0.55)
    _draw_gem(draw, x, cy, r=26, text="7")
    # Curved arrow up-and-to-the-right
    draw.arc([(x + 10, cy - 80), (x + 90, cy + 10)],
             start=90, end=180, fill=(245, 195, 90), width=5)
    # Arrowhead
    draw.polygon([(x + 88, cy - 70), (x + 78, cy - 58), (x + 95, cy - 56)],
                 fill=(245, 195, 90))
    font = font_fn(18, bold=True)
    draw.text((x + 36, cy - 100), "½", fill=(245, 195, 90), font=font)


def glyph_slides(draw, area, font_fn):
    """Two gems on the same string, connected by a diagonal line."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(3, x_l, x_r)
    cy1 = int(y_t + (y_b - y_t) * 0.30)
    cy2 = int(y_t + (y_b - y_t) * 0.70)
    draw.line([(x, cy1), (x, cy2)], fill=(122, 168, 255), width=8)
    _draw_gem(draw, x, cy1, r=24, text="5")
    _draw_gem(draw, x, cy2, r=24, text="7")


def glyph_legato(draw, area, font_fn):
    """Two gems linked by an overhead arc — hammer-on / pull-off tie."""
    x_l, x_r, y_t, y_b = area
    x1 = _string_x(3, x_l, x_r)
    x2 = _string_x(4, x_l, x_r)
    cy = int(y_t + (y_b - y_t) * 0.55)
    # Arc above the gems
    arc_box = (min(x1, x2) - 10, cy - 80, max(x1, x2) + 10, cy - 10)
    draw.arc(arc_box, start=180, end=360, fill=(120, 220, 160), width=5)
    _draw_gem(draw, x1, cy, r=24, text="5")
    _draw_gem(draw, x2, cy, r=24, text="7")


def glyph_harmonics(draw, area, font_fn):
    """Diamond-shaped gem on the high E string at the 12th fret."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(5, x_l, x_r)
    cy = int(y_t + (y_b - y_t) * 0.50)
    r = 26
    draw.polygon([(x, cy - r), (x + r, cy), (x, cy + r), (x - r, cy)],
                 fill=(220, 180, 255), outline=(255, 255, 255))
    font = font_fn(20, bold=True)
    bbox = draw.textbbox((0, 0), "12", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x - tw // 2, cy - th // 2 - 2), "12", fill=(40, 20, 60), font=font)


def glyph_palm_mute(draw, area, font_fn):
    """Gem with a 'P.M.' bracket above it."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(0, x_l, x_r)  # low E for palm-mute riff feel
    cy = int(y_t + (y_b - y_t) * 0.55)
    _draw_gem(draw, x, cy, r=24, text="0", color=(170, 170, 170))
    # Bracket above
    bx1, bx2, by = x - 30, x + 30, cy - 55
    draw.line([(bx1, by), (bx2, by)],     fill=(245, 195, 90), width=3)
    draw.line([(bx1, by), (bx1, by + 8)], fill=(245, 195, 90), width=3)
    draw.line([(bx2, by), (bx2, by + 8)], fill=(245, 195, 90), width=3)
    font = font_fn(18, bold=True)
    draw.text((x - 22, by - 28), "P.M.", fill=(245, 195, 90), font=font)


def glyph_muted(draw, area, font_fn):
    """X marker on the B string."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(4, x_l, x_r)
    cy = int(y_t + (y_b - y_t) * 0.50)
    r = 24
    draw.line([(x - r, cy - r), (x + r, cy + r)], fill=(230, 110, 110), width=6)
    draw.line([(x - r, cy + r), (x + r, cy - r)], fill=(230, 110, 110), width=6)


def glyph_tremolo(draw, area, font_fn):
    """Three stacked diagonal slashes above a gem — tremolo bars notation."""
    x_l, x_r, y_t, y_b = area
    x = _string_x(5, x_l, x_r)
    cy = int(y_t + (y_b - y_t) * 0.60)
    _draw_gem(draw, x, cy, r=24, text="12")
    for i in range(3):
        offset = -45 + i * 12
        draw.line([(x - 18, cy + offset + 6), (x + 18, cy + offset - 6)],
                  fill=(245, 195, 90), width=4)


def glyph_finale(draw, area, font_fn):
    """A small medley of glyphs from earlier lessons."""
    x_l, x_r, y_t, y_b = area
    cy = int(y_t + (y_b - y_t) * 0.55)
    # Bend marker
    x1 = _string_x(3, x_l, x_r)
    _draw_gem(draw, x1, cy - 60, r=18, text="7")
    draw.arc([(x1 + 6, cy - 100), (x1 + 50, cy - 50)],
             start=90, end=180, fill=(245, 195, 90), width=3)
    # Slide
    x2 = _string_x(4, x_l, x_r)
    draw.line([(x2, cy - 10), (x2, cy + 40)], fill=(122, 168, 255), width=5)
    _draw_gem(draw, x2, cy - 10, r=16, text="5")
    _draw_gem(draw, x2, cy + 40, r=16, text="7")
    # Diamond harmonic
    x3 = _string_x(5, x_l, x_r)
    r = 16
    cy3 = cy + 30
    draw.polygon([(x3, cy3 - r), (x3 + r, cy3), (x3, cy3 + r), (x3 - r, cy3)],
                 fill=(220, 180, 255), outline=(255, 255, 255))


GLYPHS: dict[str, Callable] = {
    "notes":      glyph_notes,
    "chords":     glyph_chords,
    "bends":      glyph_bends,
    "slides":     glyph_slides,
    "legato":     glyph_legato,
    "harmonics":  glyph_harmonics,
    "palm_mute":  glyph_palm_mute,
    "muted":      glyph_muted,
    "tremolo":    glyph_tremolo,
    "finale":     glyph_finale,
}


PALETTES = [
    ((24, 36, 56), (70, 52, 100)),    # 1 indigo
    ((40, 30, 50), (100, 60, 80)),    # 2 plum
    ((20, 44, 50), (54, 90, 90)),     # 3 teal
    ((58, 30, 22), (110, 60, 30)),    # 4 amber
    ((22, 50, 28), (50, 110, 60)),    # 5 olive
    ((30, 30, 60), (90, 60, 120)),    # 6 violet (ambient)
    ((50, 18, 18), (110, 30, 30)),    # 7 crimson (metal)
    ((36, 24, 50), (80, 40, 100)),    # 8 grape (funk)
    ((26, 40, 60), (50, 110, 150)),   # 9 ocean (surf)
    ((36, 22, 60), (110, 60, 140)),   # 10 magenta (finale)
]


def render_pack_cover(out_path: Path):
    """The pack cover for "Reading the Highway"."""
    from PIL import Image, ImageDraw
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (20, 28, 50))
    _gradient_bg(img, (20, 28, 50), (90, 60, 130))
    string_area = (W * 0.10, W * 0.90, H * 0.20, H * 0.70)
    _draw_highway_strings(img, *string_area)
    draw = ImageDraw.Draw(img)
    # A handful of representative glyphs across the strings
    cy_top    = int(H * 0.30)
    cy_mid    = int(H * 0.45)
    cy_bot    = int(H * 0.60)
    _draw_gem(draw, _string_x(1, *string_area[:2]), cy_top, r=28, text="3")
    _draw_gem(draw, _string_x(2, *string_area[:2]), cy_mid, r=28, text="5")
    _draw_gem(draw, _string_x(3, *string_area[:2]), cy_bot, r=28, text="7")
    # Bend arrow
    bx = _string_x(4, *string_area[:2])
    _draw_gem(draw, bx, cy_mid, r=28, text="8")
    draw.arc([(bx + 12, cy_mid - 100), (bx + 100, cy_mid + 10)],
             start=90, end=180, fill=(245, 195, 90), width=6)
    title_font = _try_load_font(112, bold=True)
    sub_font   = _try_load_font(44)
    draw.text((90, 100), "Reading the Highway", fill=(232, 236, 244), font=title_font)
    draw.text((92, 226), "10 lessons · learn the notation", fill=(170, 175, 200), font=sub_font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)


def render_lesson_thumb(out_path: Path, notation: str, lesson_idx: int,
                        title: str, sub: str | None):
    """Render the per-lesson thumb."""
    palette = PALETTES[lesson_idx % len(PALETTES)]
    glyph = GLYPHS.get(notation, glyph_notes)
    img = _render_thumb_base(notation, lesson_idx, title, sub, palette, glyph)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)


# ── Lesson definitions ───────────────────────────────────────────────────────

@dataclass
class Lesson:
    id:        str
    title:     str
    notation:  str
    template:  SongTemplate
    build_notes: Callable[[SongTemplate], list[dict]]
    pass_acc:  float = 0.7
    xp:        tuple[int, int] = (100, 250)
    techniques: list[str] = field(default_factory=list)


# ── L1: Notes & sustain — A minor acoustic ballad, 75 BPM ────────────────────

def _notes_l1(t: SongTemplate) -> dict:
    """Eight single notes from the A natural minor scale, hitting the chord
    tones of each bar's harmony. Sustain length varies to show how the
    highway draws short/medium/long durations."""
    # Bar 4 = Am, 5 = F, 6 = C, 7 = G, repeat for 12 exercise bars total.
    bar_notes = [
        # (bar_offset, string, fret, sustain_beats)
        (0,  3, 14, 2.0),   # A4   on Am
        (1,  3, 10, 1.5),   # F4   on F
        (2,  3, 12, 1.0),   # G4   on C
        (3,  3, 12, 4.0),   # G4   on G — held the full bar
        (4,  4, 13, 2.0),   # C5   on Am
        (5,  3, 10, 0.5),   # F4   short snap on F
        (6,  3, 12, 0.5),   # G4   short snap on C
        (7,  3, 14, 3.5),   # A4   on G — held nearly the whole bar
        (8,  3, 14, 2.0),   # A4   on Am (repeat)
        (9,  4,  6, 1.0),   # F4   on F (B string fret 6 — alternate voicing)
        (10, 4,  8, 1.0),   # G4   on C
        (11, 4, 10, 4.0),   # A4   on G — sustained final note
    ]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for offset, s, f, sus_beats in bar_notes:
        out.append(note(
            t=bar_time(start_bar + offset, 0.0, t.bar_duration),
            s=s, f=f,
            sus=sus_beats * beat_sec,
        ))
    return {"notes": out, "chords": [], "templates": []}


# ── L2: Chords & handshapes — E major rock, 105 BPM ──────────────────────────

def _notes_l2(t: SongTemplate) -> dict:
    """Power-chord strums emitted as proper chord events so the highway
    draws the chord box (rather than three independent gems). Each chord
    shape is registered once in `templates`; chord events reference it
    by index."""
    chord_shapes = {
        "E5":  [(0, 0), (1, 2), (2, 2)],   # low E open + A2 + D2
        "C#5": [(1, 4), (2, 6), (3, 6)],
        "A5":  [(0, 5), (1, 7), (2, 7)],
        "B5":  [(0, 7), (1, 9), (2, 9)],
    }
    # Register one template per shape, in the order encountered.
    templates: list[dict] = []
    template_id: dict[str, int] = {}
    for name, shape in chord_shapes.items():
        frets = [-1] * 6
        for s, f in shape:
            frets[s] = f
        template_id[name] = len(templates)
        templates.append(chord_template(frets, name=name))

    chords: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        chord_name = t.progression[bar_offset % len(t.progression)]
        if chord_name not in template_id:
            continue
        tid = template_id[chord_name]
        shape = chord_shapes[chord_name]
        # Two strums per bar — beats 1 and 3, each sustained ~1.5 beats.
        for beat in (0.0, 2.0):
            ev_t = bar_time(start_bar + bar_offset, beat, t.bar_duration)
            notes_list = [chord_note(s, f, sus=1.5 * beat_sec) for s, f in shape]
            chords.append(chord_event(ev_t, tid, notes_list))
    return {"notes": [], "chords": chords, "templates": templates}


# ── L3: Bends — A minor blues shuffle, 88 BPM ────────────────────────────────

def _notes_l3(t: SongTemplate) -> dict:
    """Half- and full-step bends drawn from the A minor pentatonic, two
    bends per bar across the 12-bar exercise. Each bar emits one bend on
    beat 1 and another on beat 3."""
    # (string, fret, bend_semis)
    pool = [
        (3, 7,  1.0),   # G string fret 7 (D4)  ½ → D#4
        (3, 9,  2.0),   # G string fret 9 (E4)  full → F#4
        (4, 8,  1.0),   # B string fret 8 (G4)  ½ → G#4
        (4, 10, 2.0),   # B string fret 10 (A4) full → B4
        (5, 7,  1.0),   # high E fret 7 (B4)    ½ → C5
        (5, 8,  2.0),   # high E fret 8 (C5)    full → D5
    ]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        for slot, beat in enumerate((0.0, 2.0)):
            s, f, bn = pool[(bar_offset * 2 + slot) % len(pool)]
            out.append(note(
                t=bar_time(start_bar + bar_offset, beat, t.bar_duration),
                s=s, f=f, sus=1.5 * beat_sec, bn=bn,
            ))
    return {"notes": out, "chords": [], "templates": []}


# ── L4: Slides — D major country, 95 BPM ─────────────────────────────────────

def _notes_l4(t: SongTemplate) -> dict:
    """Slide-to notation: every note plays at a source fret and slides
    to a destination fret (`sl` field). The exercise alternates ascending
    and descending slides on the G and B strings."""
    # (string, source_fret, target_fret) — each slide spans 2-4 frets so
    # the line is visible on the highway without crossing octaves.
    pool = [
        (3, 5,  7),    # G: 5 → 7
        (3, 7,  9),    # G: 7 → 9
        (4, 5,  3),    # B: 5 → 3 (descending)
        (4, 7,  5),    # B: 7 → 5 (descending)
        (3, 9,  12),   # G: 9 → 12
        (3, 12, 9),    # G: 12 → 9 (descending)
    ]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        for slot, beat in enumerate((0.0, 2.0)):
            s, src, tgt = pool[(bar_offset * 2 + slot) % len(pool)]
            out.append(note(
                t=bar_time(start_bar + bar_offset, beat, t.bar_duration),
                s=s, f=src, sus=1.7 * beat_sec, sl=tgt,
            ))
    return {"notes": out, "chords": [], "templates": []}


# ── L5: Hammer-on & pull-off — G major mellow funk, 100 BPM ─────────────────

def _notes_l5(t: SongTemplate) -> dict:
    """Two-note slurred pairs. The first note is plucked; the second
    carries the hammer-on (`ho`) or pull-off (`po`) flag so the highway
    draws a connector arc."""
    # (string, fret_a, fret_b, kind) — kind 'ho' or 'po'
    pool = [
        (3, 5, 7, "ho"),   # G: 5 → 7  hammer
        (4, 7, 5, "po"),   # B: 7 → 5  pull
        (3, 7, 9, "ho"),   # G: 7 → 9  hammer
        (5, 5, 8, "ho"),   # high E: 5 → 8 hammer
        (5, 8, 5, "po"),   # high E: 8 → 5 pull
        (4, 8, 10, "ho"),  # B: 8 → 10 hammer
    ]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        s, fa, fb, kind = pool[bar_offset % len(pool)]
        bar_t = bar_time(start_bar + bar_offset, 0.0, t.bar_duration)
        # Source note on beat 1, slurred note on beat 1.5 (an eighth later).
        out.append(note(t=bar_t,                     s=s, f=fa, sus=0.5 * beat_sec))
        out.append(note(
            t=bar_t + 0.5 * beat_sec, s=s, f=fb,
            sus=3.0 * beat_sec,
            ho=(kind == "ho"), po=(kind == "po"),
        ))
    return {"notes": out, "chords": [], "templates": []}


# ── L6: Harmonics — C major ambient, 68 BPM ──────────────────────────────────

def _notes_l6(t: SongTemplate) -> dict:
    """Natural harmonics (`hm: true`) at the canonical positions: 5, 7,
    12. Slow and sparse — one per bar, sustained to underline the
    glistening tone."""
    # Natural harmonic positions, picked to fit C–G–Am–F harmony loosely.
    pool = [
        (5, 12),  # E5 — sounds an octave above open
        (4, 12),  # B4
        (3, 12),  # G4
        (5, 7),   # B4 (perfect 5th harmonic on high E)
        (4, 7),   # F#4 (5th on B)
        (3, 5),   # G5-ish (2-octaves+5th)
    ]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        s, f = pool[bar_offset % len(pool)]
        out.append(note(
            t=bar_time(start_bar + bar_offset, 0.0, t.bar_duration),
            s=s, f=f, sus=3.8 * beat_sec, hm=True,
        ))
    return {"notes": out, "chords": [], "templates": []}


# ── L7: Palm mute — E minor hard rock, 128 BPM ──────────────────────────────

def _notes_l7(t: SongTemplate) -> dict:
    """Palm-muted 8th-note chug riff on the low E. Every note has
    `pm: True` so the highway renders the P.M. bracket and the audio
    pipeline shortens the note + lowers velocity."""
    # Use the open low E (s=0 f=0) and the 3rd fret (G) for the classic
    # E-minor chug. Eight notes per bar.
    pattern = [
        (0, 0), (0, 0), (0, 3), (0, 0),
        (0, 0), (0, 3), (0, 0), (0, 0),
    ]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        bar_t = bar_time(start_bar + bar_offset, 0.0, t.bar_duration)
        for i, (s, f) in enumerate(pattern):
            out.append(note(
                t=bar_t + i * 0.5 * beat_sec,
                s=s, f=f,
                sus=0.5 * beat_sec,
                pm=True,
            ))
    return {"notes": out, "chords": [], "templates": []}


# ── L8: Muted / dead notes — D minor funk-rock, 112 BPM ─────────────────────

def _notes_l8(t: SongTemplate) -> dict:
    """Funk pattern alternating live notes and dead notes. The live notes
    sit on chord tones; the dead notes (`mt: True`) carry the percussive
    feel that funk hinges on."""
    # Each bar emits 8 events alternating live/dead. Fret 0 for dead notes
    # so the cross marker stays compact and at a consistent height.
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    for bar_offset in range(t.exercise_bars):
        bar_t = bar_time(start_bar + bar_offset, 0.0, t.bar_duration)
        for i in range(8):
            t_at = bar_t + i * 0.5 * beat_sec
            if i % 2 == 0:
                # Live note — alternate between D string and A string.
                s = 2 if (i // 2) % 2 == 0 else 1
                f = 5 if s == 2 else 5
                out.append(note(t=t_at, s=s, f=f, sus=0.35 * beat_sec))
            else:
                # Dead note on the same string family.
                s = 2 if (i // 2) % 2 == 0 else 1
                out.append(note(t=t_at, s=s, f=0, sus=0.15 * beat_sec, mt=True))
    return {"notes": out, "chords": [], "templates": []}


# ── L9: Tremolo & taps — E major surf, 124 BPM ──────────────────────────────

def _notes_l9(t: SongTemplate) -> dict:
    """Alternating tremolo notes (`tr: True`) and tapped notes (`tp: True`).
    Tremolo notes get rapid 1/16-note re-attacks in the MIDI; taps are
    visual-only (the highway draws a T marker). Tap bars now use 4 high-
    fret notes (was 2) so the exercise feels alive instead of sleepy."""
    # Tremolo notes step through the E-major triad so consecutive
    # tremolo bars don't feel identical: E (fret 12) → B (fret 7) → G#
    # (fret 4) → E (fret 12).
    tremolo_pool = [(5, 12), (5, 7), (4, 9), (5, 12)]
    # Tap fret pattern — ascending shred run, repeats per tap bar.
    tap_pool = [(5, 12), (5, 15), (5, 17), (5, 19)]
    out: list[dict] = []
    start_bar = t.count_in_bars
    beat_sec = 60.0 / t.bpm
    tremolo_idx = 0
    for bar_offset in range(t.exercise_bars):
        bar_t = bar_time(start_bar + bar_offset, 0.0, t.bar_duration)
        if bar_offset % 2 == 0:
            s, f = tremolo_pool[tremolo_idx % len(tremolo_pool)]
            tremolo_idx += 1
            out.append(note(t=bar_t, s=s, f=f, sus=3.7 * beat_sec, tr=True))
        else:
            # 4 taps, one per beat
            for i, (s, f) in enumerate(tap_pool):
                out.append(note(t=bar_t + i * beat_sec, s=s, f=f,
                                sus=0.9 * beat_sec, tp=True))
    return {"notes": out, "chords": [], "templates": []}


# ── L10: Combined finale — A minor modern rock, 100 BPM ─────────────────────

def _notes_l10(t: SongTemplate) -> dict:
    """A 12-bar passage that cycles through the notations the previous
    lessons taught — a low-stakes demo, not a virtuoso run. The lead
    references each technique in turn so the player practices switching
    between them."""
    beat_sec = 60.0 / t.bpm
    start_bar = t.count_in_bars

    out_notes: list[dict] = []

    # Bar 0: plain sustained note (notes & sustain)
    out_notes.append(note(t=bar_time(start_bar + 0, 0.0, t.bar_duration),
                          s=3, f=12, sus=3.5 * beat_sec))
    # Bar 1: bend
    out_notes.append(note(t=bar_time(start_bar + 1, 0.0, t.bar_duration),
                          s=3, f=12, sus=3.5 * beat_sec, bn=1.0))
    # Bar 2: slide
    out_notes.append(note(t=bar_time(start_bar + 2, 0.0, t.bar_duration),
                          s=3, f=10, sus=3.5 * beat_sec, sl=7))
    # Bar 3: hammer-on pair
    out_notes.append(note(t=bar_time(start_bar + 3, 0.0, t.bar_duration),
                          s=3, f=7, sus=0.5 * beat_sec))
    out_notes.append(note(t=bar_time(start_bar + 3, 0.0, t.bar_duration) + 0.5 * beat_sec,
                          s=3, f=10, sus=3.0 * beat_sec, ho=True))
    # Bar 4: pull-off pair
    out_notes.append(note(t=bar_time(start_bar + 4, 0.0, t.bar_duration),
                          s=3, f=10, sus=0.5 * beat_sec))
    out_notes.append(note(t=bar_time(start_bar + 4, 0.0, t.bar_duration) + 0.5 * beat_sec,
                          s=3, f=7, sus=3.0 * beat_sec, po=True))
    # Bar 5: natural harmonic
    out_notes.append(note(t=bar_time(start_bar + 5, 0.0, t.bar_duration),
                          s=5, f=12, sus=3.6 * beat_sec, hm=True))
    # Bar 6: palm-muted chug (4 hits)
    bar_t6 = bar_time(start_bar + 6, 0.0, t.bar_duration)
    for i in range(4):
        out_notes.append(note(t=bar_t6 + i * beat_sec, s=0, f=0,
                              sus=0.7 * beat_sec, pm=True))
    # Bar 7: dead-note funk stab pattern
    bar_t7 = bar_time(start_bar + 7, 0.0, t.bar_duration)
    for i in range(8):
        t_at = bar_t7 + i * 0.5 * beat_sec
        if i % 2 == 0:
            out_notes.append(note(t=t_at, s=2, f=5, sus=0.35 * beat_sec))
        else:
            out_notes.append(note(t=t_at, s=2, f=0, sus=0.15 * beat_sec, mt=True))
    # Bar 8: tremolo
    out_notes.append(note(t=bar_time(start_bar + 8, 0.0, t.bar_duration),
                          s=5, f=8, sus=3.8 * beat_sec, tr=True))
    # Bar 9: tapped run
    bar_t9 = bar_time(start_bar + 9, 0.0, t.bar_duration)
    for i, fret in enumerate((12, 15, 17, 19)):
        out_notes.append(note(t=bar_t9 + i * beat_sec, s=5, f=fret,
                              sus=0.9 * beat_sec, tp=True))

    # Bars 10–11: a chord-strummed cadence to wrap up. Build it as a
    # proper chord event so the highway shows the chord box — referencing
    # `templates[0]` (Am power-chord-ish shape).
    chord_shapes = {"Am": [(1, 0), (2, 2), (3, 2)]}
    templates: list[dict] = []
    template_id: dict[str, int] = {}
    for name, shape in chord_shapes.items():
        frets = [-1] * 6
        for s, f in shape:
            frets[s] = f
        template_id[name] = len(templates)
        templates.append(chord_template(frets, name=name))

    chords: list[dict] = []
    for bar_offset in (10, 11):
        bar_t = bar_time(start_bar + bar_offset, 0.0, t.bar_duration)
        for beat in (0.0, 2.0):
            chords.append(chord_event(
                bar_t + beat * beat_sec, template_id["Am"],
                [chord_note(s, f, sus=1.5 * beat_sec) for s, f in chord_shapes["Am"]],
            ))

    return {"notes": out_notes, "chords": chords, "templates": templates}


LESSONS: list[Lesson] = [
    Lesson(
        id="l1",
        title="Notes & sustain",
        notation="notes",
        template=SongTemplate(
            bpm=75,
            progression=["Am", "F", "C", "G"],
            drum_pattern="ballad_gentle",
            rhythm_program=24,          # Nylon String Guitar
            bass_program=33,
            lead_program=27,            # Clean Electric Guitar
            bass_style="quarter",
            rhythm_style="arpeggio",
        ),
        build_notes=_notes_l1,
        techniques=["notes", "sustain"],
    ),
    Lesson(
        id="l2",
        title="Chords & handshapes",
        notation="chords",
        template=SongTemplate(
            bpm=105,
            progression=["E5", "C#5", "A5", "B5"],
            drum_pattern="rock_backbeat",
            rhythm_program=29,          # Overdriven Guitar
            bass_program=33,
            lead_program=29,
            bass_style="root_pulse",
            rhythm_style="strum_quarters",
        ),
        build_notes=_notes_l2,
        techniques=["chords", "handshape"],
    ),
    Lesson(
        id="l3",
        title="Bends",
        notation="bends",
        template=SongTemplate(
            # 4-chord loop instead of the 8-chord blues-form; the latter
            # wrapped awkwardly across 12 exercise bars.  Walking bass
            # now avoids the chord 3rd (see bass_walking) so it doesn't
            # voice a major-3rd over the minor 7ths.
            bpm=85,
            progression=["Am7", "Dm7", "Am7", "Em7"],
            drum_pattern="shuffle",
            rhythm_program=27,          # Clean Electric Guitar
            bass_program=33,
            lead_program=29,            # Overdriven for bluesy bends
            bass_style="walking",
            rhythm_style="strum_quarters",
        ),
        build_notes=_notes_l3,
        techniques=["bend"],
    ),
    Lesson(
        id="l4",
        title="Slides",
        notation="slides",
        template=SongTemplate(
            # Train beat at 95 BPM piled onto acoustic strums was too
            # busy; rock_backbeat is roomier and lets the lead breathe.
            # Bass is on root_pulse so the chord changes punch through
            # rather than thumping through every quarter.
            bpm=92,
            progression=["D", "G", "A", "D"],
            drum_pattern="rock_backbeat",
            rhythm_program=25,          # Acoustic Guitar (steel)
            bass_program=33,
            lead_program=27,
            bass_style="root_pulse",
            rhythm_style="strum_quarters",
        ),
        build_notes=_notes_l4,
        techniques=["slide"],
    ),
    Lesson(
        id="l5",
        title="Hammer-on & pull-off",
        notation="legato",
        template=SongTemplate(
            bpm=100,
            progression=["Gmaj7", "Cmaj7", "Gmaj7", "Cmaj7"],
            drum_pattern="funk_16",
            rhythm_program=27,
            bass_program=33,
            lead_program=27,
            bass_style="funk_16",
            rhythm_style="stab_2_4",
        ),
        build_notes=_notes_l5,
        techniques=["hammer-on", "pull-off", "legato"],
    ),
    Lesson(
        id="l6",
        title="Harmonics",
        notation="harmonics",
        template=SongTemplate(
            bpm=68,
            progression=["C", "G", "Am", "F"],
            drum_pattern="ambient",
            rhythm_program=89,          # Pad 2 (Warm)
            bass_program=33,
            lead_program=27,
            bass_style="sparse_root",
            rhythm_style="pad",
        ),
        build_notes=_notes_l6,
        techniques=["harmonic"],
    ),
    Lesson(
        id="l7",
        title="Palm mute",
        notation="palm_mute",
        template=SongTemplate(
            # Original was a low-mud disaster — distorted rhythm + lead +
            # 8th-note metal bass + 8th-note kick all stomped on each
            # other.  Open it up: rock backbeat, root-pulse bass, clean
            # rhythm guitar (overdriven, not full distortion) so the
            # palm-muted lead has room to chug audibly.  Lead stays on
            # distortion so the player still hears that grit.
            bpm=120,
            progression=["Em", "C", "G", "D"],
            drum_pattern="rock_backbeat",
            rhythm_program=29,          # Overdriven (not distortion)
            bass_program=33,
            lead_program=30,            # Distortion guitar for the chug
            bass_style="root_pulse",
            rhythm_style="strum_quarters",
        ),
        build_notes=_notes_l7,
        techniques=["palm-mute"],
    ),
    Lesson(
        id="l8",
        title="Muted / dead notes",
        notation="muted",
        template=SongTemplate(
            bpm=112,
            progression=["Dm7", "Gm7", "Dm7", "Gm7"],
            drum_pattern="funk_16",
            rhythm_program=27,
            bass_program=33,
            lead_program=27,
            bass_style="funk_16",
            rhythm_style="stab_2_4",
        ),
        build_notes=_notes_l8,
        techniques=["dead-note", "mute"],
    ),
    Lesson(
        id="l9",
        title="Tremolo & taps",
        notation="tremolo",
        template=SongTemplate(
            bpm=124,
            progression=["E", "A", "B", "E"],
            drum_pattern="surf",
            rhythm_program=29,
            bass_program=33,
            lead_program=29,
            bass_style="quarter",
            rhythm_style="strum_quarters",
        ),
        build_notes=_notes_l9,
        techniques=["tremolo", "tap"],
    ),
    Lesson(
        id="l10",
        title="Combined arrangement",
        notation="finale",
        template=SongTemplate(
            bpm=100,
            progression=["Am", "F", "C", "G"],
            drum_pattern="half_full_alt",
            rhythm_program=29,
            bass_program=33,
            lead_program=29,
            bass_style="quarter",
            rhythm_style="strum_quarters",
        ),
        build_notes=_notes_l10,
        techniques=["bend", "slide", "hammer-on", "pull-off", "harmonic",
                    "palm-mute", "dead-note", "tremolo", "tap", "chords"],
        xp=(200, 500),
    ),
]


# ── Main ─────────────────────────────────────────────────────────────────────

PACK_ID = "reading-the-highway"
PACK_TITLE = "Reading the Highway"
PACK_SUBTITLE = "Learn the sloppak / highway notation"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent / "sloppaks")
    parser.add_argument("--dlc", type=Path,
                        default=Path.home() / ".local/share/Steam/steamapps/common/Rocksmith2014/dlc")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    builtin_dir = Path(__file__).resolve().parent
    runtime_pack_dir = Path.home() / f".config/slopsmith/tutorials/packs/{PACK_ID}"
    runtime_pack_dir.mkdir(parents=True, exist_ok=True)

    # Pack-level cover
    cover_path = builtin_dir / "cover.png"
    render_pack_cover(cover_path)
    shutil.copy2(cover_path, runtime_pack_dir / "cover.png")
    print(f"wrote pack cover → {runtime_pack_dir / 'cover.png'}")

    # Pack-level manifest skeleton — lessons appended below.
    manifest_lessons: list[dict] = []

    thumbs_builtin = builtin_dir / "thumbs"
    thumbs_builtin.mkdir(parents=True, exist_ok=True)
    runtime_thumbs = runtime_pack_dir / "thumbs"
    runtime_thumbs.mkdir(parents=True, exist_ok=True)

    for idx, lesson in enumerate(LESSONS):
        sloppak_name = f"Tutorial_Reading_the_Highway_{lesson.id.upper()}.sloppak"
        out_path = args.out / sloppak_name
        print(f"Building {sloppak_name}...")

        content = lesson.build_notes(lesson.template)
        midi_notes = midi_notes_from_content(content)
        lead_events = lead_events_from_notes(midi_notes, lesson.template)
        # Use a deterministic seed derived from the lesson id so regenerated
        # audio is reproducible across Python interpreter invocations.
        # Python's built-in hash() is randomized per-process (PYTHONHASHSEED)
        # and must not be used for reproducibility.
        _stable_seed = int(hashlib.sha256(lesson.id.encode()).hexdigest()[:8], 16)
        mid = build_backing_midi(lesson.template, lead_events, rng_seed=_stable_seed)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
            tmp_mp3_path = Path(tmp_mp3.name)
        try:
            render_audio(tmp_mp3_path, mid, lesson.template.total_duration)
            arrangement = build_arrangement(content, lesson.template)
            sloppak_manifest = build_manifest(
                f"Reading the Highway — {lesson.id.upper()} {lesson.title}",
                lesson.template,
            )
            write_sloppak(out_path, sloppak_manifest, arrangement, tmp_mp3_path)
            print(f"  wrote {out_path} ({out_path.stat().st_size} bytes)")
        finally:
            try:
                tmp_mp3_path.unlink()
            except OSError:
                pass

        if str(args.dlc) != "-" and args.dlc.is_dir():
            # Copy to the same subdirectory that setup() seeds into at runtime,
            # so regenerated sloppaks are immediately playable via playSong.
            target_dir = args.dlc / "tutorials-builtin" / PACK_ID
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / sloppak_name
            shutil.copy2(out_path, target)
            print(f"  copied to {target}")

        # Per-lesson thumb
        thumb_builtin_path = thumbs_builtin / f"{lesson.id}.png"
        sub = f"{lesson.template.bpm:.0f} BPM · {int(lesson.template.total_duration)}s"
        render_lesson_thumb(thumb_builtin_path, lesson.notation, idx, lesson.title, sub)
        shutil.copy2(thumb_builtin_path, runtime_thumbs / f"{lesson.id}.png")

        # Exercise sloppak path must be DLC-relative so playSong can find it.
        # setup() seeds builtin sloppaks under <DLC_DIR>/tutorials-builtin/<pack>/
        # so the reference must include that prefix. The video slot is left
        # empty — the .webm files are not generated by this script and are not
        # committed; the lesson player shows "No video attached" for an empty src.
        dlc_sloppak_path = f"tutorials-builtin/{PACK_ID}/{sloppak_name}"
        manifest_lessons.append({
            "id":         lesson.id,
            "title":      lesson.title,
            "video":      {"type": "file", "src": ""},
            "exercise":   {"sloppak": dlc_sloppak_path, "arrangement": ""},
            "pass":       {"accuracy": lesson.pass_acc},
            "mastery":    {"accuracy": 0.9, "speed": 1.0},
            "xp":         {"pass": lesson.xp[0], "mastery": lesson.xp[1]},
            "techniques": lesson.techniques,
        })

    # Write the pack-level pack.json
    pack_manifest = {
        "schema":     1,
        "id":         PACK_ID,
        "title":      PACK_TITLE,
        "author":     "Slopsmith",
        "techniques": sorted({t for L in LESSONS for t in L.techniques}),
        "lessons":    manifest_lessons,
    }
    (builtin_dir / "pack.json").write_text(json.dumps(pack_manifest, indent=2))
    (runtime_pack_dir / "pack.json").write_text(json.dumps(pack_manifest, indent=2))
    print(f"\nwrote pack.json → {runtime_pack_dir / 'pack.json'} ({len(LESSONS)} lessons)")


if __name__ == "__main__":
    main()
