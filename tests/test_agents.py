"""Agent executors against fake pi/codex binaries that mimic their output."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_core import Env, write  # noqa: E402

FAKE_PI = r'''#!/bin/sh
# Record argv and require the persisted session on follow-up invocations.
echo "$@" >> "$PEERLINK_RUN_DIR/argv.log"
[ "$1" = "--print" ] && [ "$2" = "--session" ] || exit 2
session="$3"
prompt=$(cat)
case "$prompt" in
  *"follow-up message"*)
    [ -f "$session" ] || exit 3
    echo "final answer" > "$PEERLINK_RUN_DIR/result.md" ;;
  *request.json*) if grep -q ASKME "$PEERLINK_RUN_DIR/request.json"; then
        echo "which file?" > "$PEERLINK_RUN_DIR/question.txt"; fi ;;
esac
echo session > "$session"
if grep -q FAILME "$PEERLINK_RUN_DIR/request.json"; then
  echo 'provider failed' >&2
  exit 1
fi
echo 'done by pi'
'''

FAKE_CODEX = r'''#!/bin/sh
echo "$@" >> "$PEERLINK_RUN_DIR/argv.log"
cat > /dev/null
last=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && last="$2"; shift; done
echo '{"type":"thread.started","thread_id":"t-42"}'
echo '{"type":"turn.completed"}'
echo "codex says hi" > "$last"
'''

ASK = '''
description = "ask"
kind = "pi"
runbook = "runbook.md"
timeout = "30s"
max_turns = 7
[params.request]
type = "string"
max_length = 100
[params.agent]
enum = ["pi", "codex"]
default = "pi"
'''


class AgentEnv(Env):
    def setUp(self):
        super().setUp()
        bin_dir = os.path.join(self.root, "bin")
        write(os.path.join(bin_dir, "pi"), FAKE_PI, 0o755)
        write(os.path.join(bin_dir, "codex"), FAKE_CODEX, 0o755)
        self.cfg["pi_bin"] = os.path.join(bin_dir, "pi")
        self.cfg["codex_bin"] = os.path.join(bin_dir, "codex")
        self.cfg["env"] = {"HTTPS_PROXY": "http://127.0.0.1:9"}
        self.task("ask", ASK, {"runbook.md": ("do what request says\n", None)})

    def argv(self, run):
        return open(os.path.join(run["run_dir"], "argv.log")).read().splitlines()


class AgentTest(AgentEnv):

    def test_pi_run(self):
        e = self.engine()
        _, r = e.submit("grok/bot", "ask", {"request": "hello"})
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["state"], "succeeded", run["error"])
        self.assertEqual(run["result"]["summary"].strip(), "done by pi")
        self.assertEqual(run["agent"], "pi")
        argv = self.argv(run)[0]
        self.assertIn("--print", argv)
        self.assertNotIn("--max-turns", argv)
        self.assertIn(f"--session {run['session_id']}", argv)
        prompt = open(os.path.join(run["run_dir"], "prompt.1.txt")).read()
        self.assertIn("do what request says", prompt)
        self.assertNotIn("hello", prompt)        # request text stays in request.json

    def test_pi_question_then_resume(self):
        e = self.engine()
        _, r = e.submit("grok/bot", "ask", {"request": "ASKME"})
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["state"], "needs_input")
        self.assertEqual(run["result"]["question"], "which file?")
        letters = e.mail.pending("grok/bot")
        self.assertEqual([l["kind"] for l in letters], ["run.question"])
        self.assertEqual(letters[0]["body"]["question"], "which file?")
        self.assertEqual(e.message("other", run["run"] if "run" in run else run["id"], "x")[0], 404)
        st, _ = e.message("grok/bot", run["id"], "the csv one")
        self.assertEqual(st, 202)
        run = self.run_until_done(e, run["id"])
        self.assertEqual(run["state"], "succeeded")
        self.assertEqual(run["turn"], 2)
        self.assertEqual(run["result"]["summary"].strip(), "final answer")
        self.assertIn(f"--session {run['session_id']}", self.argv(run)[1])
        self.assertEqual(self.argv(run)[0], self.argv(run)[1])
        self.assertEqual(open(os.path.join(run["run_dir"], "message.2.txt")).read(), "the csv one")
        self.assertEqual(e.message("grok/bot", run["id"], "")[0], 400)

    def test_pi_error(self):
        e = self.engine()
        _, r = e.submit("grok/bot", "ask", {"request": "FAILME"})
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["state"], "failed")
        self.assertIn("provider failed", run["error"])

    def test_removed_agent_rejected(self):
        e = self.engine()
        self.assertEqual(e.submit("grok/bot", "ask", {"request": "hi", "agent": "claude"})[0], 400)

    def test_pi_kind_without_agent_param(self):
        self.task("plain", 'kind="pi"\nrunbook="runbook.md"\n',
                  {"runbook.md": ("do the task\n", None)})
        e = self.engine()
        _, r = e.submit("owner", "plain")
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["agent"], "pi")
        self.assertEqual(run["state"], "succeeded", run["error"])

    def test_codex_run_and_resume(self):
        e = self.engine()
        _, r = e.submit("grok/bot", "ask", {"request": "hi", "agent": "codex"})
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["state"], "succeeded", run["error"])
        self.assertEqual(run["agent"], "codex")
        self.assertEqual(run["session_id"], "t-42")
        self.assertEqual(run["result"]["summary"].strip(), "codex says hi")
        self.assertIn("exec --json --skip-git-repo-check -C", self.argv(run)[0])
        st, _ = e.message("grok/bot", run["id"], "more")
        self.assertEqual(st, 202)
        run = self.run_until_done(e, run["id"])
        self.assertEqual(run["state"], "succeeded")
        self.assertTrue(self.argv(run)[1].startswith("exec resume t-42 --json"))

    def test_env_passthrough(self):
        self.task("envt", 'description="e"\nkind="script"\ncommand=["sh","e.sh"]\n',
                  {"e.sh": ('echo "$HTTPS_PROXY" > "$PEERLINK_RUN_DIR/result.md"\n', 0o755)})
        e = self.engine()
        _, r = e.submit("owner", "envt")
        run = self.run_until_done(e, r["run"])
        self.assertEqual(run["result"]["summary"].strip(), "http://127.0.0.1:9")

    def test_script_runs_reject_messages(self):
        self.task("envt", 'description="e"\nkind="script"\ncommand=["sh","e.sh"]\n',
                  {"e.sh": ("true\n", 0o755)})
        e = self.engine()
        _, r = e.submit("owner", "envt")
        run = self.run_until_done(e, r["run"])
        self.assertEqual(e.message("owner", run["id"], "x")[0], 409)

    def test_messages_respect_pause_cap_and_disable(self):
        self.cfg["principals"]["grok/bot"]["daily_submits"] = 2
        e = self.engine()
        _, r = e.submit("grok/bot", "ask", {"request": "hello"})
        self.run_until_done(e, r["run"])
        e.stop_all()
        self.assertEqual(e.message("grok/bot", r["run"], "more")[0], 503)
        e.resume()
        self.assertEqual(e.message("grok/bot", r["run"], "more")[0], 202)   # 2nd use today
        self.run_until_done(e, r["run"])
        self.assertEqual(e.message("grok/bot", r["run"], "again")[0], 429)  # cap reached
        self.assertEqual(e.submit("grok/bot", "ask", {"request": "x"})[0], 429)
        with open(os.path.join(self.tasks, "ask", "task.toml"), "a") as f:
            pass
        self.task("ask", "enabled = false\n" + ASK, {"runbook.md": ("x\n", None)})
        self.cfg["principals"]["grok/bot"]["daily_submits"] = 0
        self.assertEqual(e.message("grok/bot", r["run"], "again")[0], 404)


class GatedFollowupTest(AgentEnv):
    """A follow-up to an approve = true task is a new instruction and needs a new code."""

    def setUp(self):
        super().setUp()
        self.task("ask", "approve = true\n" + ASK, {"runbook.md": ("do what request says\n", None)})
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

    def approved_run(self, e, request="hello"):
        _, r = e.submit("grok/bot", "ask", {"request": request})
        self.assertEqual(e.approve("grok/bot", r["run"], self.code())[0], 202)
        return self.run_until_done(e, r["run"])

    def test_followup_waits_for_its_own_code(self):
        e = self.engine()
        run = self.approved_run(e)
        self.assertEqual(run["state"], "succeeded", run["error"])
        first_code = self.code()
        st, res = e.message("grok/bot", run["id"], "now delete everything")
        self.assertEqual((st, res["state"]), (202, "awaiting_approval"))
        self.assertIn("Follow-up · turn 2", self.sent[-1][0])
        self.assertIn("now delete everything", self.sent[-1][0])
        for _ in range(3):
            e.tick()
        self.assertEqual(e.db.run(run["id"])["state"], "awaiting_approval")  # not started
        self.assertEqual(len(self.argv(run)), 1)
        if first_code != self.code():
            self.assertEqual(e.approve("grok/bot", run["id"], first_code)[0], 403)  # old code is dead
        self.assertEqual(e.approve("grok/bot", run["id"], self.code())[0], 202)
        run = self.run_until_done(e, run["id"])
        self.assertEqual((run["state"], run["turn"]), ("succeeded", 2), run["error"])
        self.assertEqual(run["result"]["summary"].strip(), "final answer")
        self.assertIsNone(e.db.get(f"followup:{run['id']}"))

    def test_expired_followup_is_dropped_not_the_run(self):
        e = self.engine(clock=lambda: self.now[0])
        run = self.approved_run(e)
        e.message("grok/bot", run["id"], "more")
        self.now[0] += 1801
        e.tick()
        after = e.db.run(run["id"])
        self.assertEqual((after["state"], after["turn"]), ("succeeded", 1))
        self.assertEqual(after["result"]["summary"].strip(), "done by pi")
        self.assertFalse(os.path.exists(os.path.join(run["run_dir"], "message.2.txt")))
        self.assertTrue(os.path.exists(os.path.join(run["run_dir"], "message.2.dropped.txt")))
        note = e.mail.pending("grok/bot")[-1]
        self.assertEqual((note["kind"], note["body"]["event"]), ("note", "followup.dropped"))
        self.assertEqual(e.approve("grok/bot", run["id"], self.code())[0], 409)
        self.assertEqual(e.message("grok/bot", run["id"], "again")[0], 202)  # can ask again

    def test_wrong_codes_drop_the_followup(self):
        e = self.engine()
        run = self.approved_run(e)
        e.message("grok/bot", run["id"], "more")
        bad = "999999" if self.code() != "999999" else "111111"
        for _ in range(5):
            st, res = e.approve("grok/bot", run["id"], bad)
        self.assertEqual((st, res["error"]), (403, "wrong code; the follow-up is dropped"))
        after = e.db.run(run["id"])
        self.assertEqual((after["state"], after["turn"]), ("succeeded", 1))

    def test_dropped_answer_restores_the_question(self):
        e = self.engine(clock=lambda: self.now[0])
        run = self.approved_run(e, "ASKME")
        self.assertEqual(run["state"], "needs_input")
        e.message("grok/bot", run["id"], "the csv one")
        self.assertFalse(os.path.exists(os.path.join(run["run_dir"], "question.txt")))
        self.now[0] += 1801
        e.tick()
        after = e.db.run(run["id"])
        self.assertEqual((after["state"], after["turn"]), ("needs_input", 1))
        self.assertTrue(os.path.exists(os.path.join(run["run_dir"], "question.txt")))

    def test_send_failure_drops_the_followup(self):
        e = self.engine()
        run = self.approved_run(e)
        self.fail_send = "telegram send failed (http 401)"
        st, res = e.message("grok/bot", run["id"], "more")
        self.assertEqual(st, 503)
        after = e.db.run(run["id"])
        self.assertEqual((after["state"], after["turn"]), ("succeeded", 1))

    def test_owner_followup_is_not_gated(self):
        e = self.engine()
        _, r = e.submit("owner", "ask", {"request": "mine"})
        run = self.run_until_done(e, r["run"])
        self.assertEqual(self.sent, [])
        st, res = e.message("owner", run["id"], "more")
        self.assertEqual((st, res["state"]), (202, "queued"))
        self.assertEqual(self.sent, [])

    def test_owner_can_approve_a_followup(self):
        e = self.engine()
        run = self.approved_run(e)
        e.message("grok/bot", run["id"], "more")
        self.assertEqual(e.approve("owner", run["id"])[0], 202)
        run = self.run_until_done(e, run["id"])
        self.assertEqual((run["state"], run["turn"]), ("succeeded", 2), run["error"])

    def test_cancel_while_followup_waits(self):
        e = self.engine()
        run = self.approved_run(e)
        e.message("grok/bot", run["id"], "more")
        self.assertEqual(e.cancel("grok/bot", run["id"])[0], 202)
        self.assertEqual(e.db.run(run["id"])["state"], "cancelled")
        self.assertIsNone(e.db.get(f"followup:{run['id']}"))
        self.assertIsNone(e.db.get(f"approval:{run['id']}"))


if __name__ == "__main__":
    unittest.main()
