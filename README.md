# claude-powernap

[![CI](https://github.com/asiagenawi/claude-powernap/actions/workflows/ci.yml/badge.svg)](https://github.com/asiagenawi/claude-powernap/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/asiagenawi/claude-powernap)](https://github.com/asiagenawi/claude-powernap/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/asiagenawi/claude-powernap/blob/main/LICENSE)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)
![Deps](https://img.shields.io/badge/deps-stdlib%20only-brightgreen)

**Your Claude Code session sees the usage limit coming, saves its work, and
resumes itself when the window resets. You do nothing.**

*(Demo below is an illustrative mock-up of a real cycle the tool ran.)*

![claude-powernap demo: a session gets warned at 91%, checkpoints, pauses, and self-resumes after the window resets](https://raw.githubusercontent.com/asiagenawi/claude-powernap/main/demo/powernap.gif)

## The problem

You kick off a long Claude Code automation — a big refactor, a batch
migration, an overnight research run. You check back three hours later and
find it died 40 minutes in:

```
You've hit your session limit · resets 3:00am
```

Everything since the last commit is in limbo. The context that session had
built up is sitting frozen mid-thought. And the limit reset hours ago — the
session just had no way to know, no way to save itself, and no way to come
back. Your subscription's 5-hour window reset while your automation sat
dead in a terminal.

## The idea

A session that's *about* to hit the limit still has tokens left. That's
enough budget to do something smarter than die:

1. **A hook watches your usage** (every ~2 min normally, tightening to ~15s
   near the limit; zero workflow changes). It knows your exact window percentage and reset time — the same
   numbers Claude Code's own `/usage` screen shows.
2. **At 90% — or earlier if your burn rate projects the limit within ~9
   minutes — the session itself gets warned**: the hook injects a message
   into the live conversation: *"you're at 91%, window resets at 3:00am,
   pause now."*
3. **Claude wraps up gracefully**: finishes the current step, writes a
   checkpoint file (what's done, what's in flight, exact next steps), and —
   when the session has a scheduled-tasks capability — schedules a one-shot
   task for just after the reset. Then it idles.
4. **At reset, the session wakes itself** — same terminal, same context,
   reads its checkpoint, keeps going. Sessions without a scheduler instead
   checkpoint and tell you when to send any message to resume — and the
   watcher (below) still notifies or reopens as needed.

If the estimate is ever wrong and a session hard-hits the wall anyway, a
background watcher catches that too: after the reset it reopens the session
in a visible terminal window with the "continue from your checkpoint" prompt
already submitted. If the original window is still open, you get a
notification instead — it will never fork a live session.

## How it compares

| | warns before the limit | session saves its work | resumes automatically | needs tmux |
|---|---|---|---|---|
| **claude-powernap** | ✅ | ✅ | ✅ same session | no |
| usage monitors (ccusage, claude-monitor) | ⚠️ warns *you*, not the session | ❌ | ❌ | — |
| auto-retriers (unsnooze, claude-auto-retry, …) | ❌ reacts after death | ❌ dies mid-thought | ✅ | mostly yes |

Free, privacy-first, local-only, no dependencies (Python 3 stdlib only). macOS, Linux (incl.
WSL), native Windows (experimental).

## Install

As a Claude Code plugin (inside any Claude Code session):

```
/plugin marketplace add asiagenawi/claude-powernap
/plugin install powernap@claude-powernap
```

The plugin activates the monitoring hooks immediately. The one thing a
plugin can't do itself is register the fallback watcher (an OS-level
background job) — so on first run the hook notices it's missing and has the
session offer you the one command to set it up (or run `/powernap:watcher`
yourself anytime). Also ships `/powernap:status`, `/powernap:on`, and
`/powernap:off`.

If the `/powernap:*` commands don't appear after installing: restart the
Claude Code session (plugin commands register at session start), and if the
interactive install didn't take, run
`claude plugin install powernap@claude-powernap` from a terminal instead.

From PyPI (all platforms):

```bash
uvx claude-powernap setup        # or: pipx install claude-powernap && claude-powernap setup
```

Or from a clone, if you'd rather read what you run first:

```bash
git clone https://github.com/asiagenawi/claude-powernap.git && cd claude-powernap
./install.sh        # macOS / Linux / WSL — same as `claude-powernap setup`
```

Native Windows clone (PowerShell 7 recommended): `.\install.ps1`

Pick ONE install method — plugin or pip/clone. (Both at once is harmless —
the shared state throttle makes duplicate checks no-ops — but pointless.)

That's the whole setup. Every Claude Code session on the machine is covered —
no per-project config, no special launcher, no flags to remember. Start your
long automations exactly the way you already do.

Setup is deliberately boring and inspectable: it copies a few Python files
to `~/.claude/claude-powernap/`, merges three hook entries into
`~/.claude/settings.json` (backing it up first), and schedules the fallback
watcher (launchd / systemd user timer / Task Scheduler).
`claude-powernap remove` (or `./uninstall.sh`) reverses all of it. If you
set `CLAUDE_CONFIG_DIR`, setup follows it — run setup from a normal shell,
not from inside a Claude Code session, so the right settings file is edited.

On macOS the first usage check may trigger one Keychain prompt — the monitor
reads Claude Code's own OAuth token to ask Anthropic for your real usage
numbers (see Caveats). Click "Always Allow".

## Toggle

```bash
claude-powernap on       # enable everything
claude-powernap off      # disable everything
claude-powernap status   # current 5h + weekly usage, watcher state
claude-powernap log      # what the watcher and monitor have been doing
```

## Configure — `~/.claude/claude-powernap/config.json`

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | master switch (what `on`/`off` flips) |
| `threshold_pct` | `90` | warn/pause threshold for the 5h window |
| `check_interval_s` | `120` | base seconds between checks (auto-tightens to 30s above 80%, every event above 90%) |
| `safety_margin_min` | `9` | also warn when the current burn rate projects the limit within this many minutes |
| `weekly_guard` | `false` | opt-in: also warn/pause on the **weekly** window (no auto-resume — its reset is days away) |
| `weekly_threshold_pct` | `90` | weekly-guard warning threshold |
| `endpoint_enabled` | `true` | `false` = fully local estimation only |
| `endpoint_failure_grace_min` | `15` | for this long after a good endpoint reading, a failed read (e.g. HTTP 429) is skipped instead of switching to local estimation, as long as that reading's trend is clear of the warning triggers; `0` = switch at once |
| `terminal_app` | `Terminal` | macOS: `Terminal`/`iTerm2`; Linux: terminal binary name (auto-detected if unset) |
| `resume_grace_min` | `3` | schedule resume this long after reset |
| `headless_resume_extra_args` | `[]` | extra flags for last-resort headless resume |
| `local_budget_weighted_tokens` | `null` | manual budget for local estimation |
| `token_weights` | in/out/cache | per-token-type weights for local estimation |

## Plays by the rules

claude-powernap works *with* the usage limit, not around it:

- **Zero extra usage.** The same quota, consumed the same way. It never
  gets you more tokens — it just stops a session from dying mid-thought
  and picks the work back up when your own window resets.
- **Genuine client, official hooks.** Everything runs inside the real
  Claude Code client through its documented hooks system. It never spoofs
  a client identity and never touches permission systems.
- **Privacy-first, local-only.** Checkpoints, transcripts, config — all of
  it stays on your machine. The one outbound call is the same usage query
  the built-in `/usage` screen makes, and `endpoint_enabled: false` turns
  even that off.

## Caveats

- **Undocumented usage endpoint** — exact percentages come from the same
  unpublished API `/usage` uses (read-only, via the OAuth token already on
  your machine; a ToS gray area). Handled: if the endpoint fails or
  you set `endpoint_enabled: false`, the tool switches automatically to
  local transcript estimation, self-calibrated whenever endpoint data was
  available. A brief failure right after a good reading (a 429 when
  several tools poll the endpoint) only skips that check while the last
  reading's trend is clear of the warning triggers; otherwise, or after
  `endpoint_failure_grace_min`, it switches.
- **The paused session must stay open** — that's the point. If its alarm
  dies anyway (sleep, reboot, crash), the watcher notices the missed wake-up
  and resumes the session itself.
- **Windows is experimental** — Windows can't reliably tell whether a session
  is still open, so the watcher errs toward caution there: when unsure it
  notifies you instead of auto-resuming (one click, never a forked session).
  CI runs the full cycle on real Windows runners. WSL users: use
  `./install.sh` inside WSL.
- **Sleep beats timers** — a sleeping laptop fires nothing: the in-session
  alarm and the watcher both wait until the machine wakes, then the watcher
  catches up (rescuing any missed wake-ups). For unattended overnight runs
  keep the machine awake, e.g. `caffeinate -is` on macOS.
- **Linux notes**: terminal emulator auto-detected; headless servers fall
  through to headless resume; `loginctl enable-linger $USER` keeps the
  watcher running while logged out.

## Uninstall

By install method:

| installed via | uninstall with |
|---|---|
| plugin | run `python3 ~/.claude/claude-powernap/cli.py watcher-remove` (if you set up the watcher), then `/plugin uninstall powernap` |
| PyPI | `claude-powernap remove` (add `--purge` to delete config/state too), **then** `pip uninstall claude-powernap` — in that order |
| clone | `./uninstall.sh` (or `--purge`); Windows: `.\uninstall.ps1` (or `-Purge`) |

## License

MIT
