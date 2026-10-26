#!/usr/bin/env python3
"""CI check: a brief endpoint failure never switches a quiet window to local estimation.

Regression for a field incident: the usage endpoint answered HTTP 429 on and
off for an hour (several tools polled it). Each failure fell back to
get_usage_local, whose budget came from the limit-hit heuristic, and a
burn-rate warning paused two sessions at "93.4%" while the endpoint read 19%.

Runs in-process against src/ with the endpoint stubbed, and with POWERNAP_HOME
and the transcript directory sandboxed, so it makes no network call and never
touches a live install.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="powernap-grace-"))
HOME = ROOT / "powernap"
PROJECTS = ROOT / "projects"
HOME.mkdir()
(PROJECTS / "-test").mkdir(parents=True)
os.environ["POWERNAP_HOME"] = str(HOME)  # read at import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "claude_powernap"))
import powernap_common as pc  # noqa: E402
import usage_check as uc  # noqa: E402

pc.PROJECTS_DIR = PROJECTS          # transcripts come from the sandbox only
pc.HOME = ROOT                      # no .claude.json: no account-switch handling
uc.watcher_scheduled = lambda: True  # no setup nudge in the output

# One heavy block: 950k weighted tokens against a 1M budget reads 95% locally,
# above the 90% threshold, while the endpoint says 20%.
rec = {"type": "assistant", "sessionId": "grace-test", "requestId": "req-grace-1",
       "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
       "message": {"usage": {"input_tokens": 950_000, "output_tokens": 0,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0}}}
(PROJECTS / "-test" / "grace-test.jsonl").write_text(json.dumps(rec) + "\n")


def write_config(**over):
    cfg = {"enabled": True, "threshold_pct": 90, "check_interval_s": 120,
           "safety_margin_min": 9, "endpoint_enabled": True,
           "local_budget_weighted_tokens": 1_000_000}
    cfg.update(over)
    (HOME / "config.json").write_text(json.dumps(cfg))


ENDPOINT = {"reply": None, "calls": 0}


def fake_endpoint(state):
    ENDPOINT["calls"] += 1
    return ENDPOINT["reply"]


pc.get_usage_from_endpoint = fake_endpoint  # get_usage resolves it as a module global


def reading(pct):
    resets = datetime.fromtimestamp(time.time() + 3 * 3600, tz=timezone.utc).isoformat()
    return {"source": "endpoint", "pct": pct, "resets_at": resets,
            "weekly_pct": 10, "weekly_resets_at": None}


def check(session, reply, **state_over):
    """One hook check (throttle passed unless state_over sets last_check)."""
    ENDPOINT["reply"], ENDPOINT["calls"] = reply, 0
    state = pc.load_state()
    state["last_check"] = 0
    state.update(state_over)
    pc.save_state(state)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        uc.run_check(pc.load_config(), session, "PostToolUse", debug=False, force_local=False)
    return buf.getvalue(), pc.load_state()


write_config()

# 1. A good endpoint reading: no warning, the success time is recorded.
out, st = check("grace-test", reading(20))
assert "USAGE LIMIT WARNING" not in out, f"warned at 20%: {out!r}"
assert st["last_source"] == "endpoint" and st["last_pct"] == 20, st
samples = list(st["samples"])
print("scenario 1 OK: endpoint 20%, no warning")

# 2. The endpoint fails right after (a 429): no switch to the 95% local
#    estimate, no warning, the last reading and its samples stand.
out, st = check("grace-test", None)
assert "USAGE LIMIT WARNING" not in out, f"brief failure fell back and warned: {out!r}"
assert st["last_source"] == "endpoint" and st["last_pct"] == 20, st
assert st["samples"] == samples, f"samples changed: {st['samples']} vs {samples}"
print("scenario 2 OK: failure inside the grace window skipped the check")

# 3. Near the limit the skip never applies: endpoint 74% -> 80% over 4 min
#    (1.5%/min), last read 5 min ago, so the trend is ~8 min from 100%, inside
#    the 9-min safety margin. A failure there falls back to local as before.
now = time.time()
out, st = check("grace-test-near", None, last_source="endpoint", last_pct=80,
                samples=[[now - 540, 74], [now - 300, 80]], endpoint_ok_ts=now - 300)
assert "USAGE LIMIT WARNING" in out and "approximate local estimate" in out, \
    f"skip hid a warning the endpoint trend calls for: {out!r}"
print("scenario 3 OK: near-limit trend falls back to local, no hidden warning")

# 4. A real reading above the threshold still warns.
out, st = check("grace-test", reading(92))
assert "USAGE LIMIT WARNING" in out and "consumed 92% of" in out, f"no warning: {out!r}"
assert "approximate local estimate" not in out, out
print("scenario 4 OK: endpoint 92% still warns")

# 5. A longer outage (last good read 16 min ago) falls back to local as before.
out, st = check("grace-test-2", None, endpoint_ok_ts=time.time() - 16 * 60)
assert "USAGE LIMIT WARNING" in out and "approximate local estimate" in out, \
    f"no local fallback after the grace window: {out!r}"
assert st["last_source"] == "local", st
print("scenario 5 OK: outage past the grace window falls back to local")

# 6. endpoint_failure_grace_min = 0 restores the immediate fallback.
write_config(endpoint_failure_grace_min=0)
check("grace-test-3", reading(20))
out, st = check("grace-test-3", None)
assert st["last_source"] == "local" and "approximate local estimate" in out, \
    f"grace 0 did not fall back at once: {out!r}"
print("scenario 6 OK: grace 0 falls back at once")

print("endpoint grace test OK")
