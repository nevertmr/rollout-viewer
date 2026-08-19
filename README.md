# rollout-viewer

A local web viewer for **on-policy rollouts of a robot manipulation policy** (SO-101 arm, π0.5 fine-tune).
Built to inspect what the policy actually did during inference: two camera views, joint trajectories, and
per-episode quality metrics, with the retries of the same task laid side by side.

The layout follows the LeRobot `visualize_dataset` Space, with one addition: the same task was attempted
several times (the operator retried after a failure), so the viewer groups those attempts together and can
overlay them on the same charts.

<img width="900" alt="viewer" src="https://github.com/user-attachments/assets/placeholder" />

## What it shows

- **Finder-style sidebar, two columns** — *Run* (experiment) → *Try*. Selecting a try tiles every task
  (step of the routine) of that try as one card grid, so a whole try fits on one screen and failed / successful
  cards sit right next to each other. One global play bar (bottom) drives every card in sync; a segmented
  *Attention / Causal / Original* control switches the overlay on all cards at once (the ◉ chip on a card
  overrides it for that card). Charts for the focused card's task live in a collapsible box under the grid.
  `↑↓` moves between tries, `⌥↑↓` between runs (the same try number is kept when it exists), `Space` plays,
  `←→` steps a frame, `[ ]` moves the focused card.
- **Both cameras at once** — top view and wrist view, stacked, played in sync.
- **Trajectory charts** — joints grouped into three panels; solid lines are `observation.state`, dashed are
  `action`. The gap between them is where the arm was blocked or lagging behind the command.
- **Final success by default** — failed retries are hidden until you tick *Show all attempts*, which puts
  every attempt of a task side by side (same task grouped, fail → success) and overlays them on the charts.
- **Manual-stop shading** — the operator ended each episode by hand, so the idle tail is shaded and excluded
  from the metrics. Episode length is not a quality signal in this data.
- **Lamp panel for press steps** — the coffee machine lamp is read straight from the pixels, which gives a
  frame-exact success moment for the two button-press steps.

## Data layout

The viewer reads the recorder output directly (see
[lerobot-inference-recorder](https://github.com/nevertmr/lerobot-inference-recorder)):

```
<root>/<run>/
  run_meta.json                     event log: labels (success/fail/unstable), memos, deleted flags
  raw/epNNNN_cC_tT/
    episode.json                    id, cycle, task_index, task, n_frames
    steps.jsonl                     per frame: {f, ts, pt, state[6], action[6]}
    frames/fNNNNN_{front,wrist}.jpg
```

`DATA_ROOT` at the top of `viewer_data.py` points at that root. Every directory under it that contains a
`raw/` folder is indexed as a run. `run_meta.json` may also carry `custom_tasks: {"103": "..."}` for ad-hoc
instructions outside the numbered routine; those show up as `Task 103` etc.

### Run › Try › Task

The sidebar groups the recorder output in two levels (the third, Task, is laid out in the main pane).
Directories listed in `EXCLUDE_RUNS` (`build_index.py`, mirrored in `viewer_data.py`) are skipped entirely. The mapping from directory to the first two levels
is a small rule table at the top of `build_index.py` (`EXPERIMENT_RULES`):

| directory | Run (experiment) | Try |
|---|---|---|
| `coffee_new01` … `coffee_new15` | `NorRec_RW___Red` (one experiment, 15 tries) | directory number (1…15) |
| anything else (e.g. `NorRec_RW___white`, `record_for_Exp4`) | the directory name, unchanged | the recorder cycle `cN` (1 try per cycle; a single-cycle run shows `Try 1` only) |

Cycle numbers are used as-is (a gap such as c1, c2, c4 stays a gap) so a try can always be traced back to
the recording. The grouping key for attempts is still `(run, cycle, step)`; episode ids (`<run>/epNNNN`),
clip names (`<run>_NNNN_<cam>.mp4`) and episode JSON paths are unchanged. The index only *adds*
`experiment` / `try_no` to every episode and group, plus an `experiments` summary and per-run `tasks`
(the global `tasks` list is the union across runs).

## Run

```bash
python3 build_index.py          # scans the runs, writes cache/index.json
python3 server.py --port 8760   # http://127.0.0.1:8760
```

Only the standard library plus numpy and Pillow. Clips are transcoded from the JPEG frames on first request
and cached under `cache/clips/`.

## Deploy

`deploy/` holds a self-contained variant: pre-baked per-episode clips and per-episode JSON are served by a
small static server, so the machine running it does not need the raw frames. Bake the artifacts first:

```bash
python3 build_clips.py   # bakes clips into dist/clips/ (6-way parallel ffmpeg); existing clips are skipped,
                         # so after adding runs only the new ones are encoded (FORCE=1 to redo, RUNS=a,b to limit)
python3 build_api.py     # dumps /api/index + /api/episode responses into dist/api/
```

Then copy `index.html` and the `static/` directory next to `deploy/serve.py`
(see the comment at the top of `deploy/docker-compose.yml`).

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Bind-mount the baked `clips/` and `api/` directories; do not bake them into the image.

## Files

| file | role |
|---|---|
| `server.py` | entry point: CLI, background index/lamp warm-up, HTTP server bootstrap |
| `viewer_data.py` | data layer: raw reading, labels, stop-trim, metrics, lamp series, episode/index payloads, scores |
| `viewer_clips.py` | clip baking: JPEG frames → mp4 via ffmpeg (`/clip`) |
| `viewer_http.py` | HTTP handler: routing, static files, frame/clip range streaming |
| `metrics.py` | episode loading, stop-trim, jerk / tracking error / reversals, lamp ROI reading, label resolution |
| `build_index.py` | scans all runs into `cache/index.json`; holds the Run/Try mapping rules (`EXPERIMENT_RULES`) |
| `build_clips.py`, `build_api.py` | bake the static deployment bundle (`dist/clips/`, `dist/api/`) |
| `index.html` | front-end markup (no build step, no CDN) |
| `static/viewer.css` | front-end styles |
| `static/js/` | front-end scripts, classic `<script>` files loaded in order: `config.js` (constants), `state.js` (shared state + helpers), `api.js` (fetch), `media.js` (clip playback, frame fallback, attention/causal overlays, sync), `charts.js` (SVG charts + cursor), `main.js` (sidebar, group selection, playback loop, keyboard) |
| `quality_metrics.py`, `margin_metrics.py` | offline analysis: smoothness estimators and failure-margin metrics |
| `deploy/` | static deployment variant (`serve.py` serves `index.html`, `static/`, baked `api/` + `clips/`) |

## Notes

- No authentication. Intended for a local machine or an internal network.
- The lamp ROI coordinates and the joint names are specific to this setup; change them in `metrics.py`.
