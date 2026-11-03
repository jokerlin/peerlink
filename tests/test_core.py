import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peerlink import catalog, config, schema  # noqa: E402
from peerlink.engine import Engine  # noqa: E402
from peerlink.identity import Peer, principal_for  # noqa: E402
from peerlink.server import Api  # noqa: E402


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(textwrap.dedent(text))
    if mode:
        os.chmod(path, mode)


class Env(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="peerlink-test-")
        self.tasks = os.path.join(self.root, "tasks")
        os.makedirs(self.tasks)
        self.rings = []
        self.cfg = config.load(path=os.path.join(self.root, "none.toml"), overrides={
            "home": os.path.join(self.root, "home"),
            "tasks_dir": self.tasks,
            "work_dir": os.path.join(self.root, "work"),
            "node": "testnode",
            "listen": [],
            "principals": {
                "owner": {"logins": ["owner@example.com"]},
                "grok/bot": {"tags": ["tag:agent"], "daily_submits": 20,
                             "doorbell": os.path.join(self.root, "bell.env")},
            },
        })
        write(os.path.join(self.root, "bell.env"), "X=1\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def task(self, name, toml, files=None):
        d = os.path.join(self.tasks, name)
        write(os.path.join(d, "task.toml"), toml)
        for fn, (text, mode) in (files or {}).items():
            write(os.path.join(d, fn), text, mode)
        return d

    def engine(self, clock=None):
        def ringer(env, payload, extra_env=None):  # same shape as mail.ring_webhook
            self.rings.append(payload)
            return 200
        e = Engine(self.cfg, ringer=ringer, clock=clock or time.time)
        return e

    def run_until_done(self, e, rid, limit=20):
        end = time.time() + limit
        while time.time() < end:
            e.tick()
            r = e.db.run(rid)
            if r["state"] not in ("queued", "running"):
                return r
            time.sleep(0.1)
        self.fail(f"run {rid} still {e.db.run(rid)['state']}")


ECHO = '''\
    description = "echo"
    kind = "script"
    command = ["sh", "run.sh"]
    timeout = "20s"
    [params.word]
    type = "string"
    max_length = 20
    default = "hi"
'''
ECHO_SH = ('echo "word=$PEERLINK_PARAM_WORD"\n'
           'printf "said %s" "$PEERLINK_PARAM_WORD" > "$PEERLINK_RUN_DIR/result.md"\n', 0o755)


class SchemaTest(unittest.TestCase):
    specs = {
        "n": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
        "s": {"type": "string", "max_length": 3},
        "e": {"enum": ["a", "b"], "default": "a"},
        "b": {"type": "boolean", "default": False},
    }

    def test_defaults_and_coercion(self):
        clean, errs = schema.validate({"s": "ok", "n": "4", "b": "true"}, self.specs)
        self.assertEqual(errs, [])
        self.assertEqual(clean, {"n": 4, "s": "ok", "e": "a", "b": True})

    def test_errors(self):
        _, errs = schema.validate({"n": 9, "s": "toolong", "e": "c", "x": 1}, self.specs)
        self.assertEqual(len(errs), 4)
        _, errs = schema.validate({}, self.specs)
        self.assertEqual(errs, ["missing param s"])

    def test_spec_check(self):
        self.assertTrue(schema.check_spec("q", {"type": "string"}))  # free text needs max_length
        self.assertFalse(schema.check_spec("q", {"type": "string", "max_length": 10}))
        self.assertTrue(schema.check_spec("q", {"type": "integer", "default": "x"}))


class LintTest(Env):
    def test_good_script(self):
        d = self.task("echo", ECHO, {"run.sh": ECHO_SH})
        errs, warns = catalog.lint_dir(d)
        self.assertEqual(errs, [])

    def test_bad(self):
        d = self.task("bad", '''
            kind = "pi"
            timeout = "9h"
            sandbox = "x"
            [params.req]
            type = "string"
        ''')
        errs, _ = catalog.lint_dir(d)
        joined = " | ".join(errs)
        self.assertIn("unknown keys", joined)
        self.assertIn("timeout", joined)
        self.assertIn("runbook", joined)
        self.assertIn("max_length", joined)

    def test_missing_script(self):
        d = self.task("nofile", 'kind = "script"\ncommand = ["sh", "missing.sh"]\n')
        errs, _ = catalog.lint_dir(d)
        self.assertTrue(any("not found" in e for e in errs))

    def test_catalog_skips_broken_and_hot_reloads(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        self.task("bad", 'kind = "nope"\n')
        c = catalog.Catalog(self.tasks)
        c.refresh(force=True)
        self.assertIn("echo", c.tasks)
        self.assertIn("bad", c.errors)
        time.sleep(0.01)
        self.task("bad", 'description="x"\nkind = "script"\ncommand=["sh","run.sh"]\n',
                  {"run.sh": ("true\n", 0o755)})
        self.assertIsNotNone(c.get("bad"))


class EngineTest(Env):
    def test_script_run_and_result_letter(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        e = self.engine()
        st, res = e.submit("grok/bot", "echo", {"word": "yo"})
        self.assertEqual(st, 202)
        r = self.run_until_done(e, res["run"])
        self.assertEqual(r["state"], "succeeded")
        self.assertEqual(r["result"]["summary"], "said yo")
        req = json.load(open(os.path.join(r["run_dir"], "request.json")))
        self.assertEqual(req["params"], {"word": "yo"})
        letters = e.mail.pending("grok/bot")
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0]["kind"], "run.result")
        self.assertEqual(letters[0]["body"]["summary"], "said yo")
        self.assertEqual(e.mail.ack("grok/bot", [letters[0]["id"]]), [letters[0]["id"]])
        self.assertEqual(e.mail.pending("grok/bot"), [])
        self.assertEqual(e.mail.ack("grok/bot", [letters[0]["id"]]), [])  # idempotent

    def test_owner_runs_send_no_letters(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        e = self.engine()
        _, res = e.submit("owner", "echo")
        self.run_until_done(e, res["run"])
        self.assertEqual(e.mail.pending("owner"), [])

    def test_failure(self):
        self.task("boom", 'description="b"\nkind="script"\ncommand=["sh","b.sh"]\n',
                  {"b.sh": ("echo bad >&2; exit 3\n", 0o755)})
        e = self.engine()
        _, res = e.submit("grok/bot", "boom")
        r = self.run_until_done(e, res["run"])
        self.assertEqual(r["state"], "failed")
        self.assertEqual(r["exit_code"], 3)
        self.assertIn("bad", r["error"])

    def test_invalid_params_and_unknown_task(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        e = self.engine()
        st, res = e.submit("grok/bot", "echo", {"word": "x" * 50})
        self.assertEqual(st, 400)
        st, _ = e.submit("grok/bot", "nope")
        self.assertEqual(st, 404)

    def test_allow_and_disabled(self):
        self.task("secret", 'allow = ["owner"]\n' + ECHO, {"run.sh": ECHO_SH})
        self.task("off", "enabled = false\n" + ECHO, {"run.sh": ECHO_SH})
        e = self.engine()
        self.assertEqual(e.submit("grok/bot", "secret")[0], 404)
        self.assertEqual(e.submit("owner", "secret")[0], 202)
        self.assertEqual(e.submit("owner", "off")[0], 404)
        names = [t.name for t in e.catalog.visible("grok/bot")]
        self.assertEqual(names, [])

    def test_per_day_dedupe_and_retry(self):
        self.task("daily", ECHO.replace('timeout = "20s"', 'timeout = "20s"\ndedupe = "per-day"'),
                  {"run.sh": ("exit 1\n", 0o755)})
        e = self.engine()
        st1, r1 = e.submit("grok/bot", "daily")
        st2, r2 = e.submit("grok/bot", "daily")
        self.assertEqual((st1, st2), (202, 200))
        self.assertEqual(r1["run"], r2["run"])
        self.assertTrue(r2["deduped"])
        self.run_until_done(e, r1["run"])
        st3, r3 = e.submit("grok/bot", "daily", retry=True)
        self.assertEqual(st3, 202)
        self.assertNotEqual(r3["run"], r1["run"])

    def test_daily_cap(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        self.cfg["principals"]["grok/bot"]["daily_submits"] = 2
        e = self.engine()
        self.assertEqual(e.submit("grok/bot", "echo")[0], 202)
        self.assertEqual(e.submit("grok/bot", "echo")[0], 202)
        self.assertEqual(e.submit("grok/bot", "echo")[0], 429)
        self.assertEqual(e.submit("owner", "echo")[0], 202)

    def test_locks_serialize(self):
        toml = 'description="s"\nkind="script"\ncommand=["sh","s.sh"]\nlocks=["app:x"]\n'
        self.task("a", toml, {"s.sh": ("sleep 0.5\n", 0o755)})
        self.task("b", toml, {"s.sh": ("sleep 0.5\n", 0o755)})
        e = self.engine()
        _, ra = e.submit("owner", "a")
        _, rb = e.submit("owner", "b")
        e.tick()
        states = {e.db.run(ra["run"])["state"], e.db.run(rb["run"])["state"]}
        self.assertEqual(states, {"running", "queued"})
        a = self.run_until_done(e, ra["run"])
        b = self.run_until_done(e, rb["run"])
        self.assertGreaterEqual(b["started"], a["finished"] - 0.01)

    def test_concurrency_limit(self):
        self.cfg["max_concurrent"] = 1
        self.task("s", 'description="s"\nkind="script"\ncommand=["sh","s.sh"]\n',
                  {"s.sh": ("sleep 0.3\n", 0o755)})
        e = self.engine()
        ids = [e.submit("owner", "s")[1]["run"] for _ in range(2)]
        e.tick()
        self.assertEqual([e.db.run(i)["state"] for i in ids], ["running", "queued"])
        for i in ids:
            self.run_until_done(e, i)

    def test_timeout_kills_group(self):
        self.task("slow", 'description="s"\nkind="script"\ncommand=["sh","s.sh"]\ntimeout="1s"\n',
                  {"s.sh": ("sleep 30 & sleep 30\n", 0o755)})
        e = self.engine()
        _, r = e.submit("grok/bot", "slow")
        r = self.run_until_done(e, r["run"], limit=15)
        self.assertEqual(r["state"], "timed_out")
        self.assertEqual(e.mail.pending("grok/bot")[0]["body"]["state"], "timed_out")

    def test_cancel_running_and_queued(self):
        self.task("slow", 'description="s"\nkind="script"\ncommand=["sh","s.sh"]\n',
                  {"s.sh": ("sleep 30\n", 0o755)})
        e = self.engine()
        _, r = e.submit("grok/bot", "slow")
        e.tick()
        self.assertEqual(e.cancel("owner", r["run"])[0], 202)
        r = self.run_until_done(e, r["run"])
        self.assertEqual(r["state"], "cancelled")
        _, q = e.submit("grok/bot", "slow")
        self.assertEqual(e.cancel("grok/bot", q["run"])[0], 202)
        self.assertEqual(e.db.run(q["run"])["state"], "cancelled")

    def test_stop_all_pauses(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        e = self.engine()
        _, r = e.submit("grok/bot", "echo")
        e.stop_all()
        self.assertEqual(e.db.run(r["run"])["state"], "cancelled")
        self.assertEqual(e.submit("grok/bot", "echo")[0], 503)
        e.resume()
        self.assertEqual(e.submit("grok/bot", "echo")[0], 202)

    def test_reconcile_after_restart(self):
        self.task("s", 'description="s"\nkind="script"\ncommand=["sh","s.sh"]\n',
                  {"s.sh": ("sleep 0.5; echo done > \"$PEERLINK_RUN_DIR/result.md\"\n", 0o755)})
        e = self.engine()
        _, r = e.submit("grok/bot", "s")
        e.tick()
        self.assertEqual(e.db.run(r["run"])["state"], "running")
        # a new engine on the same db, as after a restart, while the run continues
        e2 = self.engine()
        e2.reconcile()
        self.assertEqual(e2.db.run(r["run"])["state"], "running")
        time.sleep(1.0)
        e.procs.clear()
        e2.reconcile()
        run = e2.db.run(r["run"])
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(run["result"]["summary"].strip(), "done")

    def test_reused_pid_is_not_ours(self):
        import subprocess as sp
        self.task("s", 'description="s"\nkind="script"\ncommand=["sh","s.sh"]\n',
                  {"s.sh": ("sleep 30\n", 0o755)})
        e = self.engine()
        _, r = e.submit("grok/bot", "s")
        e.tick()
        run = e.db.run(r["run"])
        stranger = sp.Popen(["sleep", "30"], start_new_session=True)
        try:
            e._kill(run)                       # stop the real run
            e.procs.clear()
            e.db.update_run(run["id"], pid=stranger.pid)
            e2 = self.engine()
            e2.reconcile()                     # stranger's pid must not count as our run
            self.assertEqual(e2.db.run(run["id"])["state"], "failed")
            run = e2.db.run(run["id"])
            e2._kill({**run, "state": "running"})
            self.assertIsNone(stranger.poll())  # and we never signal it
        finally:
            stranger.kill()
            stranger.wait()

    def test_other_callers_cannot_see_runs(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        self.cfg["principals"]["other"] = {"tags": ["tag:other"]}
        e = self.engine()
        _, r = e.submit("grok/bot", "echo")
        self.assertEqual(e.get("other", r["run"])[0], 404)
        self.assertEqual(e.get("owner", r["run"])[0], 200)
        self.assertEqual(e.cancel("other", r["run"])[0], 404)


class DoorbellTest(Env):
    def test_merge_window_and_retry(self):
        now = [1000.0]
        e = self.engine(clock=lambda: now[0])
        bell = e.doorbell
        e.mail.post("grok/bot", "note", {"n": 1})
        bell.tick(now[0])
        self.assertEqual(len(self.rings), 1)          # first letter rings at once
        now[0] += 60
        e.mail.post("grok/bot", "note", {"n": 2})
        bell.tick(now[0])
        self.assertEqual(len(self.rings), 1)          # within 10 min: wait
        now[0] += 600
        bell.tick(now[0])
        self.assertEqual(len(self.rings), 2)          # merged ring
        self.assertEqual(self.rings[-1]["pending"], 2)
        now[0] += 3599
        bell.tick(now[0])
        self.assertEqual(len(self.rings), 2)
        now[0] += 2
        bell.tick(now[0])
        self.assertEqual(len(self.rings), 3)          # 1h retry
        ids = [l["id"] for l in e.mail.pending("grok/bot")]
        e.mail.ack("grok/bot", ids)
        now[0] += 999999
        bell.tick(now[0])
        self.assertEqual(len(self.rings), 3)          # nothing pending: quiet
        self.assertEqual(self.rings[0], {"from": "testnode", "pending": 1})

    def test_missing_env_file_does_not_ring(self):
        os.remove(os.path.join(self.root, "bell.env"))
        e = self.engine()
        e.mail.post("grok/bot", "note", {})
        e.doorbell.tick()
        self.assertEqual(self.rings, [])

    def test_expiry(self):
        e = self.engine()
        e.mail.post("grok/bot", "note", {})
        self.assertEqual(e.mail.expire(time.time() + 8 * 86400), 1)
        self.assertEqual(e.mail.pending("grok/bot"), [])


class FakeResolver:
    def __init__(self, table):
        self.table = table

    def peer(self, ip):
        return self.table.get(ip, Peer(ip))


class ApiTest(Env):
    def api(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        e = self.engine()
        res = FakeResolver({
            "100.64.0.2": Peer("100.64.0.2", "agent", ["tag:agent"]),
            "100.64.0.3": Peer("100.64.0.3", "laptop", [], "owner@example.com"),
            "127.0.0.1": Peer("127.0.0.1", "localhost", [], "local"),
            "100.64.0.9": Peer("100.64.0.9", "stranger", [], "stranger@example.com"),
        })
        return Api(e, res), e

    def test_identity_mapping(self):
        pr = self.cfg["principals"]
        self.assertEqual(principal_for(Peer("x", tags=["tag:agent"]), pr), "grok/bot")
        self.assertEqual(principal_for(Peer("x", login="local"), pr), "owner")
        self.assertIsNone(principal_for(Peer("x", login="a@b"), pr))

    def test_flow(self):
        api, e = self.api()
        g = "100.64.0.2"
        st, info = api.handle("GET", "/info", b"", g)
        self.assertEqual((st, info["you"]), (200, "grok/bot"))
        st, tasks = api.handle("GET", "/tasks", b"", g)
        self.assertEqual([t["name"] for t in tasks["tasks"]], ["echo"])
        body = json.dumps({"task": "echo", "params": {"word": "w"}, "caller": "owner"}).encode()
        st, run = api.handle("POST", "/runs", body, g)
        self.assertEqual(st, 202)
        self.assertEqual(run["caller"], "grok/bot")    # self-claimed caller is ignored
        self.run_until_done(e, run["run"])
        st, mail = api.handle("GET", "/mail", b"", g)
        self.assertEqual(len(mail["letters"]), 1)
        self.assertEqual(api.handle("GET", "/mail", b"", "127.0.0.1")[1]["letters"], [])
        ids = [l["id"] for l in mail["letters"]]
        st, ack = api.handle("POST", "/mail/ack", json.dumps({"ids": ids}).encode(), g)
        self.assertEqual(ack["acked"], ids)
        st, runs = api.handle("GET", "/runs?state=succeeded", b"", g)
        self.assertEqual(len(runs["runs"]), 1)

    def test_strangers_and_admin(self):
        api, e = self.api()
        self.assertEqual(api.handle("GET", "/info", b"", "100.64.0.9")[0], 403)
        self.assertEqual(api.handle("POST", "/admin/stop", b"{}", "100.64.0.2")[0], 404)
        self.assertEqual(api.handle("POST", "/admin/stop", b"{}", "100.64.0.3")[0], 200)
        self.assertTrue(e.paused())

    def test_bundle(self):
        import io, subprocess, tarfile
        api, e = self.api()
        self.assertEqual(api.handle("GET", "/bundle.tar.gz", b"", "100.64.0.9")[0], 403)
        st, data = api.handle("GET", "/bundle.tar.gz", b"", "100.64.0.2")
        self.assertEqual(st, 200)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            self.assertIn("bin/peerlink.pyz", t.getnames())
            t.extractall(self.root, filter="data")
        out = subprocess.run([sys.executable, os.path.join(self.root, "bin/peerlink.pyz"), "--help"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("sync", out.stdout)
        with open(os.path.join(self.root, "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        self.assertNotIn("{{", skill)
        self.assertIn("http://127.0.0.1:8091", skill)  # no tailnet address configured in tests
        self.assertIn("testnode", skill)

    def test_inbound_letters_dedupe(self):
        api, e = self.api()
        letter = json.dumps({"id": "g_1", "kind": "note", "body": {"t": "hello"}}).encode()
        self.assertEqual(api.handle("POST", "/msg", letter, "100.64.0.2"), (202, {"stored": True}))
        self.assertEqual(api.handle("POST", "/msg", letter, "100.64.0.2"), (202, {"stored": False}))
        inbox = e.mail.inbox()
        self.assertEqual(inbox[0]["from"], "grok/bot")
        bad = json.dumps({"id": "g_2", "kind": "evil"}).encode()
        self.assertEqual(api.handle("POST", "/msg", bad, "100.64.0.2")[0], 400)




class ReviewRegressionTest(Env):
    """Regression tests for bugs found in review."""

    def api(self):
        res = FakeResolver({
            "100.64.0.2": Peer("100.64.0.2", "agent", ["tag:agent"]),
            "127.0.0.1": Peer("127.0.0.1", "localhost", [], "local"),
            "100.64.0.9": Peer("100.64.0.9", "stranger", [], "stranger@example.com"),
        })
        e = self.engine()
        return Api(e, res), e

    def test_dedupe_does_not_leak_other_callers_run(self):
        self.task("daily", ECHO.replace('timeout = "20s"', 'timeout = "20s"\ndedupe = "per-day"'),
                  {"run.sh": ECHO_SH})
        e = self.engine()
        _, mine = e.submit("owner", "daily", {"word": "secret"})
        st, other = e.submit("grok/bot", "daily")
        self.assertEqual(st, 200)
        self.assertEqual(other["run"], mine["run"])
        self.assertNotIn("params", other)
        self.assertNotIn("run_dir", other)

    def test_cancel_needs_input_and_stop_all(self):
        e = self.engine()
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        _, r = e.submit("grok/bot", "echo")
        e.db.update_run(r["run"], state="needs_input")
        self.assertEqual(e.cancel("grok/bot", r["run"])[0], 202)
        self.assertEqual(e.db.run(r["run"])["state"], "cancelled")
        _, r2 = e.submit("grok/bot", "echo")
        e.db.update_run(r2["run"], state="needs_input")
        e.stop_all()
        self.assertEqual(e.db.run(r2["run"])["state"], "cancelled")

    def test_malformed_task_does_not_break_catalog(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        self.task("typo", 'description="t"\nkind="script"\ncommand=["sh","run.sh"]\nreport="finished"\n',
                  {"run.sh": ("true\n", 0o755)})
        self.task("typo2", 'description="t"\nkind="script"\ncommand=["sh","run.sh"]\n'
                  '[params.x]\ntype=["a"]\npattern=5\n', {"run.sh": ("true\n", 0o755)})
        e = self.engine()
        self.assertIsNotNone(e.catalog.get("echo"))
        self.assertIn("typo", e.catalog.errors)
        self.assertIn("typo2", e.catalog.errors)
        errs, _ = catalog.lint_dir(os.path.join(self.tasks, "typo"))
        self.assertTrue(errs)

    def test_schema_fullmatch_and_finite(self):
        specs = {"s": {"type": "string", "max_length": 9, "pattern": "[a-z]+"},
                 "n": {"type": "number", "minimum": 0, "maximum": 1}}
        self.assertTrue(schema.validate({"s": "abc\n", "n": 0.5}, specs)[1])
        self.assertTrue(schema.validate({"s": "abc", "n": "nan"}, specs)[1])
        self.assertFalse(schema.validate({"s": "abc", "n": 0.5}, specs)[1])

    def test_api_rejects_bad_input_without_500(self):
        self.task("echo", ECHO, {"run.sh": ECHO_SH})
        api, e = self.api()
        g = "100.64.0.2"
        self.assertEqual(api.handle("GET", "/mail", b"", "100.64.0.9")[0], 403)
        _, r = api.handle("POST", "/runs", b'{"task":"echo"}', g)
        cases = [
            ("POST", f"/runs/{r['run']}/messages", b"[1]"),
            ("POST", "/mail/ack", b'{"ids":[[1]]}'),
            ("POST", "/runs", b'{"task":"echo","params":{"word":"\\ud800"}}'),
            ("POST", "/runs", b'{"task":"echo","dedupe_key":{"a":1}}'),
            ("POST", "/msg", b'{"id":"x","kind":"note","body":"\\ud800"}'),
            ("POST", "/runs", b"[" * 100000),
        ]
        for method, path, body in cases:
            st, _ = api.handle(method, path, body, g)
            self.assertEqual(st, 400, (path, body[:40]))


class Examples(unittest.TestCase):
    def test_example_tasks_lint_clean(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "tasks")
        names = sorted(os.listdir(root))
        self.assertIn("hello", names)
        for n in names:
            if os.path.isfile(os.path.join(root, n, "task.toml")):
                errs, _ = catalog.lint_dir(os.path.join(root, n))
                self.assertEqual(errs, [], n)


class VersionTest(unittest.TestCase):
    def test_version_flag(self):
        import contextlib
        import io

        import peerlink
        from peerlink import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"peerlink {peerlink.__version__}")
        self.assertRegex(peerlink.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
