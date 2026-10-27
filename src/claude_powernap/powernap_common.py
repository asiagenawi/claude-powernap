"""Shared logic for claude-powernap: config, state, usage sources, block math."""
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
POWERNAP_DIR = Path(os.environ.get("POWERNAP_HOME", HOME / ".claude" / "claude-powernap"))
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
CONFIG_PATH = POWERNAP_DIR / "config.json"
STATE_PATH = POWERNAP_DIR / "state.json"
CHECKPOINT_DIR = POWERNAP_DIR / "checkpoints"
LOG_PATH = POWERNAP_DIR / "powernap.log"

VERSION = "0.3.3"  # single source for deployed copies (UA, cli version)
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE_PREFIX = "Claude Code-credentials"
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
CREDENTIALS_FILE = CLAUDE_DIR / ".credentials.json"  # Linux/WSL/Windows token location

DEFAULT_CONFIG = {
    "enabled": True,
    "threshold_pct": 90,
    "check_interval_s": 120,
    "safety_margin_min": 9,     # warn when projected time-to-limit dips below this
    "weekly_guard": False,      # opt-in: warn/pause on the WEEKLY window too
    "weekly_threshold_pct": 90,
    "endpoint_enabled": True,
    "endpoint_failure_grace_min": 15,    # skip a failed read this soon after a good one
    "terminal_app": "Terminal",          # or "iTerm2"
    "resume_grace_min": 3,               # schedule resume this many min after reset
    "headless_resume_extra_args": [],    # e.g. ["--dangerously-skip-permissions"]
    "local_budget_weighted_tokens": None,  # override for local estimation budget
    "token_weights": {"input": 1.0, "output": 3.0, "cache_creation": 1.0, "cache_read": 0.1},
}


def log(msg):
    POWERNAP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(LOG_PATH, "a") as f:
        f.write(f"{ts} {msg}\n")


def log_throttled(state, key, msg, now=None, interval=3600):
    """Log msg at most once per `interval`, tracked in state under `key`.

    For conditions that persist across many checks (missing token, garbage
    budget): one line per hour keeps the log debuggable without spam.
    """
    now = now or time.time()
    if now - state.get(key, 0) >= interval:
        state[key] = now
        log(msg)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = load_json(CONFIG_PATH, {})
    # Deep-merge token_weights so a partial override can't KeyError the scan.
    weights = {**DEFAULT_CONFIG["token_weights"], **(user.get("token_weights") or {})}
    cfg.update(user)
    cfg["token_weights"] = weights
    # Sanity: a real 5h budget is millions of weighted tokens. A tiny value is
    # a test/demo leftover (CI's threshold step writes 1000) and would inflate
    # pct by orders of magnitude — ignore it, remember why for the log line.
    budget = cfg.get("local_budget_weighted_tokens")
    if budget and budget < 100_000:
        cfg["ignored_local_budget"] = budget
        cfg["local_budget_weighted_tokens"] = None
    return cfg


@contextlib.contextmanager
def state_lock(timeout=2.0):
    """Exclusive advisory lock for state.json read-modify-write cycles.

    Yields True if acquired, False on timeout. Callers that only refresh
    shared state (hooks, watcher) should treat False as "another process is
    already on it" and skip; display-only callers may proceed unlocked.
    fcntl on POSIX, msvcrt on Windows.
    """
    POWERNAP_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = open(POWERNAP_DIR / "state.lock", "a+")
    acquired = False
    try:
        deadline = time.time() + timeout
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
        lock_file.close()


def load_state():
    return load_json(STATE_PATH, {})


def save_state(state):
    POWERNAP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------- keychain

def _keychain_services():
    """All 'Claude Code-credentials*' service names in the login keychain."""
    try:
        out = subprocess.run(["security", "dump-keychain"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return [KEYCHAIN_SERVICE_PREFIX]
    names = set(re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', out))
    return sorted(names) or [KEYCHAIN_SERVICE_PREFIX]


def _read_service_token(svc):
    """(expiresAt_ms, token) for one Keychain service, or None."""
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", svc, "-w"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        oauth = json.loads(raw).get("claudeAiOauth", {})
        tok, exp = oauth.get("accessToken"), oauth.get("expiresAt", 0)
        if tok and exp > time.time() * 1000 + 60_000:
            return exp, tok
    except Exception:
        pass
    return None


def get_oauth_token(state):
    """Freshest unexpired OAuth access token (Keychain on macOS, file elsewhere).

    Fast path: a single find-generic-password on the cached service name.
    The whole-keychain enumeration (dump-keychain) runs ONLY when that cache
    misses — never on the steady-state path.
    """
    if not IS_MAC:
        try:
            with open(CREDENTIALS_FILE) as f:
                oauth = json.load(f).get("claudeAiOauth", {})
            tok, exp = oauth.get("accessToken"), oauth.get("expiresAt", 0)
            return tok if tok and exp > time.time() * 1000 + 60_000 else None
        except (OSError, json.JSONDecodeError):
            return None
    cached = state.get("keychain_service")
    if cached:
        hit = _read_service_token(cached)
        if hit:
            return hit[1]
    candidates = []
    for svc in _keychain_services():
        if svc == cached:
            continue
        hit = _read_service_token(svc)
        if hit:
            candidates.append((hit[0], svc, hit[1]))
    if not candidates:
        return None
    exp, svc, tok = max(candidates)
    state["keychain_service"] = svc
    return tok


# ---------------------------------------------------------------- endpoint

def get_usage_from_endpoint(state):
    """Query the OAuth usage endpoint. Returns usage dict or None.

    NOTE: undocumented endpoint (same one Claude Code's /usage uses). May
    change without notice; callers must tolerate None and fall back.
    """
    token = get_oauth_token(state)
    if not token:
        log_throttled(state, "no_token_log_ts",
                      "no valid OAuth token; falling back to local estimation")
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        # Honest self-identification (verified accepted by the endpoint).
        "User-Agent": f"claude-powernap/{VERSION} (github.com/asiagenawi/claude-powernap)",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log(f"endpoint error: {e}")
        return None
    usage = {"source": "endpoint"}
    # Preferred: limits[] array; fallback: five_hour/seven_day objects.
    for lim in data.get("limits") or []:
        if lim.get("kind") == "session":
            usage["pct"] = lim.get("percent")
            usage["resets_at"] = lim.get("resets_at")
        elif lim.get("kind") == "weekly_all":
            usage["weekly_pct"] = lim.get("percent")
            usage["weekly_resets_at"] = lim.get("resets_at")
    if "pct" not in usage:
        fh = data.get("five_hour") or {}
        usage["pct"] = fh.get("utilization")
        usage["resets_at"] = fh.get("resets_at")
        sd = data.get("seven_day") or {}
        usage["weekly_pct"] = sd.get("utilization")
        usage["weekly_resets_at"] = sd.get("resets_at")
    return usage if usage.get("pct") is not None else None


# ---------------------------------------------------------------- local estimate

def _iter_transcript_events(max_age_h):
    """Yield (ts_epoch, weighted_tokens_or_0, is_rate_limit) from recent transcripts.

    Dedupes by requestId/message.id across files: `claude --resume` copies the
    history into a NEW transcript, so without this a resumed session's usage
    counts twice — poisoning both the estimate and the calibration.
    """
    cfg = load_config()
    w = cfg["token_weights"]
    cutoff = time.time() - max_age_h * 3600
    seen_ids = set()
    if not PROJECTS_DIR.is_dir():
        return
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff:
                continue
            with open(jsonl, errors="replace") as f:
                for line in f:
                    if '"timestamp"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp")
                    if not ts:
                        continue
                    try:
                        ep = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if ep < cutoff:
                        continue
                    rid = rec.get("requestId") or (rec.get("message") or {}).get("id")
                    if rid:
                        if rid in seen_ids:
                            continue
                        seen_ids.add(rid)
                    u = (rec.get("message") or {}).get("usage") or {}
                    weighted = (u.get("input_tokens", 0) * w["input"]
                                + u.get("output_tokens", 0) * w["output"]
                                + u.get("cache_creation_input_tokens", 0) * w["cache_creation"]
                                + u.get("cache_read_input_tokens", 0) * w["cache_read"])
                    is_rl = ((rec.get("error") == "rate_limit" or rec.get("apiErrorStatus") == 429)
                             and "resets" in line and "not your usage limit" not in line)
                    yield ep, weighted, is_rl
        except OSError:
            continue


def _blocks(events):
    """Group sorted (ts, tokens, is_rl) events into 5h blocks (ccusage algorithm)."""
    blocks = []
    for ep, tok, is_rl in events:
        if not blocks or ep >= blocks[-1]["start"] + 5 * 3600:
            start = ep - (ep % 3600)  # floor to hour
            blocks.append({"start": start, "tokens": 0.0, "hit_limit": False})
        blocks[-1]["tokens"] += tok
        blocks[-1]["hit_limit"] = blocks[-1]["hit_limit"] or is_rl
    return blocks


def get_usage_local(state):
    """Estimate 5h-window usage from transcripts. Approximate; may return pct=None."""
    cfg = load_config()
    now = time.time()
    events = sorted(_iter_transcript_events(max_age_h=26))
    blocks = _blocks(events)
    current = blocks[-1] if blocks and now < blocks[-1]["start"] + 5 * 3600 else None

    if cfg.get("ignored_local_budget"):
        log_throttled(state, "bad_budget_log_ts",
                      f"config local_budget_weighted_tokens={cfg['ignored_local_budget']} "
                      f"is implausibly small (<100000); ignoring it (test/demo leftover?)")

    # Budget preference: explicit config > endpoint-calibrated > limit-hit heuristic.
    budget, budget_src = cfg.get("local_budget_weighted_tokens"), "config"
    if not budget:
        budget, budget_src = state.get("calibrated_budget"), "calibrated"
    if not budget and now - state.get("no_hits_scan_ts", 0) > 3600:
        # A block that ended in a rate-limit hit reached ~100% of the budget.
        # The 14-day scan is expensive; when it finds nothing, retry hourly
        # at most instead of on every check.
        cal_events = sorted(_iter_transcript_events(max_age_h=14 * 24))
        hits = [b["tokens"] for b in _blocks(cal_events) if b["hit_limit"]]
        if hits:
            budget = state["calibrated_budget"] = max(hits)
            budget_src = "limit-hit heuristic"
        else:
            state["no_hits_scan_ts"] = now

    if current is None:
        return {"source": "local", "pct": 0.0, "resets_at": None,
                "weekly_pct": None, "weekly_resets_at": None}
    demo_min = cfg.get("demo_resets_in_min")  # testing/demo: pretend the window
    reset_ep = (now + demo_min * 60) if demo_min \
        else current["start"] + 5 * 3600      # resets N minutes from now
    resets_at = datetime.fromtimestamp(reset_ep, tz=timezone.utc).isoformat()
    pct = round(current["tokens"] / budget * 100, 1) if budget else None
    # An estimate this far past 100% means the BUDGET is garbage (leftover
    # test/demo config, bad calibration), not that the user 10x-ed the limit.
    # Better no estimate than a wildly wrong one driving false pauses.
    if pct is not None and pct > 150:
        log_throttled(state, "bad_estimate_log_ts",
                      f"local estimate unreliable: pct={pct} "
                      f"block_tokens={int(current['tokens'])} budget={int(budget)} "
                      f"budget_source={budget_src}; discarding estimate")
        pct = None
    elif pct is not None and pct > 100:
        pct = 100.0  # mild overshoot: local estimation noise, clamp
    return {"source": "local", "pct": pct, "resets_at": resets_at,
            "weekly_pct": None, "weekly_resets_at": None}


def watcher_scheduled():
    """Is the fallback watcher registered with the OS scheduler?"""
    try:
        if IS_MAC:
            r = subprocess.run(["launchctl", "list"], capture_output=True,
                               text=True, timeout=10)
            return "com.claude-powernap.watcher" in r.stdout
        if IS_WIN:
            r = subprocess.run(["schtasks", "/query", "/tn",
                                "claude-powernap-watcher"], capture_output=True,
                               timeout=10)
            return r.returncode == 0
        r = subprocess.run(["systemctl", "--user", "is-active",
                            "claude-powernap.timer"], capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:
        return True  # can't tell -> don't nag


ACCOUNT_KEYS = ("keychain_service", "calibrated_budget", "calibration_ts",
                "samples", "last_pct", "endpoint_ok_ts")


def check_account_switch(state):
    """Clear per-account cached state when the logged-in account changes.

    Usage windows, budgets, and tokens are per-account; a /login to another
    account silently invalidates all of them.
    """
    try:
        with open(HOME / ".claude.json") as f:
            uuid = (json.load(f).get("oauthAccount") or {}).get("accountUuid")
    except (OSError, json.JSONDecodeError):
        return
    if not uuid:
        return
    prev = state.get("account_uuid")
    if prev and prev != uuid:
        for k in ACCOUNT_KEYS:
            state.pop(k, None)
        log(f"account switch detected ({prev[:8]}… -> {uuid[:8]}…); cleared per-account state")
    state["account_uuid"] = uuid


def effective_interval(cfg, pct):
    """Adaptive check cadence: the closer to the limit, the shorter the throttle.

    Never zero — each check costs a token lookup and an HTTPS request, so a
    floor keeps a busy near-limit session from paying that on every tool use.
    """
    base = cfg["check_interval_s"]
    if pct is None:
        return base
    if pct >= 90:
        return min(base, 15)
    if pct >= 80:
        return min(base, 30)
    return base


def record_sample(state, pct, now=None):
    """Append a (ts, pct) usage sample; keep a short rolling history."""
    now = now or time.time()
    hist = state.setdefault("samples", [])
    hist.append([now, pct])
    # New 5h window (pct dropped sharply) -> old samples are meaningless.
    if len(hist) >= 2 and pct < hist[-2][1] - 10:
        del hist[:-1]
    del hist[:-12]


def projected_minutes_to_limit(state, now=None):
    """Minutes until 100% at the recent burn rate, or None if unknown/idle.

    Burn rate is an exponentially-weighted mean of per-sample deltas, so a
    subagent-heavy sprint between checks shows up immediately.
    """
    now = now or time.time()
    hist = [s for s in state.get("samples", []) if now - s[0] < 45 * 60]
    if len(hist) < 2:
        return None
    rate, weight = 0.0, 0.0
    for (t0, p0), (t1, p1) in zip(hist, hist[1:]):
        dt = (t1 - t0) / 60
        if dt <= 0:
            continue
        w = 0.6 ** ((now - t1) / 300)   # halve influence every ~4 min of age
        rate += w * ((p1 - p0) / dt)
        weight += w
    if not weight:
        return None
    rate /= weight
    if rate <= 0.05:                     # effectively idle
        return None
    # Never negative: last_pct above 100 means "already there", not
    # "the limit was minutes ago".
    return max(0.0, 100 - hist[-1][1]) / rate


def _endpoint_gap_is_safe(state, cfg, now=None):
    """May a failed endpoint read be skipped instead of estimated locally?

    Only while the last endpoint reading, carried forward at its recent burn
    rate, is below the threshold and outside the burn-rate safety margin, so
    a skipped check can never hide a warning the endpoint trend calls for.
    """
    now = now or time.time()
    pct, hist = state.get("last_pct"), state.get("samples") or []
    if state.get("last_source") != "endpoint" or pct is None or not hist:
        return False
    projected = projected_minutes_to_limit(state, now)
    if projected is None:  # idle or too few samples: the last reading holds
        return pct < cfg["threshold_pct"]
    left = projected - (now - hist[-1][0]) / 60
    est = 100 - max(left, 0) * (100 - pct) / projected if projected > 0 else 100
    return (est < cfg["threshold_pct"]
            and not (est >= 50 and left <= cfg["safety_margin_min"]))


def get_usage(state, cfg=None):
    cfg = cfg or load_config()
    check_account_switch(state)
    usage = get_usage_from_endpoint(state) if cfg.get("endpoint_enabled") else None
    if usage is None:
        # A failed read shortly after a good one (HTTP 429 when several tools
        # poll the endpoint, a timeout) is a gap, not a reason to switch
        # scales: the local estimate counts every transcript on this machine
        # against an estimated budget and can sit far from the endpoint's
        # figure, so a mid-window switch can pause sessions on a number the
        # endpoint never showed. Skip this check (the next one retries) while
        # the last endpoint reading, carried forward at its burn rate, stays
        # clear of both warning triggers; otherwise fall back as before.
        age = time.time() - state.get("endpoint_ok_ts", 0)
        if (cfg.get("endpoint_enabled")
                and 0 <= age < (cfg.get("endpoint_failure_grace_min") or 0) * 60
                and _endpoint_gap_is_safe(state, cfg)):
            return {"source": "unavailable", "pct": None, "resets_at": None,
                    "weekly_pct": None, "weekly_resets_at": None}
        return get_usage_local(state)
    state["endpoint_ok_ts"] = time.time()
    # Continuously calibrate the local fallback budget against endpoint truth
    # (at most every 6h, only when the window is meaningfully used, and only
    # when THIS machine's block is substantial — on multi-machine accounts the
    # endpoint pct includes remote usage this machine's transcripts can't see,
    # which would otherwise collapse the budget toward zero).
    if usage.get("pct", 0) >= 20 and time.time() - state.get("calibration_ts", 0) > 6 * 3600:
        events = sorted(_iter_transcript_events(max_age_h=26))
        blocks = _blocks(events)
        if (blocks and time.time() < blocks[-1]["start"] + 5 * 3600
                and blocks[-1]["tokens"] >= 500_000):
            new_budget = blocks[-1]["tokens"] / (usage["pct"] / 100)
            prev = state.get("calibrated_budget")
            if prev:  # smooth: a single skewed reading can't swing the budget
                new_budget = 0.5 * prev + 0.5 * new_budget
            state["calibrated_budget"] = new_budget
            state["calibration_ts"] = time.time()
            log(f"calibrated local budget={int(new_budget)} from endpoint pct={usage['pct']}")
    return usage


# ---------------------------------------------------------------- misc

def fmt_local(iso_ts):
    if not iso_ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %I:%M%p %Z")
    except ValueError:
        return iso_ts
