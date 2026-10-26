#!/usr/bin/env python3
"""Claude Code hook: warn the live session when the 5h usage window nears its limit.

Registered as PostToolUse / UserPromptSubmit / Stop hook. Reads hook JSON on
stdin; at/above threshold emits hookSpecificOutput.additionalContext with the
pause-and-self-resume protocol. Silent otherwise.

Flags: --debug (print usage, no throttle)  --force-local (skip endpoint)
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from powernap_common import (CHECKPOINT_DIR, effective_interval, fmt_local,
                             load_config, load_state, log,
                             projected_minutes_to_limit, record_sample,
                             save_state, state_lock, watcher_scheduled,
                             get_usage, get_usage_local)


def build_warning(pct, resets_iso, session_id, cfg, source, projected=None):
    resume_at = "the reset time"
    try:
        dt = datetime.fromisoformat(resets_iso.replace("Z", "+00:00"))
        resume_at = (dt + timedelta(minutes=cfg["resume_grace_min"])).astimezone() \
            .strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, AttributeError):
        pass
    ckpt = CHECKPOINT_DIR / f"{session_id or 'unknown-session'}.md"
    approx = " (approximate local estimate)" if source == "local" else ""
    pace = (f" At the current burn rate the limit is ~{projected:.0f} minutes away."
            if projected is not None and projected > 0 else "")
    return (
        f"[claude-powernap] USAGE LIMIT WARNING: you have consumed {pct}% of the "
        f"5-hour usage window{approx}. The window resets at {fmt_local(resets_iso)}.{pace} "
        f"To avoid dying mid-task, follow this pause protocol NOW:\n"
        f"1. Do NOT start new work. Finish or safely stop the current step only.\n"
        f"2. Write a checkpoint file to {ckpt} covering: work completed, current "
        f"state, and exact next steps so you can resume cold.\n"
        f"3. SCHEDULE YOUR OWN WAKE-UP — this step is what makes the pause "
        f"recoverable. Create a ONE-SHOT scheduled task (via your scheduled-tasks "
        f"/ cron capability, e.g. CronCreate) that fires at {resume_at} with this "
        f"prompt: 'Session window has reset. Read {ckpt} and continue the work "
        f"from where it left off.' Without this wake-up task the session will "
        f"sit idle after the reset instead of resuming.\n"
        f"4. Tell the user you are pausing until {resume_at}, then END your turn "
        f"and idle. Do not keep working past this warning.\n"
        f"If you have no scheduled-task capability, still write the checkpoint, "
        f"then tell the user to send any message after {resume_at} to resume."
    )


def build_weekly_warning(pct, resets_iso, session_id, cfg):
    ckpt = CHECKPOINT_DIR / f"{session_id or 'unknown-session'}.md"
    return (
        f"[claude-powernap] WEEKLY LIMIT WARNING: you have consumed {pct}% of the "
        f"WEEKLY usage window, which resets {fmt_local(resets_iso)} — days away, "
        f"not hours. Auto-resume does not apply here. Do this NOW:\n"
        f"1. Do NOT start new work. Finish or safely stop the current step only.\n"
        f"2. Write a checkpoint file to {ckpt} covering: work completed, current "
        f"state, and exact next steps.\n"
        f"3. Tell the user plainly: the weekly limit is nearly exhausted, work is "
        f"stopping to preserve the remainder, and it resets {fmt_local(resets_iso)}. "
        f"Do NOT schedule a resume task.\n"
        f"4. END your turn and idle."
    )


def _reset_bucket(resets_iso):
    """Reset time coarsened to the hour, for stable dedup keys across drift."""
    try:
        ep = datetime.fromisoformat(
            (resets_iso or "").replace("Z", "+00:00")).timestamp()
        return str(int(ep // 3600))
    except ValueError:
        return resets_iso or "unknown"


NUDGE_RETRY_S = 14 * 86400  # if declined/forgotten, mention again in two weeks


def watcher_nudge(state, event, now):
    """One-time setup nudge when the fallback watcher isn't scheduled.

    The plugin install activates hooks with no way to register an OS-level
    job, so the hook itself detects the gap and asks (option b): full
    monitoring works either way; the watcher adds crash/hard-limit rescue.
    """
    last = state.get("watcher_nudge_ts")
    if last and now - last < NUDGE_RETRY_S:
        return None
    if watcher_scheduled():
        state["watcher_nudge_ts"] = now + 100 * 365 * 86400  # present; never ask
        return None
    state["watcher_nudge_ts"] = now
    cli = Path(__file__).resolve().parent / "cli.py"
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": (
        "[claude-powernap] SETUP NOTE (one-time, not urgent): powernap's usage "
        "monitoring is active, but its fallback watcher — the background job "
        "that rescues sessions which hard-hit the limit or miss their wake-up "
        "after a sleep/reboot — is not scheduled on this machine. When "
        "convenient (do NOT interrupt current work), tell the user and offer "
        f"to run: python3 \"{cli}\" watcher-setup  (plugin users can also run "
        "/powernap:watcher). If they decline, drop the subject; this note "
        "repeats at most every two weeks."
    )}}


def main():
    argv = sys.argv[1:]
    debug = "--debug" in argv
    force_local = "--force-local" in argv

    cfg = load_config()
    if not cfg.get("enabled") and not debug:
        return

    hook_input = {}
    if not sys.stdin.isatty():
        try:
            hook_input = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            hook_input = {}
    session_id = hook_input.get("session_id", "")
    if not session_id:
        # Fall back to the transcript filename so multi-session dedup,
        # checkpoints, and the pause ledger still work per-session.
        tp = hook_input.get("transcript_path", "")
        session_id = Path(tp).stem if tp else ""
    event = hook_input.get("hook_event_name", "PostToolUse")

    # One checker at a time: state.json is shared across sessions, so if
    # another hook/watcher holds the lock, its check covers this one too.
    with state_lock() as acquired:
        if not acquired and not debug:
            return
        # Debug may run without the lock, but must then never write state —
        # an unlocked save can drop concurrent sessions' ledger updates.
        run_check(cfg, session_id, event, debug, force_local, save_ok=acquired)


def run_check(cfg, session_id, event, debug, force_local, save_ok=True):
    persist = save_state if save_ok else (lambda _s: None)
    state = load_state()
    now = time.time()
    throttle = effective_interval(cfg, state.get("last_pct"))
    if not debug and now - state.get("last_check", 0) < throttle:
        return
    state["last_check"] = now

    usage = get_usage_local(state) if force_local else get_usage(state, cfg)
    if usage["source"] == "unavailable":
        # Endpoint gap inside the grace window: no reading this round.
        # The last reading and its samples stand; the next check retries.
        if debug:
            print(json.dumps(usage, indent=2))
        persist(state)
        return
    if usage["source"] != state.get("last_source"):
        state["samples"] = []  # endpoint and local pct are different scales
    if usage.get("pct") is not None:
        record_sample(state, usage["pct"], now)
    projected = projected_minutes_to_limit(state, now)
    state.update({"last_pct": usage.get("pct"), "last_source": usage["source"],
                  "last_resets_at": usage.get("resets_at"),
                  "last_weekly_pct": usage.get("weekly_pct"),
                  "projected_min_to_limit": round(projected, 1) if projected else None})

    if debug:
        print(json.dumps({**usage, "projected_min_to_limit": projected}, indent=2))
        persist(state)
        return

    # Stop fires after the turn ends: additionalContext emitted here reaches
    # no model turn, so emitting AND recording the warned-marker would silently
    # eat the warning for the whole window. Keep the state/sample updates, but
    # defer all messaging to the next PostToolUse/UserPromptSubmit.
    if event == "Stop":
        persist(state)
        return

    warned = state.setdefault("warned", {})

    # Weekly guard (opt-in, default off): a wall the 5h machinery can't help
    # with — reset is days out, so pause WITHOUT scheduling a resume.
    wpct, wresets = usage.get("weekly_pct"), usage.get("weekly_resets_at")
    if (cfg.get("weekly_guard") and wpct is not None
            and wpct >= cfg.get("weekly_threshold_pct", 90)):
        weekly_key = f"{session_id}:weekly:{_reset_bucket(wresets)}"
        if weekly_key not in warned:
            warned[weekly_key] = now
            log(f"WEEKLY-WARN session={session_id} weekly_pct={wpct} resets={wresets}")
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": build_weekly_warning(wpct, wresets,
                                                          session_id, cfg),
            }}))
            persist(state)
            return  # weekly wall supersedes the 5h protocol

    # While a weekly stop is in force for this session, the 5h protocol's
    # "schedule a resume" instruction would contradict it — stay quiet.
    if any(k.startswith(f"{session_id}:weekly:") and now - v < 7 * 86400
           for k, v in warned.items()):
        persist(state)
        return

    pct, resets = usage.get("pct"), usage.get("resets_at")
    # Fire on the static threshold OR when the burn rate projects the limit
    # inside the safety margin (catches fast burns the threshold would miss).
    over_threshold = pct is not None and pct >= cfg["threshold_pct"]
    burn_trigger = (projected is not None and pct is not None and pct >= 50
                    and projected <= cfg["safety_margin_min"])
    # One warning per session per window. Keyed by the reset HOUR, not the
    # raw string — endpoint/local resets_at drift would otherwise mint a new
    # key mid-window and re-warn.
    warn_key = f"{session_id}:{_reset_bucket(resets)}"
    if (over_threshold or burn_trigger) and warn_key not in warned:
        warned[warn_key] = now
        # Drop stale warn entries (> 24h).
        for k in [k for k, v in warned.items() if now - v > 86400]:
            del warned[k]
        # Pause ledger: the watcher guarantees recovery even if this session
        # fumbles (or never creates) its own scheduled resume task.
        if session_id:
            state.setdefault("paused", {})[session_id] = {
                "paused_at": now, "resets_at": resets}
        why = (f"burn-rate projection: ~{projected:.0f} min to limit"
               if burn_trigger and not over_threshold else "threshold")
        log(f"WARN session={session_id} pct={pct} trigger={why} "
            f"source={usage['source']} resets={resets}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": build_warning(pct, resets, session_id, cfg,
                                               usage["source"], projected),
        }}))
    else:
        # No warning this round — safe slot for the one-time watcher nudge.
        nudge = watcher_nudge(state, event, now)
        if nudge:
            log("nudge: watcher not scheduled; asked session to offer setup")
            print(json.dumps(nudge))
    persist(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the user's session over a monitor bug
        try:
            log(f"usage_check error: {e!r}")
        except Exception:
            pass  # even logging must not turn into a nonzero exit
    sys.exit(0)
