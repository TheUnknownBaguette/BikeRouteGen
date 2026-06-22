# BikeRouteGen — Code Health & Engineering Work Plan

**Purpose:** this is the companion to `ROUTE_ALGO_WORKPLAN.md`. That one is about the
*routing algorithm*; this one is about the *engineering* around it — performance,
correctness defects, test/CI infra, packaging, and maintainability. It came out of a
critical code review (2026-06-21). Read `PROJECT_CONTEXT.md` first for what exists;
this file is the source of truth for what to change here and in what order.

If `ROUTE_ALGO_WORKPLAN.md` and this file ever conflict on a shared file, the algo
plan wins on scoring/generation behavior; this plan wins on structure/infra. Neither
may regress the home-region golden route (see Regression gate).

---

## 0. Read this before touching code

### Prime directives (hold for EVERY task here)

- **Keep the architecture.** `engine` functions stay pure (no I/O beyond HTTP).
  Orchestration stays in `planner.plan_routes` — the single pipeline; never
  reimplement it in a front-end. CLI / web / Discord stay thin pass-throughs.
- **Do not regress the home region.** The grid-farmland (flat IL) ROAD default is
  validated against 108 real rides and is **byte-identical** today. No task here is
  *meant* to change routing output; any that touches scoring/generation must
  reproduce the golden route exactly (Regression gate). Performance work especially
  must change *speed only*, never *which routes come back*.
- **Preserve module public APIs.** Lots of code does `engine.foo(...)`,
  `planner.plan_routes(...)`. If a task moves code, keep the old import paths working
  (re-export) so nothing downstream breaks silently.
- **Verify names against the real code** before editing — the review references may
  have drifted. Confirm signatures first.

### Decisions already made this round (don't relitigate)

- **The test net lands before the perf refactor.** pytest + a one-command runner +
  CI (Task A1) go in *first* so the parallelization refactor (A2) and everything
  after it is guarded. This is deliberate sequencing, not gold-plating.
- **Stay on public ORS for now.** Parallelization works within the free tier's
  ~40 req/min burst; it does not require a self-hosted router (that's algo Task 7).
- **Honesty over silent failure.** Where a data source dies (wind, surface), degrade
  with a user-visible note rather than crashing or guessing (matches algo Task 5).
- **No behavior change without a flag or a no-op default.** Same rule as the algo plan.

---

## Task sequence

| #  | Task | Why | Primary files | Risk |
|----|------|-----|---------------|------|
| A1 | pytest + one-command runner + CI | Safety net for everything below | `pyproject.toml`, `conftest.py`, `.github/workflows/`, `tests/*` | low |
| A2 | Parallelize ORS candidate generation | Biggest UX win (~5–8× faster plans) | `engine.py` (`generate_candidates`) | med |
| A3 | Discord front-end: async + temp-file fix | Real defect in shipped sample | `discord_bot.py` | low |
| B1 | Wind fallback hardening (no crash) | Non-US / dual-outage crashes a plan | `engine.py` (`get_wind`), `planner.py` | low |
| B2 | Packaging: pin deps + `pyproject` extras | Reproducible installs; drop sys.path hack | `requirements*.txt`, `pyproject.toml` | low |
| C1 | Split `engine.py` into focused modules | 1,911 lines / 5 concerns; navigability | `engine.py` → `geocode/wind/routing/scoring` | med-high |
| C2 | Observability: per-plan ORS-call counter | See the quota burn the README warns of | `engine.py`, `planner.py`, `webapp.py` | low |
| D1 | Add a LICENSE | README invites forks but none is legal | `LICENSE` | trivial |
| D2 | Trim doc redundancy (3-place feature lists) | Maintenance tax | `PROJECT_CONTEXT.md`, `README.md` | low |
| D3 | Park `valhalla.py` / note RL scaling | Dead-by-default code; per-process limiter | `valhalla.py`, `webapp.py` | low |

**Tier A** is the agreed starting set (the review's top three). **Tier B** is
correctness/robustness. **Tier C** is maintainability. **Tier D** is housekeeping.

---

## Task A1 — pytest + one-command runner + CI

> **STATUS: DONE (2026-06-21).** Added root `conftest.py` (centralizes the package
> `sys.path` insert for the `pytest` entry point; per-file inserts kept for the
> `python tests/test_x.py` direct-run path), `pyproject.toml`
> `[tool.pytest.ini_options]` (testpaths=tests, `-q`), `requirements-dev.txt`
> (pytest), and `.github/workflows/tests.yml` (3.11, installs deps + dev, runs
> `pytest` on push/PR — ready for when the repo is pushed to GitHub). Verified:
> `pytest` from the project root runs all suites — **69 passed**. Existing `_run()`
> blocks left intact (no-pytest path preserved). **Next: A2 (or A3).**

**Problem:** 69 good offline tests exist, but each file hand-rolls its own `_run()`
loop and must be invoked one at a time; every file repeats a
`sys.path.insert(0, ...)` to import the package; there is no `pytest`, no single
command, and no CI. The web smoke test exists *because* a template/view drift shipped
a 500-on-every-plan — exactly what CI would catch on push.

**Build:**
- Add `conftest.py` at the repo root so the package imports without the per-file
  `sys.path` hack (collection inserts the root on `sys.path`).
- Add `pyproject.toml` `[tool.pytest.ini_options]` (testpaths = `tests`, quiet).
- The existing `test_*` functions already use plain `assert`, so they collect as-is.
  Keep each file's `if __name__ == "__main__": _run()` block (don't break the
  no-pytest path the project has used), but `_run()` is no longer the primary entry.
- Add `.github/workflows/tests.yml`: on push/PR, set up Python 3.11, install deps +
  pytest, run the suite. (Repo isn't on GitHub yet — the workflow is ready for when
  it is, per README's "push to GitHub" step.)
- Record `pytest` as a dev dependency (coordinated with B2).

**Acceptance:**
- `pytest` from the project root runs all suites and reports the same pass count the
  manual runners do (69 today).
- A fresh checkout + `pip install` + `pytest` is green with no `PYTHONPATH` fiddling.
- The CI workflow file is valid YAML and runs the suite on a clean runner.

---

## Task A2 — Parallelize ORS candidate generation

> **STATUS: DONE (2026-06-21).** `generate_candidates` now dispatches each candidate
> build through a bounded `concurrent.futures.ThreadPoolExecutor` (new `workers`
> param, default `engine.ORS_MAX_WORKERS=6`). Per-shape seed indices are assigned up
> front and results slotted back into plan order, so the **route set + tie-break
> ordering are identical to the serial path**; a seed that HTTPErrors is still
> skipped; all-fail still raises `RuntimeError`. `workers=1` reproduces the old
> fully-serial path; `sleep` kept for back-compat but is now unused (concurrency
> replaces manual pacing); the per-call 429 back-off in `_ors_directions` is
> untouched. Offline tests in `tests/test_generate_concurrency.py` (serial==concurrent
> set+order, speedup, seed-skip, all-fail-raises). Full suite: **73 passed.**
> Live wall-time win needs an ORS key to confirm, but the call count is unchanged so
> the free-tier envelope holds. **Next: A3 (done) → B tier.**

**Problem:** `engine.generate_candidates` issues ~12 ORS POSTs strictly sequentially
with `time.sleep(0.4)` between each — the bulk of the 20–40 s/plan, and in the web app
it holds a worker that whole time. The calls are independent.

**Build:**
- Replace the sequential loop with a bounded `concurrent.futures.ThreadPoolExecutor`
  (start `max_workers=6`, a module constant). Each task builds one candidate
  (`_make_polygon_loop` / `_make_rectangle` / `_make_lollipop` / ...).
- Stay within ORS free-tier limits: ~40 req/min burst, so ≤ ~6–8 in flight is safe.
  Keep the existing 429 back-off in `_ors_directions` (it already retries once).
  Drop or shrink the inter-call `sleep` — concurrency replaces the manual pacing.
- Preserve current semantics: a seed that raises `HTTPError` is skipped, not fatal;
  `evaluate` already sorts, so **result set + ordering must be identical to
  sequential** — only latency changes.
- Make the pool size overridable (arg or constant) so a low-quota user can set 1
  (= today's serial behavior) for debugging.
- Surface a clear error if the executor swallows the "no routes at all" case
  (keep the existing `RuntimeError`).

**Test (no live key needed):** stub the `_make_*` builders (or `_ors_directions`) to
return canned candidates with a small artificial delay; assert (a) the same set comes
back as the serial path, (b) wall time is well under serial, (c) one builder raising
`HTTPError` is skipped without sinking the batch, (d) `max_workers=1` reproduces
serial order.

**Acceptance:**
- Same candidates/scores as before for a fixed seed set; plan wall time materially
  lower. ORS calls per plan unchanged in *count* (only concurrency differs), within
  free-tier burst. Golden route byte-identical.

---

## Task A3 — Discord front-end: async + temp-file fix

> **STATUS: DONE (2026-06-21).** Extracted the blocking pipeline+render into a sync
> `_build_plan(...)` and call it via `await asyncio.to_thread(...)` so a `!route` no
> longer freezes the event loop. Each request renders into its own
> `tempfile.mkdtemp(prefix="windroute-")` dir (no more fixed-path collisions),
> cleaned up in a `finally` via `shutil.rmtree`. Still a thin pass-through over
> `planner.plan_routes` — no logic duplicated. `py_compile` clean (discord.py not
> installed locally; full live test needs a token, not required to land).

**Problem:** `discord_bot.py` calls the fully-synchronous `planner.plan_routes`
(uses `requests` + `time.sleep`) directly inside `async def on_message`, blocking the
entire event loop for 20–40 s per `!route` — no other message is processed. Also the
output path is fixed (`tempfile.gettempdir()/windroute.png|.gpx`), so two concurrent
rides clobber each other's files.

**Build:**
- Run the blocking pipeline off the loop: `await asyncio.to_thread(planner.plan_routes,
  ...)` (Py 3.9+). Render/GPX writes can move into the same worker.
- Use a unique temp path per request (e.g. `tempfile.mkdtemp()` or a uuid stem) and
  clean it up after sending.
- Keep it a thin pass-through; no scoring/routing logic added. Still "not wired in,"
  but correct as a copyable example.

**Acceptance:**
- Two `!route` commands in flight don't block each other or overwrite files (verify by
  reasoning/structure; full live test needs a Discord token — not required to land).
- No logic duplicated from `planner`.

---

## Task B1 — Wind fallback hardening (no crash on dual outage / non-US)

> **STATUS: DONE (2026-06-21).** `get_wind` now wraps BOTH sources (Open-Meteo →
> NWS) against `(RequestException, ValueError, KeyError, IndexError)`; if both fail it
> returns a calm `Wind(..., known=False)` instead of letting the NWS 404 (non-US) /
> dual outage propagate. Added `Wind.known: bool = True`; `evaluate` neutralizes the
> wind term when `known=False` (`wind_score=0.0`, `wind_norm=0.5`) so it can't bias
> direction — ranking falls to surface/traffic/path/shape. `planner` appends a
> user-facing note. Happy path byte-identical (default `known=True`). Tests in
> `tests/test_wind.py` (dual-failure degrades, NWS fallback still succeeds, evaluate
> neutralizes). Full suite: **76 passed.**

**Problem:** `engine.get_wind` catches Open-Meteo's `RequestException` and falls to
NWS — but NWS 404s outside the US, and that `HTTPError` is raised *from inside* the
except block, so it propagates out of `get_wind` and kills the plan. A non-US start
when Open-Meteo is throttled = crash, not a degraded plan.

**Build:**
- Wrap the NWS fallback too; if both sources fail, return a sentinel calm `Wind`
  (speed 0, a defined direction) and have `planner` append a note ("couldn't fetch
  wind here — planned without a wind line"). Wind scoring of a calm wind is neutral,
  so routes still come back.
- Keep today's happy path byte-identical (Open-Meteo succeeds → unchanged).

**Acceptance:**
- Simulated dual failure (stub both fetchers to raise) returns a calm `Wind` + note,
  no exception. US happy path unchanged.

---

## Task B2 — Packaging: pin deps, `pyproject`, dev/optional extras

> **STATUS: DONE (2026-06-21).** `pyproject.toml` now carries a full `[project]` table
> (installable: `pip install -e .[dev]` / `.[discord]`), runtime deps with upper
> bounds (`<3`, `<4`, `Pillow<13`, etc.) so a surprise MAJOR can't break a fresh
> install, and `[build-system]` + `[tool.setuptools]` (packages=windroute,
> py-modules=webapp,discord_bot). `requirements.txt` updated to the same bounds and
> kept as the source for `run.bat` (double-click path stays build-tool-free). CI now
> installs via `pip install -e ".[dev]"` (also validates the build). Verified: editable
> install builds, `import windroute, webapp` works from an unrelated cwd, suite still
> **76 passed.** Decision: kept the per-file `sys.path.insert` + `_run()` blocks (they
> serve the `python tests/test_x.py` direct-run path the project values); `conftest.py`
> covers the `pytest` path. So the hacks stay by choice, not removed.

**Problem:** `requirements.txt` has no upper bounds (a future Flask 4 / Pillow 11 can
break a fresh `run.bat` months from now), and `discord.py` (optional front-end) and
`pytest` (dev) are captured nowhere.

**Build:**
- Add a `pyproject.toml` declaring the package, runtime deps with sane bounds (pin at
  least majors for the hosted path), and extras: `[dev]` (pytest) and `[discord]`
  (discord.py). Enables `pip install -e .` and `pip install -e .[dev]`.
- Keep `requirements.txt` working for `run.bat` (or have it mirror the runtime pins).
- Once installable, the test `sys.path` hack is fully removable (folds into A1).

**Acceptance:**
- `pip install -e .[dev]` then `pytest` is green. `run.bat` still builds and launches.
- Hosted deploy (`waitress-serve webapp:app`) unaffected.

---

## Task C1 — Split `engine.py` into focused modules

> **STATUS: DONE (2026-06-21).** `engine.py` (1,911 lines) split into `models.py`
> (67), `geometry.py` (140), `geocode.py` (243), `wind.py` (203), `routing.py` (790),
> `scoring.py` (562); `engine.py` is now a 26-line compatibility facade that
> re-exports every name (public + private) so all `engine.NAME` call sites + imports
> are unchanged. Done mechanically via an `ast`-based splitter (bodies moved verbatim,
> not retyped; script removed after). Dependency layering is acyclic: models/geometry
> are leaves; geocode leaf; wind→(models,geometry,geocode); routing→(models,geometry,
> valhalla); scoring→(models,geometry,routing). Monkeypatch caveat: the facade can't
> absorb test rebinding, so `test_wind`/`test_weights`/`test_generate_concurrency` now
> patch the HOME modules (`routing._ors_directions`, `wind._wind_from_*`,
> `routing._candidate_from_waypoints`, `routing._make_*`) — documented in the facade.
> Verified: full suite **76 passed** (2.4s, no stray network), `python -m windroute.cli
> --help` imports clean, direct-run test path works. PROJECT_CONTEXT file-map updated.

**Problem:** `engine.py` is 1,911 lines spanning five concerns (geocode, wind
providers, ORS routing + geometry, scoring, option selection). Any change requires
holding the whole file in your head; the seams are already clean.

**Build (compatibility-first):**
- Carve into `windroute/geocode.py`, `windroute/wind.py`, `windroute/routing.py`
  (ORS + shapes + geometry), `windroute/scoring.py` (weights, `evaluate`,
  `select_route_options`). Keep `Candidate`/`Wind`/`RouteOption` in a small
  `models.py` (or wherever minimizes cross-imports).
- **Preserve the public surface:** `engine.py` becomes a thin facade that re-exports
  the moved names (`from .scoring import evaluate, ...`) so every existing
  `engine.foo` call site and test keeps working unchanged. Migrate call sites
  opportunistically, not as a big-bang.
- Move incrementally, one concern per commit, running `pytest` after each.

**Acceptance:**
- `pytest` green after each extraction; golden route byte-identical; no front-end or
  test edited just to chase a moved import (the facade absorbs it).

---

## Task C2 — Observability: per-plan ORS-call counter

> **STATUS: NOT STARTED.**

**Problem:** the README warns about the ~2,000 ORS calls/day free cap, but nothing
counts the burn. No way to see when a hosted instance is near the limit.

**Build:**
- Count ORS directions calls per plan (a counter incremented in `_ors_directions`,
  threaded back through `generate_candidates` / refinement, or a lightweight
  thread-safe tally). Surface the count in `PlanResult` (or a note) and log it in the
  web app per request. Optionally a process-lifetime running total in logs.
- Keep it cheap and side-effect-free in `engine` (pure-ish: return the count, don't
  print). Front-ends log it.

**Acceptance:**
- A plan logs "N ORS calls"; the number matches the ~12–15 envelope. No behavior change.

---

## Task D1 — Add a LICENSE

> **STATUS: NOT STARTED.**

README has a whole "share it / fork it / keep these attributions" section but ships no
project license, so others can't legally fork. Add one (MIT suggested; confirm with
owner). Make sure it doesn't conflict with the data-source attributions already noted.

**Acceptance:** `LICENSE` present; README links it.

---

## Task D2 — Trim documentation redundancy

> **STATUS: NOT STARTED.**

`README.md` and `PROJECT_CONTEXT.md` both restate the full feature list, options
table, and data sources; every new feature now gets written up in three places
(README, PROJECT_CONTEXT "Features built", and a workplan STATUS line). Trim
PROJECT_CONTEXT's "Features built" toward pointers ("see README §X / code") and keep
it as the *decisions + gotchas* source of truth, which is where it's uniquely valuable.

**Acceptance:** no feature documented in full in 3 places; PROJECT_CONTEXT still
carries the non-obvious decisions/gotchas. (Owner reviews — docs are taste.)

---

## Task D3 — Park `valhalla.py`; note rate-limiter scaling

> **STATUS: NOT STARTED.**

- `valhalla.py` is shipped, gated off, untested against any live server, and needs a
  custom costing model to do anything (per its own docstring). Consider moving it to a
  branch until real, or at minimum keep the honest "experimental seam" labeling and
  ensure it's import-clean and excluded from coverage expectations.
- The web rate limiter's `_rl_hits` is per-process in-memory; on multi-worker
  waitress / multiple instances the effective limit is N× intended. Fine for a hobby
  host — add a one-line comment so a future "bump the workers" change doesn't silently
  weaken quota protection.

**Acceptance:** decision recorded (park vs keep); limiter caveat documented in code.

---

## Regression gate (run before considering any task done)

- **Golden route:** a fixed grid-farmland plan (the Chicago example, fixed
  date/time/seed) reproduces its recommendation + 2 alternatives (shape, length,
  score, key roads). Perf/structure tasks (A2, C1) must be byte-identical here.
- **Test suite green:** `pytest` passes (once A1 lands). New behavior gets a test.
- **Purity:** new `engine`-side functions take inputs / return values, no I/O beyond
  HTTP; `planner.plan_routes` stays the only pipeline; front-ends only pass through.
- **Budget:** ORS calls per plan stay within the free-tier envelope (~12–15). A2
  changes concurrency, not count.
- **Public API intact:** existing `engine.*` / `planner.*` import paths still resolve.

---

## Suggested order of attack

Land **A1** (test net + CI) first — it guards everything else and is fully verifiable
offline today. Then **A2** (parallelize — the headline win) with the net in place, and
**A3** (Discord fix — small, contained). **B1**/**B2** next (cheap robustness +
reproducible installs). **C1** when you want the engine navigable again (incremental,
behind the facade). **C2**/**D1–D3** as housekeeping whenever there's room.
