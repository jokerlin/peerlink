"""Published files, owner approval, leftover-process reaping and the browser guard."""
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peerlink import catalog, executor  # noqa: E402
from peerlink.identity import Peer  # noqa: E402
from peerlink.server import Api, Server, make_handler  # noqa: E402
from test_core import Env, FakeResolver  # noqa: E402

SHOTS = '''\
    description = "publishes files"
    kind = "script"
    command = ["sh", "run.sh"]
    timeout = "20s"
'''
SHOTS_SH = r'''
o="$PEERLINK_RUN_DIR/out"; mkdir -p "$o"
printf 'PNGDATA' > "$o/shot.png"
printf 'log line' > "$o/notes.txt"
printf 'x' > "$o/noext"
printf 'x' > "$o/bad name.txt"
ln -s /etc/hosts "$o/link.png"
mkdir "$o/sub.png"
echo done > "$PEERLINK_RUN_DIR/result.md"
'''

GATED = '''\
    description = "gated"
    kind = "script"
    command = ["sh", "run.sh"]
    timeout = "20s"
    approve = true
    [params.request]
    type = "string"
    max_length = 100
'''
GATED_SH = 'echo "ran $PEERLINK_PARAM_REQUEST" > "$PEERLINK_RUN_DIR/result.md"\n'

LEAVER = '''\
    description = "leaves a detached process behind"
    kind = "script"
    command = ["sh", "run.sh"]
    timeout = "20s"
'''
LEAVER_SH = r'''
# the shapes left behind on 2026-10-13: a platform binary in the run's session, and
# (here) a process that left the session with setsid
nohup sleep 37 >/dev/null 2>&1 &
echo $! > "$PEERLINK_RUN_DIR/child.pid"
python3 -c 'import os, time; os.setsid(); time.sleep(39)' >/dev/null 2>&1 &
echo $! > "$PEERLINK_RUN_DIR/setsid.pid"
sleep 0.5
echo left > "$PEERLINK_RUN_DIR/result.md"
'''

RESOLVER = {
    "100.64.0.2": Peer("100.64.0.2", "agent", ["tag:agent"]),
    "100.64.0.3": Peer("100.64.0.3", "laptop", [], "owner@example.com"),
    "127.0.0.1": Peer("127.0.0.1", "localhost", [], "local"),
}
G = "100.64.0.2"


class FilesTest(Env):
    def setUp(self):
        super().setUp()
        self.task("shots", SHOTS, {"run.sh": (SHOTS_SH, 0o755)})

    def done_run(self, e, who="grok/bot"):
        _, r = e.submit(who, "shots")
        return self.run_until_done(e, r["run"])

    def test_manifest_letter_and_download(self):
        e = self.engine()
        run = self.done_run(e)
        self.assertEqual(run["state"], "succeeded", run["error"])
        files = run["result"]["files"]
        self.assertEqual([f["name"] for f in files], ["notes.txt", "shot.png"])
        self.assertEqual(files[1], {"name": "shot.png", "size": 7,
                                    "sha256": hashlib.sha256(b"PNGDATA").hexdigest()})
        skipped = {s["name"]: s["why"] for s in run["result"]["files_skipped"]}
        self.assertEqual(set(skipped), {"noext", "bad name.txt", "link.png", "sub.png"})
        self.assertIn("not a regular file", skipped["link.png"])
        letter = e.mail.pending("grok/bot")[0]
        self.assertEqual(letter["body"]["files"], files)

        api = Api(e, FakeResolver(RESOLVER))
        st, got = api.handle("GET", f"/runs/{run['id']}/files/shot.png", b"", G)
        self.assertEqual((st, got), (200, (b"PNGDATA", "image/png")))
        self.assertEqual(api.handle("GET", f"/runs/{run['id']}/files/shot.png", b"", "100.64.0.3")[0], 200)
        for name in ("link.png", "noext", "..%2Frequest.json", "request.json"):
            self.assertEqual(api.handle("GET", f"/runs/{run['id']}/files/{name}", b"", G)[0], 404, name)

    def test_other_callers_and_late_changes(self):
        e = self.engine()
        mine = self.done_run(e, who="owner")
        api = Api(e, FakeResolver(RESOLVER))
        # grok cannot read the owner's run files
        self.assertEqual(api.handle("GET", f"/runs/{mine['id']}/files/shot.png", b"", G)[0], 404)
        # a file changed after the manifest was taken is not served
        with open(os.path.join(mine["run_dir"], "out", "shot.png"), "wb") as f:
            f.write(b"SWAPPED")
        self.assertEqual(api.handle("GET", f"/runs/{mine['id']}/files/shot.png", b"",
                                    "127.0.0.1")[0], 404)

    def test_out_symlink_is_ignored(self):
        d = os.path.join(self.root, "rundir")
        os.makedirs(os.path.join(self.root, "elsewhere"))
        with open(os.path.join(self.root, "elsewhere", "secret.txt"), "w") as f:
            f.write("s")
        os.makedirs(d)
        os.symlink(os.path.join(self.root, "elsewhere"), os.path.join(d, "out"))
        self.assertEqual(executor.list_outputs(d), ([], []))

    def test_limits(self):
        d = os.path.join(self.root, "rundir", "out")
        os.makedirs(d)
        for i in range(3):
            with open(os.path.join(d, f"f{i}.txt"), "w") as f:
                f.write("x" * 10)
        old = executor.OUT_MAX_FILE, executor.OUT_MAX_TOTAL
        try:
            executor.OUT_MAX_FILE, executor.OUT_MAX_TOTAL = 10, 25
            files, skipped = executor.list_outputs(os.path.dirname(d))
        finally:
            executor.OUT_MAX_FILE, executor.OUT_MAX_TOTAL = old
        self.assertEqual([f["name"] for f in files], ["f0.txt", "f1.txt"])
        self.assertIn("per-run limit", skipped[0]["why"])


class ApprovalTest(Env):
    def setUp(self):
        super().setUp()
        self.task("gated", GATED, {"run.sh": (GATED_SH, 0o755)})
        self.sent = []
        self.fail_send = None
        self.now = [1_000_000.0]

    def engine(self, clock=None):
        e = super().engine(clock=clock)

        def notifier(env_file, texts, env):
            self.sent.append(texts)
            return self.fail_send
        e.notifier = notifier
        return e

    def code(self):
        return re.search(r"<code>(\d{6})</code>", self.sent[-1][-1]).group(1)

    def test_code_from_telegram_approves(self):
        e = self.engine()
        st, r = e.submit("grok/bot", "gated", {"request": "please"})
        self.assertEqual((st, r["state"]), (202, "awaiting_approval"))
        self.assertEqual(len(self.sent[-1]), 1)
        self.assertIn("<b>Approval code</b>", self.sent[-1][0])
        self.assertIn('"request": "please"', self.sent[-1][0])
        for _ in range(3):
            e.tick()
        self.assertEqual(e.db.run(r["run"])["state"], "awaiting_approval")  # not started
        api = Api(e, FakeResolver(RESOLVER))
        st, res = api.handle("POST", f"/runs/{r['run']}/approve", b'{"code": "000000x"}', G)
        self.assertEqual((st, res["tries_left"]), (403, 4))
        st, res = api.handle("POST", f"/runs/{r['run']}/approve",
                             json.dumps({"code": self.code()}).encode(), G)
        self.assertEqual((st, res["state"]), (202, "queued"))
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["result"]["summary"].strip(), "ran please")
        self.assertIsNone(e.db.get(f"approval:{r['run']}"))
        # a used code does nothing more
        self.assertEqual(e.approve("grok/bot", r["run"], self.code())[0], 409)

    def test_code_is_bound_to_its_run(self):
        e = self.engine()
        _, a = e.submit("grok/bot", "gated", {"request": "a"})
        code_a = self.code()
        _, b = e.submit("grok/bot", "gated", {"request": "b"})
        if code_a != self.code():
            self.assertEqual(e.approve("grok/bot", b["run"], code_a)[0], 403)

    def test_owner_skips_and_owner_can_approve(self):
        e = self.engine()
        st, r = e.submit("owner", "gated", {"request": "mine"})
        self.assertEqual(r["state"], "queued")
        self.assertEqual(self.sent, [])
        _, r = e.submit("grok/bot", "gated", {"request": "x"})
        self.assertEqual(e.approve("owner", r["run"])[0], 202)

    def test_wrong_codes_cancel(self):
        e = self.engine()
        _, r = e.submit("grok/bot", "gated", {"request": "x"})
        for _ in range(5):
            st, res = e.approve("grok/bot", r["run"], "999999" if self.code() != "999999" else "111111")
        self.assertEqual(st, 403)
        run = e.db.run(r["run"])
        self.assertEqual((run["state"], run["error"]), ("cancelled", "too many wrong approval codes"))
        self.assertEqual(e.mail.pending("grok/bot")[-1]["body"]["state"], "cancelled")

    def test_expiry(self):
        e = self.engine(clock=lambda: self.now[0])
        _, r = e.submit("grok/bot", "gated", {"request": "x"})
        self.now[0] += 1801
        e.tick()
        run = e.db.run(r["run"])
        self.assertEqual((run["state"], run["error"]), ("cancelled", "approval expired"))
        self.assertEqual(e.approve("grok/bot", r["run"], self.code())[0], 409)

    def test_send_failure_fails_the_run(self):
        self.fail_send = "telegram send failed (http 401)"
        e = self.engine()
        st, res = e.submit("grok/bot", "gated", {"request": "x"})
        self.assertEqual(st, 503)
        self.assertEqual(e.db.run(res["run"])["state"], "failed")

    def test_cancel_and_stop_all(self):
        e = self.engine()
        _, a = e.submit("grok/bot", "gated", {"request": "a"})
        _, b = e.submit("grok/bot", "gated", {"request": "b"})
        self.assertEqual(e.cancel("grok/bot", a["run"])[0], 202)
        e.stop_all()
        self.assertEqual(e.db.run(b["run"])["state"], "cancelled")

    def test_long_request_is_split(self):
        self.task("gated", GATED.replace("max_length = 100", "max_length = 8000"),
                  {"run.sh": (GATED_SH, 0o755)})
        e = self.engine()
        e.submit("grok/bot", "gated", {"request": "z" * 7500})
        texts = self.sent[-1]
        self.assertEqual(len(texts), 4)  # 3 request parts + the code
        self.assertTrue(all(len(t) < 4096 for t in texts))

    def test_rich_text_escapes_request_and_metadata(self):
        e = self.engine()
        e.cfg["node"] = "mac<&>"
        request = '<b>do not format me</b> & `code`'
        st, r = e.submit("grok/bot", "gated", {"request": request})
        self.assertEqual(st, 202)
        root = ET.fromstring("<message>" + self.sent[-1][0] + "</message>")
        self.assertEqual(json.loads(root.find("pre").text), {"request": request})
        self.assertIn("mac<&>", "".join(root.itertext()))
        self.assertEqual(e.approve("grok/bot", r["run"], self.code())[0], 202)

    def test_emoji_request_chunks_remain_valid_and_lossless(self):
        self.task("gated", GATED.replace("max_length = 100", "max_length = 8000"),
                  {"run.sh": (GATED_SH, 0o755)})
        e = self.engine()
        request = "😀<&>" * 1800
        st, _ = e.submit("grok/bot", "gated", {"request": request})
        self.assertEqual(st, 202)
        chunks = []
        for text in self.sent[-1]:
            root = ET.fromstring("<message>" + text + "</message>")
            visible = "".join(root.itertext())
            self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)
        for text in self.sent[-1][:-1]:
            root = ET.fromstring("<message>" + text + "</message>")
            chunks.append(root.find("pre").text)
        self.assertEqual(json.loads("".join(chunks)), {"request": request})

    def test_lint(self):
        d = self.task("badgate", GATED.replace("approve = true", 'approve = "yes"'),
                      {"run.sh": (GATED_SH, 0o755)})
        errs, _ = catalog.lint_dir(d)
        self.assertIn("approve must be true or false", errs)


class ReapTest(Env):
    def test_detached_child_is_killed_when_run_ends(self):
        self.task("leaver", LEAVER, {"run.sh": (LEAVER_SH, 0o755)})
        e = self.engine()
        _, r = e.submit("grok/bot", "leaver")
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["state"], "succeeded", run["error"])
        reaped = e.db.q("SELECT detail FROM audit WHERE action='run.reap'")
        for fn in ("child.pid", "setsid.pid"):
            with open(os.path.join(run["run_dir"], fn)) as f:
                pid = int(f.read())
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, 9)
                self.fail(f"{fn} survived the run")
            self.assertIn(pid, json.loads(reaped[0]["detail"])["killed"])
        self.assertEqual(executor.leftover_pids(run["id"], run["pid"]), [])


class BrowserGuardTest(Env):
    def setUp(self):
        super().setUp()
        self.task("shots", SHOTS, {"run.sh": (SHOTS_SH, 0o755)})
        self.e = self.engine()
        self.srv = Server(("127.0.0.1", 0), make_handler(Api(self.e, FakeResolver(RESOLVER))))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), data

    def test_guard(self):
        body = json.dumps({"task": "shots"})
        js = {"Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/runs", body, {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.req("POST", "/runs", body, {**js, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/runs", None, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.req("GET", "/info", None, {"Host": "evil.example:8091"})[0], 403)
        for host in ("127.0.0.1:8091", "localhost:8091", "testnode:8091", "testnode.tail1.ts.net",
                     "[::1]:8091", "100.64.0.1:8091"):
            self.assertEqual(self.req("GET", "/info", None, {"Host": host})[0], 200, host)
        self.assertEqual(self.e.db.runs(), [])
        st, _, data = self.req("POST", "/runs", body, js)
        self.assertEqual(st, 202)
        rid = json.loads(data)["run"]
        self.run_until_done(self.e, rid)
        st, ctype, data = self.req("GET", f"/runs/{rid}/files/shot.png")
        self.assertEqual((st, ctype, data), (200, "image/png", b"PNGDATA"))


if __name__ == "__main__":
    unittest.main()
