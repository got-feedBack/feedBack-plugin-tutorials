# slopsmith-plugin-tutorials — agent notes

## Architecture invariants

- **One plugin, many packs.** Packs are pure data under
  `<CONFIG_DIR>/tutorials/packs/<pack_id>/`. Never add per-pack plugins.
- **XP goes through minigames.** The frontend posts a run to both
  `POST /api/plugins/minigames/runs` (XP, with
  `game_id = "tutorial:<pack_id>:<lesson_id>"`) and
  `POST /api/plugins/tutorials/runs` (local progress). There is no
  server-side relay — the two calls are independent. We do not maintain
  our own XP ledger; only local progress (best score, pass/mastery state)
  lives in this plugin.
- **Sloppak playback is upstream.** Exercises launch via
  `window.playSong(filename, arrangement)`; we never decode chart data here.
  Lesson manifests store DLC-relative paths. Builtin pack sloppaks are seeded
  into `<DLC_DIR>/tutorials-builtin/<pack_id>/` by `setup()` so `playSong`
  can resolve them via the highway WS without user intervention.
- **Video upload mirrors `slopsmith-plugin-3dhighway`.** Stream to temp,
  atomic rename, MIME + size caps. Single slot per lesson, named
  `<lesson_id>.<ext>`.
- **Atomic writes** for `pack.json` and `progress.json`
  (temp+fsync+rename per constitution VII).

## Out of scope (v1)

- Coach / recommendation loop — manifests tag `techniques` so v2 can mine
  them, but no recommender ships in v1.
- Pack zip export/import — flagged for v1.1.
- Public pack index / remote discovery.
