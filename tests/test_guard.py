import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "fireborn-guard")
SCRIPT = os.path.join(PLUGIN, "scripts", "fireborn_guard.py")
sys.path.insert(0, os.path.dirname(SCRIPT))
import fireborn_guard as fg  # noqa: E402


def usage_line(i, model, inp=0, out=0, cache_read=0):
    return {"uuid": "u%d" % i, "requestId": "r%d" % i,
            "message": {"id": "m%d" % i, "model": model, "role": "assistant",
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": cache_read},
                        "content": []}}


def tool_use(i, name, inp):
    return {"uuid": "t%d" % i, "message": {"id": "tm%d" % i, "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu%d" % i, "name": name, "input": inp}]}}


def tool_result(i, error):
    return {"uuid": "tr%d" % i, "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu%d" % i, "is_error": error}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["FIREBORN_GUARD_HOME"] = os.path.join(self.tmp, "state")
        for k in list(os.environ):
            if k.startswith("FIREBORN_") and k != "FIREBORN_GUARD_HOME":
                del os.environ[k]
        self.cfg = fg.load_prices()
        self.limits = fg.limits_from_env()

    def transcript(self, lines):
        p = os.path.join(self.tmp, "t.jsonl")
        with open(p, "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return p

    def event(self, path, name="Read", inp=None, session="s1"):
        return {"session_id": session, "transcript_path": path, "hook_event_name": "PreToolUse",
                "tool_name": name, "tool_input": inp if inp is not None else {"file_path": "/x"}}


class Pricing(Base):
    def test_known_model(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5-20251101", inp=1_000_000)])
        self.assertAlmostEqual(fg.read_transcript(p, self.cfg)["cost"], 5.0)

    def test_unknown_model_never_free(self):
        p = self.transcript([usage_line(1, "mystery-model-9", inp=1_000_000)])
        tr = fg.read_transcript(p, self.cfg)
        self.assertGreater(tr["cost"], 5.0)
        self.assertEqual(tr["unknown_models"], 1)
        top = fg.ceiling(self.cfg["models"])["input"]
        self.assertAlmostEqual(tr["cost"], top)

    def test_streamed_duplicates_counted_once(self):
        ln = usage_line(1, "claude-opus-4-5", inp=1_000_000)
        p = self.transcript([ln, ln, ln])
        self.assertAlmostEqual(fg.read_transcript(p, self.cfg)["cost"], 5.0)

    def test_missing_transcript(self):
        self.assertEqual(fg.read_transcript("/nope", self.cfg)["cost"], 0.0)

    def test_bad_lines_skipped(self):
        p = os.path.join(self.tmp, "bad.jsonl")
        with open(p, "w") as f:
            f.write("not json\n[1,2]\n\n" + json.dumps(usage_line(1, "claude-opus-4-5", inp=1_000_000)) + "\n")
        self.assertAlmostEqual(fg.read_transcript(p, self.cfg)["cost"], 5.0)


class SpendCap(Base):
    def test_below_soft_allows(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=100_000)])
        self.assertEqual(fg.decide(self.event(p), self.cfg, self.limits)[0], "allow")

    def test_soft_line_warns(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=1_200_000)])  # $6
        kind, reason, _ = fg.decide(self.event(p), self.cfg, self.limits)
        self.assertEqual(kind, "warn")
        self.assertIn("soft line", reason)

    def test_hard_line_denies(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=3_200_000)])  # $16
        kind, reason, pat = fg.decide(self.event(p), self.cfg, self.limits)
        self.assertEqual(kind, "deny")
        self.assertIn("hard line", reason)
        self.assertIn("allow s1", reason)
        self.assertEqual(pat[0], "hard_cap")

    def test_allow_exempts_one_session(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=3_200_000)])
        fg.cmd_allow("s1")
        self.assertEqual(fg.decide(self.event(p, session="s1"), self.cfg, self.limits)[0], "warn")
        self.assertEqual(fg.decide(self.event(p, session="s2"), self.cfg, self.limits)[0], "deny")

    def test_env_overrides(self):
        os.environ["FIREBORN_HARD_USD"] = "1"
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=300_000)])  # $1.5
        self.assertEqual(fg.decide(self.event(p), self.cfg, fg.limits_from_env())[0], "deny")

    def test_agent_cannot_self_allow(self):
        p = self.transcript([])
        ev = self.event(p, "Bash", {"command": "python3 /x/fireborn_guard.py allow s1"})
        self.assertEqual(fg.decide(ev, self.cfg, self.limits)[0], "deny")


class Loops(Base):
    def calls(self, n, name="Read", inp=None, error=False):
        lines = []
        for i in range(n):
            lines.append(tool_use(i, name, inp or {"file_path": "/x"}))
            if error is not None:
                lines.append(tool_result(i, error))
        return lines

    def test_same_call_warn_at_4(self):
        p = self.transcript(self.calls(3))
        self.assertEqual(fg.decide(self.event(p), self.cfg, self.limits)[0], "warn")

    def test_same_call_ok_at_3(self):
        p = self.transcript(self.calls(2))
        self.assertEqual(fg.decide(self.event(p), self.cfg, self.limits)[0], "allow")

    def test_same_call_stop_at_8(self):
        p = self.transcript(self.calls(7))
        kind, _, pat = fg.decide(self.event(p), self.cfg, self.limits)
        self.assertEqual(kind, "deny")
        self.assertEqual(pat, ("same_call", "Read", 8))

    def test_different_input_resets(self):
        lines = self.calls(7)
        lines.append(tool_use(99, "Read", {"file_path": "/other"}))
        p = self.transcript(lines)
        self.assertEqual(fg.decide(self.event(p), self.cfg, self.limits)[0], "allow")

    def test_failing_command_warn_3_stop_5(self):
        cmd = {"command": "npm test"}
        p = self.transcript(self.calls(3, "Bash", cmd, error=True))
        self.assertEqual(fg.decide(self.event(p, "Bash", cmd), self.cfg, self.limits)[0], "warn")
        p = self.transcript(self.calls(5, "Bash", cmd, error=True))
        self.assertEqual(fg.decide(self.event(p, "Bash", cmd), self.cfg, self.limits)[0], "deny")

    def test_success_breaks_failure_run(self):
        cmd = {"command": "npm test"}
        lines = self.calls(4, "Bash", cmd, error=True)
        lines += [tool_use(50, "Bash", cmd), tool_result(50, False)]
        p = self.transcript(lines)
        # 5 identical calls in a row -> same-call warn, but no failure stop
        kind, reason, _ = fg.decide(self.event(p, "Bash", cmd), self.cfg, self.limits)
        self.assertEqual(kind, "warn")
        self.assertNotIn("failed", reason)

    def test_pending_call_not_double_counted_by_id(self):
        # real Claude Code: the call about to run is already in the transcript
        lines = self.calls(7)
        p = self.transcript(lines)
        ev = self.event(p)
        ev["tool_use_id"] = "tu6"
        self.assertEqual(fg.decide(ev, self.cfg, self.limits)[0], "warn")  # 7th call
        lines = self.calls(8)
        p = self.transcript(lines)
        ev = self.event(p)
        ev["tool_use_id"] = "tu7"
        self.assertEqual(fg.decide(ev, self.cfg, self.limits)[0], "deny")  # 8th call

    def test_pending_call_dropped_without_id(self):
        p = self.transcript(self.calls(4, error=None))  # last one looks pending
        self.assertEqual(fg.decide(self.event(p), self.cfg, self.limits)[0], "warn")
        p = self.transcript(self.calls(3, error=None))
        # without id and without results the last one looks pending -> run 3
        self.assertEqual(fg.decide(self.event(p), self.cfg, self.limits)[0], "allow")

    def test_failures_counted_with_pending_call(self):
        cmd = {"command": "npm test"}
        lines = self.calls(5, "Bash", cmd, error=True)
        lines.append(tool_use(77, "Bash", cmd))  # pending, no result yet
        p = self.transcript(lines)
        ev = self.event(p, "Bash", cmd)
        ev["tool_use_id"] = "tu77"
        kind, reason, _ = fg.decide(ev, self.cfg, self.limits)
        self.assertEqual(kind, "deny")
        self.assertIn("failed 5 times", reason)

    def test_sleep_never_counts(self):
        cmd = {"command": "sleep 30"}
        p = self.transcript(self.calls(20, "Bash", cmd, error=True))
        self.assertEqual(fg.decide(self.event(p, "Bash", cmd), self.cfg, self.limits)[0], "allow")


class Sharing(Base):
    def test_default_off_writes_nothing(self):
        self.assertFalse(fg.sharing_on())
        self.assertFalse(fg.record_pattern("hard_cap", "Bash", 0, 20, "opus"))
        self.assertFalse(os.path.exists(os.path.join(fg.state_dir(), "shared-patterns.jsonl")))

    def test_on_writes_local_anonymous_line(self):
        fg.cmd_share("on")
        self.assertTrue(fg.record_pattern("same_call", "mcp__secret__tool", 8, 7.3, "opus"))
        with open(os.path.join(fg.state_dir(), "shared-patterns.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["tool"], "mcp")
        self.assertEqual(rec["spend_bucket_usd"], "5-15")
        self.assertNotIn("session", json.dumps(rec))

    def test_no_network_imports(self):
        src = open(SCRIPT).read()
        for mod in ("socket", "urllib", "http.client", "requests"):
            self.assertNotIn("import %s" % mod, src)


class HookProcess(Base):
    def run_hook(self, event, env_extra=None):
        env = dict(os.environ, **(env_extra or {}))
        r = subprocess.run([sys.executable, SCRIPT, "hook"], input=json.dumps(event),
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        return r.stdout

    def test_allow_prints_nothing(self):
        p = self.transcript([])
        self.assertEqual(self.run_hook(self.event(p)), "")

    def test_deny_json_shape(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=3_200_000)])
        out = json.loads(self.run_hook(self.event(p)))
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["permissionDecision"], "deny")

    def test_warn_json_shape(self):
        p = self.transcript([usage_line(1, "claude-opus-4-5", inp=1_200_000)])
        out = json.loads(self.run_hook(self.event(p)))
        self.assertIn("additionalContext", out["hookSpecificOutput"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_garbage_input_never_blocks(self):
        r = subprocess.run([sys.executable, SCRIPT, "hook"], input="{oops",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_plugin_data_dir_used(self):
        d = os.path.join(self.tmp, "pdata")
        env = {k: v for k, v in os.environ.items() if k != "FIREBORN_GUARD_HOME"}
        env["CLAUDE_PLUGIN_DATA"] = d
        subprocess.run([sys.executable, SCRIPT, "allow", "abc"], env=env, check=True,
                       capture_output=True, timeout=30)
        self.assertTrue(os.path.isdir(os.path.join(d, "allow")))


class Manifests(unittest.TestCase):
    def test_names_match_and_paths_exist(self):
        mk = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
        pj = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))
        entry = mk["plugins"][0]
        self.assertEqual(entry["name"], pj["name"])
        self.assertNotIn("..", entry["source"])
        self.assertTrue(os.path.isdir(os.path.join(ROOT, entry["source"])))
        hooks = json.load(open(os.path.join(PLUGIN, "hooks", "hooks.json")))
        h = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(h["args"][0], "${CLAUDE_PLUGIN_ROOT}/scripts/fireborn_guard.py")


if __name__ == "__main__":
    unittest.main()
