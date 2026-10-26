# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Fixed
- A brief endpoint failure no longer switches a quiet window to the local
  estimate. Field incident: the usage endpoint answered HTTP 429 on and off
  for an hour (several tools polled it); each failure dropped to the local
  estimate (all transcripts on the machine, budget from the limit-hit
  heuristic) and a burn-rate warning paused two sessions at "93.4%" while
  the endpoint read 19%. A failed read within `endpoint_failure_grace_min`
  (default 15) of a good one now returns `source: "unavailable"` as long as
  the last endpoint reading, carried forward at its burn rate, stays below
  `threshold_pct` and outside `safety_margin_min`; the hook then keeps the
  last reading and its samples and warns no one. Near the limit, after the
  grace window, after an account switch, and on installs that never had an
  endpoint reading, it falls back to local estimation as before.

### Added
- `scripts/ci_endpoint_grace_test.py`: in-process regression test (endpoint
  stubbed, transcripts and state sandboxed); wired into unix and windows CI.

## [0.3.2] - 2026-07-18

### Fixed
- Local estimation can no longer emit wildly wrong warnings (field incident:
  "1085.7% of the 5h window" with negative minutes-to-limit) when a leftover
  test/demo config carries a tiny `local_budget_weighted_tokens`:
  - Budgets under 100,000 weighted tokens are ignored on config load as
    test/demo leftovers (diagnostic logged, throttled hourly).
  - A local estimate above 150% now discards itself (the budget is garbage)
    with a diagnostic naming block tokens, budget, and budget source;
    estimates between 100% and 150% clamp to 100%.
  - `projected_minutes_to_limit` is floored at 0 — never negative — and the
    warning's "~N minutes away" clause only renders for positive projections.
- Endpoint fallback is no longer silent: a missing/expired OAuth token logs
  "no valid OAuth token; falling back to local estimation" (throttled hourly).

### Added
- `scripts/ci_estimate_sanity_test.py`: sandboxed regression test (tiny
  budget + heavy transcript + endpoint disabled must never produce a >150%
  warning or a negative projection); wired into unix and windows CI.

## [0.3.1] - 2026-07-18

### Changed
- Plugin: `/powernap:toggle on|off` split into argument-free `/powernap:on`
  and `/powernap:off` (first-user testing showed the argument form invited
  mistyping).
- `claude-powernap version` now reports the real version from deployed
  copies too (shared VERSION constant, also used in the User-Agent).

### Added
- README: plugin troubleshooting (restart to register commands; CLI install
  fallback when the interactive install doesn't persist).

## [0.3.0] - 2026-07-18

### Added
- **PyPI packaging**: installable via `pip install claude-powernap` /
  `uvx claude-powernap setup`; new `setup` / `remove` CLI subcommands replace
  the platform installer scripts (install.sh/.ps1 remain as thin wrappers).
- **Claude Code plugin**: the repo is its own marketplace —
  `/plugin marketplace add asiagenawi/claude-powernap` then
  `/plugin install powernap@claude-powernap`. Hooks activate with no file
  deployment; `/powernap:watcher` schedules the OS-level fallback watcher;
  `/powernap:status` and `/powernap:toggle` included
  (`watcher-setup`/`watcher-remove` CLI subcommands back them).
- `claude-powernap version` command.
- CI: package build + wheel install + entry-point smoke test job; PyPI
  publish workflow on GitHub releases (trusted publishing).

### Changed
- Repo restructured to a src-layout Python package (`src/claude_powernap/`);
  install logic consolidated into one cross-platform code path.

### Fixed (pre-release audit)
- Rescue paths now share one ledger — a warned session that then hard-hit
  the limit can no longer be resumed twice (forked).
- macOS liveness check fails toward "alive" on lsof errors, matching Windows.
- Warnings are no longer emitted (and marked delivered) on Stop events,
  where injected context reaches no model turn.
- Local estimation dedupes by request/message id — `claude --resume` copies
  history into a new transcript and previously double-counted it.
- Keychain: full-keychain enumeration (`dump-keychain`) now runs only on a
  cache miss, never on the steady-state path; check cadence has a 15s floor.
- Usage requests self-identify as `claude-powernap/<version>` (verified
  accepted) instead of a claude-cli User-Agent.
- uvx installs embed a stable system interpreter (not uv's collectible
  cache path) into hooks/shims/scheduled jobs; re-running setup repairs a
  stale interpreter in existing hooks.
- Account switches also reset burn-rate samples; source flips
  (endpoint↔local) reset them too, preventing phantom burn spikes.
- Warn keys bucket the reset time to the hour, so endpoint drift can't
  re-warn mid-window; an active weekly stop suppresses the contradictory
  5h resume instruction.
- Watcher dry-run is truly read-only; rescue retries on failed resumes;
  resumed/notified ledgers prune after 24h; cwd is shell-quoted (and falls
  back to home when deleted) in resume commands.
- `status` reads state under the lock; `--debug` never writes unlocked.
- Partial `token_weights` config merges over defaults instead of crashing
  the scan; multi-machine calibration guarded by a substantial-usage floor
  and smoothing; missing-budget rescans throttled to hourly.
- Empty hook session_id falls back to the transcript filename.
- Removed a stale committed wheel from `dist/` (would have broken future
  publishes); sdist excludes demo/CI files; README/SECURITY corrections
  (Keychain enumeration disclosure, honest UA, cadence and trigger accuracy,
  uninstall matrix per install method, sleep/`caffeinate` caveat, absolute
  image URLs for PyPI).

## [0.2.0] - 2026-07-18

### Added
- **Burn-rate prediction**: warnings also fire when the recent burn rate
  projects the limit within `safety_margin_min` minutes — catches fast
  (subagent-heavy) burns the static threshold misses.
- **Adaptive check cadence**: 30s above 80% usage, every hook event above 90%.
- **Pause ledger + rescue**: every pause is recorded; the watcher resumes
  napping sessions whose in-session alarm died (sleep/reboot/crash).
- **Weekly guard** (opt-in, `weekly_guard: true`): warn/pause on the weekly
  window; pauses without scheduling a resume since weekly resets are days out.
- **Account-switch detection**: per-account cached state clears automatically
  on `/login` to a different account.
- **Cross-platform state locking**: concurrent sessions can no longer race
  state.json (fcntl/msvcrt; 12-process CI test).

## [0.1.0] - 2026-07-18

Initial release.

### Added
- Usage monitor hook (`PostToolUse`/`UserPromptSubmit`/`Stop`): warns the live
  session at a configurable threshold (default 90%) of the 5-hour window,
  with the pause-checkpoint-reschedule protocol injected into context.
- Hybrid usage source: exact numbers from the OAuth usage endpoint with
  automatic fallback to local transcript estimation, continuously calibrated
  against endpoint truth.
- Fallback watcher: detects sessions that hard-hit the limit, waits for the
  reset, and resumes them visibly (new terminal window) — with a
  never-fork-a-live-session guarantee; notifies instead when in doubt.
- `claude-powernap on|off|status|log` CLI.
- Platforms: macOS (Keychain/launchd/osascript), Linux incl. WSL
  (credentials file/systemd user timer/notify-send, terminal auto-detect),
  native Windows experimental (Task Scheduler/toasts/wt).
- CI: full install→monitor→hook→watcher→uninstall cycle on macOS, Ubuntu,
  and Windows runners.
