# feedBack Plugin: Tutorials

Interactive video-based tutorials for [feedBack](https://github.com/got-feedBack/feedBack).
Watch a short intro video, play the paired exercise, earn XP through the
minigames profile.

## Installation

The feedBack desktop app already ships this plugin. For a Docker or source
install:

```bash
cd /path/to/feedBack/plugins
git clone https://github.com/got-feedBack/feedBack-plugin-tutorials.git tutorials
docker compose restart
```

## Usage

1. Open **Lessons** in the sidebar. It lists the installed tutorial packs, their
   lessons and your progress. Two starter packs ship with the plugin:
   **Intro to Bends** and **Reading the Highway**.
2. Click **Start**, **Continue** or **Replay**. The lesson opens in the
   Tutorials screen.
3. Watch the video, then click **Start exercise** to play the paired exercise.
4. When the exercise ends, enter your accuracy and click **Record run**. The run
   earns XP on your minigames profile and updates the lesson's pass and mastery
   state.

To build your own packs, switch to **Author** at the top of the Tutorials
screen. You can upload a video (webm/mp4) or link a YouTube URL, pick an
exercise from your library and set the pass and mastery thresholds.

## Concept

- One plugin, many **tutorial packs** (`.tutpak` = a directory with a manifest,
  optional embedded videos, and exercise sloppaks).
- The Tutorials screen has three modes. In the v3 UI you reach it from
  **Lessons** in the sidebar. The classic UI adds a top-nav "Tutorials" link.
  - **Browse** — pack grid + lesson list.
  - **Lesson** — video player + "Start exercise" → uses core `window.playSong`
    to launch the paired sloppak. When the song ends, the user is prompted to
    enter their accuracy and manually submit a run. Submission posts to the
    minigames XP endpoint and records local pass/mastery state independently.
  - **Author** — pack/lesson editor with video upload (webm/mp4) or YouTube
    URL, sloppak picker, pass/mastery thresholds, technique tags.
- All XP flows through `/api/plugins/minigames/runs`, so streaks, level, and
  unlocks stay unified with the rest of the minigame ecosystem.

## Pack layout

```
<CONFIG_DIR>/tutorials/packs/<pack_id>/
├── pack.json          # manifest (see schema in routes.py)
├── videos/            # optional local video files
└── sloppaks/          # optional exercise sloppaks copied into the pack
```

Two starter packs ship at `builtin/intro-bends/` and
`builtin/reading-the-highway/` and are copied into
`<CONFIG_DIR>/tutorials/packs/` on first run (idempotent).

## API

All under `/api/plugins/tutorials/`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/packs` | List installed packs (manifest summary). |
| GET | `/packs/{id}` | Full manifest. |
| POST | `/packs` | Create empty pack. |
| PUT | `/packs/{id}` | Replace manifest (atomic temp+rename). |
| DELETE | `/packs/{id}` | Remove a pack. |
| POST | `/packs/{id}/videos?lesson_id=...` | Upload a webm/mp4 (single slot per lesson). |
| GET | `/packs/{id}/videos/{filename}` | Stream uploaded video. |
| GET | `/packs/{id}/cover` | Serve pack cover image. |
| POST | `/packs/{id}/cover` | Upload pack cover (PNG/JPEG/WebP, up to 4 MB). |
| DELETE | `/packs/{id}/cover` | Remove pack cover. |
| GET | `/packs/{id}/lessons/{lesson_id}/thumb` | Serve per-lesson thumbnail. |
| POST | `/packs/{id}/lessons/{lesson_id}/thumb` | Upload lesson thumbnail. |
| DELETE | `/packs/{id}/lessons/{lesson_id}/thumb` | Remove lesson thumbnail. |
| POST | `/packs/{id}/sloppaks` | Copy a library sloppak into the pack. |
| GET | `/packs/{id}/sloppaks/{filename}` | Stream a pack-embedded sloppak. |
| POST | `/runs` | Record a finished run (local progress only; XP is posted directly by the frontend to the minigames plugin). |
| GET | `/progress` | Per-lesson best score + pass/mastery state. |

## Generating builtin content

The sloppaks, cover images, and thumbnails in `builtin/` are committed so
consumers get working content without running generators. To regenerate:

```bash
cd builtin/intro-bends && python3 generate.py
cd builtin/reading-the-highway && python3 generate.py
```

Requires the following on the host (all pre-installed in the feedBack Docker image):

- `fluidsynth` + `FluidR3_GM.sf2` soundfont
- `ffmpeg`
- Python packages: `mido`, `Pillow`, `PyYAML`

## Constitutional notes

- Vanilla frontend, no build step.
- Single-user, atomic writes (temp+rename) for `pack.json` and `progress.json`.
- Plugin is bundled into `feedBack-desktop` via the existing plugin-clone CI step.
