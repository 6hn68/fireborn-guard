#!/usr/bin/env python3
"""Fireborn Guard: a spend cap and loop breaker for Claude Code.

Runs as a PreToolUse hook. Before every tool call it reads the current
session transcript, estimates what the session has used at API list price,
and checks for loops:

  - past the soft line ($5 by default): the call goes through, and Claude is
    told the session is getting expensive
  - past the hard line ($15 by default): the call is denied
  - same tool with the same input: warn at 4 in a row, deny at 8
  - same failing shell command: warn after 3 failures in a row, deny after 5
  - sleep / wait / watch style commands never count as a loop

Subcommands:
  hook          read one PreToolUse event on stdin, print the decision
  allow ID      let one session continue past the hard line and loop stops
  status        show where state is kept and the current settings
  share on|off  opt-in anonymous burn-pattern log (default off, local only)

Standard library only. Never stores prompts or file contents. Makes no
network calls.
"""

import argparse
import hashlib
import json
import os
import sys
import time

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICES = os.path.join(HERE, "prices.json")

SOFT_USD = 5.0
HARD_USD = 15.0
SAME_WARN, SAME_STOP = 4, 8
FAIL_WARN, FAIL_STOP = 3, 5
WAIT_WORDS = ("sleep", "wait", "watch ", "tail -f", "timeout")
BUCKETS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
MTOK = 1_000_000


def state_dir():
    d = os.environ.get("FIREBORN_GUARD_HOME") or os.environ.get("CLAUDE_PLUGIN_DATA")
    return os.path.expanduser(d or "~/.fireborn-guard")


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def limits_from_env():
    return {"soft": _env_float("FIREBORN_SOFT_USD", SOFT_USD),
            "hard": _env_float("FIREBORN_HARD_USD", HARD_USD),
            "same_warn": _env_int("FIREBORN_SAME_WARN", SAME_WARN),
            "same_stop": _env_int("FIREBORN_SAME_STOP", SAME_STOP),
            "fail_warn": _env_int("FIREBORN_FAIL_WARN", FAIL_WARN),
            "fail_stop": _env_int("FIREBORN_FAIL_STOP", FAIL_STOP)}


# ---------------------------------------------------------------- pricing

def load_prices(path=None):
    with open(path or DEFAULT_PRICES, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg.get("models"), list):
        raise ValueError("prices.json has no 'models' list")
    return cfg


def match_model(model_id, models):
    """Longest-prefix match, or None when the model is unknown."""
    best, best_len = None, -1
    for m in models:
        for prefix in m.get("match", []):
            if model_id.startswith(prefix) and len(prefix) > best_len:
                best, best_len = m, len(prefix)
    return best


def ceiling(models):
    """The most expensive known rate in every bucket."""
    top = {}
    for m in models:
        for b in BUCKETS:
            v = m.get(b)
            if isinstance(v, (int, float)):
                top[b] = max(top.get(b, 0), v)
            lc = m.get("long_context")
            if isinstance(lc, dict) and isinstance(lc.get(b), (int, float)):
                top[b] = max(top.get(b, 0), lc[b])
    return top


def _tier(price, tokens):
    limit = price.get("long_context_threshold")
    if limit and isinstance(price.get("long_context"), dict):
        prompt = (tokens["input"] + tokens["cache_read"]
                  + tokens["cache_write_5m"] + tokens["cache_write_1h"])
        if prompt > limit:
            return price["long_context"]
    return price


def cost_of(tokens, model, cfg):
    """Return (usd, known_model). Unknown models use the ceiling rate."""
    models = cfg["models"]
    p = match_model(model or "", models)
    known = p is not None
    rates = _tier(p, tokens) if known else ceiling(models)
    top = ceiling(models)
    usd = 0.0
    for b in BUCKETS:
        rate = rates.get(b)
        if not isinstance(rate, (int, float)):
            rate = top.get(b, 0)  # a missing rate is never treated as free
        usd += tokens[b] * rate / MTOK
    return usd, known


def _n(v):
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def parse_usage(u):
    t = {"input": _n(u.get("input_tokens")), "output": _n(u.get("output_tokens")),
         "cache_read": _n(u.get("cache_read_input_tokens")),
         "cache_write_5m": 0, "cache_write_1h": 0}
    c = u.get("cache_creation")
    if isinstance(c, dict):
        t["cache_write_5m"] = _n(c.get("ephemeral_5m_input_tokens"))
        t["cache_write_1h"] = _n(c.get("ephemeral_1h_input_tokens"))
    if t["cache_write_5m"] == 0 and t["cache_write_1h"] == 0:
        t["cache_write_5m"] = _n(u.get("cache_creation_input_tokens"))
    return t


def signature(name, tool_input):
    try:
        blob = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(tool_input)
    return "%s:%s" % (name, hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12])


# ------------------------------------------------------------- transcript

def read_transcript(path, cfg):
    """Session transcript (JSONL) -> spend and the ordered tool calls.
    Only numbers, tool names, input hashes and shell commands are kept."""
    out = {"cost": 0.0, "unknown_models": 0, "tools": []}
    if not path or not os.path.exists(path):
        return out
    seen, by_id = set(), {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for seq, raw in enumerate(fh):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tid = b.get("id") or "seq%d" % seq
                    if tid in by_id:
                        continue
                    name = b.get("name") or "(tool)"
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    ev = {"id": tid, "name": name, "sig": signature(name, b.get("input")),
                          "cmd": inp.get("command") if name == "Bash" else None,
                          "error": None}
                    by_id[tid] = ev
                    out["tools"].append(ev)
                elif b.get("type") == "tool_result":
                    ev = by_id.get(b.get("tool_use_id"))
                    if ev is not None:
                        ev["error"] = bool(b.get("is_error"))
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            key = msg.get("id") or obj.get("uuid") or "line%d" % seq
            if obj.get("requestId"):
                key = "%s|%s" % (key, obj["requestId"])
            if key in seen:  # streamed messages repeat the same usage
                continue
            seen.add(key)
            usd, known = cost_of(parse_usage(usage), msg.get("model"), cfg)
            out["cost"] += usd
            if not known:
                out["unknown_models"] += 1
    return out


# ------------------------------------------------------------------ state

def _h(s):
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:16]


def allow_file(session):
    return os.path.join(state_dir(), "allow", _h(session))


def is_allowed(session):
    return bool(session) and os.path.exists(allow_file(session))


def share_file():
    return os.path.join(state_dir(), "share.json")


def sharing_on():
    try:
        with open(share_file()) as f:
            return bool(json.load(f).get("enabled"))
    except (OSError, ValueError):
        return False


def record_pattern(kind, tool, count, spent, model_family):
    """Opt-in only. Writes an anonymous line to a LOCAL file. There is no
    upload: no server exists yet, and this function never opens a socket."""
    if not sharing_on():
        return False
    bucket = "<5" if spent < 5 else "5-15" if spent < 15 else "15-50" if spent < 50 else "50+"
    line = {"v": 1, "kind": kind, "tool": tool if not tool.startswith("mcp__") else "mcp",
            "count": count, "spend_bucket_usd": bucket, "model_family": model_family,
            "day": time.strftime("%Y-%m-%d", time.gmtime())}
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), "shared-patterns.jsonl"), "a") as f:
        f.write(json.dumps(line) + "\n")
    return True


def _family(model):
    m = (model or "").lower()
    for k in ("opus", "sonnet", "haiku", "gpt"):
        if k in m:
            return k
    return "other"


# --------------------------------------------------------------- decision

def is_wait(cmd):
    c = (cmd or "").strip().lower()
    return any(c.startswith(w) or (" " + w) in c for w in WAIT_WORDS)


def same_run(tools, sig):
    n = 1  # the call about to run
    for ev in reversed(tools):
        if ev["sig"] != sig:
            break
        n += 1
    return n


def fail_run(tools, cmd):
    n = 0
    for ev in reversed(tools):
        if ev["name"] != "Bash" or ev["cmd"] != cmd or ev["error"] is not True:
            break
        n += 1
    return n


def pending_removed(tools, tool_use_id, sig):
    """Claude Code writes the assistant message, including the tool call that
    is about to run, to the transcript before PreToolUse fires. Drop that
    pending call so it is not counted twice (it is added back as +1)."""
    if tool_use_id:
        return [t for t in tools if t.get("id") != tool_use_id]
    if tools and tools[-1]["sig"] == sig and tools[-1]["error"] is None:
        return tools[:-1]
    return tools


def _self_allow(cmd):
    c = (cmd or "").lower()
    return "fireborn_guard" in c and " allow" in c


def _allow_hint(session):
    return 'python3 "%s" allow %s' % (os.path.abspath(__file__), session)


def decide(event, cfg, limits):
    """One PreToolUse event -> (kind, reason, pattern). kind is allow/warn/deny."""
    session = event.get("session_id") or ""
    tr = read_transcript(event.get("transcript_path"), cfg)
    spent = tr["cost"]
    name = event.get("tool_name") or "(tool)"
    tinput = event.get("tool_input")
    cmd = tinput.get("command") if isinstance(tinput, dict) and name == "Bash" else None
    sig = signature(name, tinput)
    tools = pending_removed(tr["tools"], event.get("tool_use_id"), sig)
    warns = []

    if cmd and _self_allow(cmd):
        return "deny", ("Fireborn Guard: only the user can lift a stop. Ask them to run "
                        "the allow command in their own terminal."), None

    if not is_allowed(session):
        if limits["hard"] and spent >= limits["hard"]:
            return "deny", ("Fireborn Guard: this session has used about $%.2f at API list price "
                            "(hard line $%.2f). Stopped. To keep going, the user runs: %s"
                            % (spent, limits["hard"], _allow_hint(session))), ("hard_cap", name, 0)
        if not (cmd and is_wait(cmd)):
            run = same_run(tools, sig)
            if limits["same_stop"] and run >= limits["same_stop"]:
                return "deny", ("Fireborn Guard: the same %s call with the same input %d times in a "
                                "row. Looks like a loop. Stopped. To override, the user runs: %s"
                                % (name, run, _allow_hint(session))), ("same_call", name, run)
            if limits["same_warn"] and run >= limits["same_warn"]:
                warns.append("the same %s call with the same input %d times in a row; "
                             "try something different or ask the user" % (name, run))
        if cmd and not is_wait(cmd):
            fails = fail_run(tools, cmd)
            if limits["fail_stop"] and fails >= limits["fail_stop"]:
                return "deny", ("Fireborn Guard: this command already failed %d times in a row. "
                                "Stopped. Fix the cause first. To override, the user runs: %s"
                                % (fails, _allow_hint(session))), ("same_failure", name, fails)
            if limits["fail_warn"] and fails >= limits["fail_warn"]:
                warns.append("this command already failed %d times in a row" % fails)
    if limits["soft"] and spent >= limits["soft"]:
        warns.append("this session has used about $%.2f at API list price (soft line $%.2f); "
                     "check with the user before spending much more" % (spent, limits["soft"]))
    if warns:
        return "warn", "Fireborn Guard: " + "; ".join(warns) + ".", None
    return "allow", "", None


def hook_output(kind, reason):
    if kind == "deny":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason},
                "systemMessage": reason}
    if kind == "warn":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": reason},
                "systemMessage": reason}
    return None


# --------------------------------------------------------------- commands

def cmd_hook(stdin=None, stdout=None, prices=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        event = json.load(stdin)
        cfg = load_prices(prices)
        kind, reason, pattern = decide(event, cfg, limits_from_env())
    except Exception:  # never break the user's session because of our bug
        return 0
    if pattern:
        try:
            spent = read_transcript(event.get("transcript_path"), cfg)["cost"]
            record_pattern(pattern[0], pattern[1], pattern[2], spent,
                           _family(event.get("model") or os.environ.get("ANTHROPIC_MODEL")))
        except OSError:
            pass
    out = hook_output(kind, reason)
    if out:
        stdout.write(json.dumps(out))
    return 0


def cmd_allow(session):
    p = allow_file(session)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(str(int(time.time())))
    print("Session %s may now continue past the hard line and loop stops." % session)
    return 0


def cmd_share(value):
    os.makedirs(state_dir(), exist_ok=True)
    if value in ("on", "off"):
        with open(share_file(), "w") as f:
            json.dump({"enabled": value == "on"}, f)
    on = sharing_on()
    print("Anonymous burn-pattern sharing: %s" % ("ON" if on else "OFF"))
    print("Patterns are written only to %s. Nothing is uploaded; there is no server yet."
          % os.path.join(state_dir(), "shared-patterns.jsonl"))
    return 0


def cmd_status():
    lim = limits_from_env()
    print("Fireborn Guard %s" % VERSION)
    print("state dir: %s" % state_dir())
    print("soft line $%.2f, hard line $%.2f" % (lim["soft"], lim["hard"]))
    print("same call: warn %d, stop %d; failing command: warn %d, stop %d"
          % (lim["same_warn"], lim["same_stop"], lim["fail_warn"], lim["fail_stop"]))
    print("sharing: %s (local file only)" % ("ON" if sharing_on() else "OFF"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fireborn_guard", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("hook")
    a = sub.add_parser("allow")
    a.add_argument("session")
    sub.add_parser("status")
    s = sub.add_parser("share")
    s.add_argument("value", nargs="?", choices=["on", "off", "status"], default="status")
    opts = ap.parse_args(argv)
    if opts.cmd == "hook":
        return cmd_hook()
    if opts.cmd == "allow":
        return cmd_allow(opts.session)
    if opts.cmd == "share":
        return cmd_share(opts.value)
    if opts.cmd == "status":
        return cmd_status()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
