"""The run engine: submit, queue, start, watch, finish, report."""
import datetime
import hashlib
import hmac
from html import escape
import json
import os
import secrets
import signal
import subprocess
import threading
import time

from . import executor, schema
from .catalog import Catalog
from .config import parse_duration
from .db import ACTIVE, DB, TERMINAL, new_id
from .mail import Doorbell, Mailbox, ring_webhook, send_telegram

FAILED_STATES = {"failed", "timed_out", "blocked", "cancelled"}
APPROVAL_TRIES = 5
TELEGRAM_CHUNK = 3500  # UTF-16 units, leaving room for message headings


def telegram_parts(text):
    """Split before HTML escaping, keeping emoji and markup intact."""
    parts, chunk, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > TELEGRAM_CHUNK:
            parts.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(char)
        size += width
    return parts + ["".join(chunk)]


class Engine:
    def __init__(self, cfg, db=None, ringer=ring_webhook, clock=time.time, notifier=send_telegram):
        self.cfg = cfg
        self.notifier = notifier
        self.clock = clock
        self.db = db or DB(os.path.join(cfg["home"], "peerlink.db"))
        self.catalog = Catalog(cfg["tasks_dir"], parse_duration(cfg["default_timeout"], 1800))
        self.catalog.refresh(force=True)
        self.mail = Mailbox(self.db, cfg["node"], cfg["letter_ttl_days"], clock=clock)
        self.doorbell = Doorbell(self.db, self.mail, cfg["principals"], cfg["node"],
                                 cfg["doorbell_window"], cfg["doorbell_retry"], ringer,
                                 env={k: str(v) for k, v in (cfg.get("env") or {}).items()})
        self.procs = {}       # run id -> Popen (children of this process)
        self.vanished = {}    # run id -> first time we saw the process gone without exit file
        self._lock = threading.RLock()
        self._last_bell = 0
        self._last_expire = 0

    # --- submission ----------------------------------------------------
    def paused(self):
        return bool(self.db.get("paused", False))

    def submit(self, principal, task_name, params=None, dedupe_key=None, retry=False):
        """Returns (http_status, payload)."""
        task = self.catalog.get(task_name)
        if not task or not task.allowed(principal):
            return 404, {"error": f"no task {task_name!r}"}
        if self.paused():
            return 503, {"error": "peerlink is paused (stop --all); try later"}
        clean, errs = schema.validate(params, task.params)
        if errs:
            return 400, {"error": "invalid params", "details": errs}
        key = None
        if dedupe_key:
            key = f"{task.name}:{str(dedupe_key)[:80]}"
        elif task.dedupe == "per-day":
            key = f"{task.name}:{self._today()}"
        with self._lock:
            if key:
                prev = self.db.runs("dedupe_key=?", (key,), order="created DESC", limit=1)
                prev = prev[0] if prev else None
                if prev and not (retry and prev["state"] in FAILED_STATES):
                    self.db.audit(principal, "run.dedupe", run=prev["id"], key=key)
                    if not self._may_see(principal, prev):
                        # someone else's run: say it exists, show nothing of it
                        return 200, {"run": prev["id"], "task": prev["task"], "state": prev["state"],
                                     "deduped": True, "other_caller": True}
                    return 200, {**self.view(prev), "deduped": True}
            if self._over_cap(principal):
                return 429, {"error": "daily submission limit reached"}
            gated = task.approve and principal != "owner"
            rid = new_id("r")
            agent = None
            if task.is_agent:
                agent = clean.get("agent") if "agent" in task.params else task.kind
            run_dir = os.path.join(self.cfg["work_dir"], rid)
            self.db.x(
                "INSERT INTO runs(id, task, kind, agent, caller, params, state, created, run_dir,"
                " timeout, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, task.name, task.kind, agent, principal, json.dumps(clean, ensure_ascii=False),
                 "awaiting_approval" if gated else "queued", self.clock(), run_dir, task.timeout, key))
            self.db.audit(principal, "run.submit", run=rid, task=task.name, params=clean)
        if gated:
            err = self._request_approval(self.db.run(rid))  # outside the lock: HTTP to Telegram
            if err:
                with self._lock:
                    self._finish(self.db.run(rid), "failed", None, err)
                return 503, {"error": err, "run": rid}
        return 202, self.view(self.db.run(rid))

    # --- owner approval ------------------------------------------------
    # A gated run waits in awaiting_approval. The request and a one-time code go to the owner
    # over Telegram, a channel the caller cannot read; the caller approves by passing on the
    # code the owner gives it. The owner may also approve directly, without a code.
    def _approval_key(self, rid):
        return f"approval:{rid}"

    def _code_hash(self, rid, code):
        return hashlib.sha256(f"{rid}:{code}".encode()).hexdigest()

    # A follow-up message to a gated task is a new instruction, so it waits for the owner too.
    # Its run goes back to awaiting_approval with turn already advanced; this key remembers
    # how to undo that if the follow-up is not approved.
    def _followup_key(self, rid):
        return f"followup:{rid}"

    def _drop_followup(self, run, reason):
        """Undo a follow-up that was not approved: the run returns to its previous state."""
        rid, turn, run_dir = run["id"], run["turn"], run["run_dir"]
        fu = self.db.get(self._followup_key(rid), {})
        msg = os.path.join(run_dir, f"message.{turn}.txt")
        if os.path.exists(msg):
            os.replace(msg, os.path.join(run_dir, f"message.{turn}.dropped.txt"))
        if fu.get("question_moved"):
            q = os.path.join(run_dir, f"question.{turn - 1}.txt")
            if os.path.exists(q):
                os.replace(q, os.path.join(run_dir, "question.txt"))
        self.db.x("DELETE FROM kv WHERE k IN (?,?)", (self._approval_key(rid), self._followup_key(rid)))
        self.db.update_run(rid, state=fu.get("prev_state", "succeeded"), turn=turn - 1)
        self.db.audit("peerlink", "run.message.dropped", run=rid, turn=turn, reason=reason)
        if run["caller"] != "owner":
            self.mail.post(run["caller"], "note", {"run": rid, "event": "followup.dropped",
                                                   "turn": turn, "reason": reason})

    def _request_approval(self, run, followup=None):
        rid = run["id"]
        code = f"{secrets.randbelow(10 ** 6):06d}"
        ttl = int(self.cfg["approval_ttl"])
        self.db.set(self._approval_key(rid), {"hash": self._code_hash(rid, code),
                                              "expires": self.clock() + ttl, "tries": 0})
        request = followup if followup is not None else json.dumps(run["params"], ensure_ascii=False,
                                                                   indent=2)
        label = f"Follow-up · turn {run['turn']}" if followup is not None else "Request"
        parts = telegram_parts(request)
        node, caller, task, run_id = (escape(str(value), quote=False) for value in
                                      (self.cfg['node'], run['caller'], run['task'], rid))
        texts = [f"⏳ <b>peerLink · Approval needed</b>\n\n"
                 f"<b>Task</b>  {task}\n"
                 f"<b>From</b>  {caller}\n"
                 f"<b>Node</b>  {node}\n"
                 f"<b>Run</b>  <code>{run_id}</code>\n\n"
                 f"<b>{label}{' · 1/' + str(len(parts)) if len(parts) > 1 else ''}</b>\n"
                 f"<pre>{escape(parts[0], quote=False)}</pre>"]
        texts += [f"📄 <b>peerLink · {label} {i + 2}/{len(parts)}</b>\n"
                  f"<b>Run</b>  <code>{run_id}</code>\n\n"
                  f"<pre>{escape(p, quote=False)}</pre>" for i, p in enumerate(parts[1:])]
        validity = f"{ttl // 60} min" if ttl % 60 == 0 else f"{ttl} sec"
        footer = (f"🔑 <b>Approval code</b>  <code>{code}</code>\n"
                  f"<b>Valid for</b>  {validity}\n\n"
                  "Give this code to your agent only if you approve this run.\n\n"
                  f"<b>To decline</b>\nIgnore this request, or run on {node}:\n"
                  f"<code>peerlink cancel {run_id}</code>")
        combined = texts[0] + "\n\n" + footer
        # Counting markup too is conservative, and keeps the single-message path safe.
        if len(parts) == 1 and len(combined.encode("utf-16-le")) // 2 <= 4096:
            texts = [combined]
        else:
            texts.append(f"🔑 <b>peerLink · Approval</b>\n\n"
                         f"<b>Task</b>  {task}\n"
                         f"<b>Run</b>  <code>{run_id}</code>\n\n" + footer)
        env = {k: str(v) for k, v in (self.cfg.get("env") or {}).items()}
        err = self.notifier(self.cfg.get("approval_telegram"), texts, env)
        self.db.audit("peerlink", "approval.request", run=rid, sent=not err, error=err)
        return err and f"could not send the approval request: {err}"

    def approve(self, principal, rid, code=None):
        with self._lock:
            run = self.db.run(rid)
            if not run or not self._may_see(principal, run):
                return 404, {"error": "no such run"}
            if run["state"] != "awaiting_approval":
                return 409, {"error": f"run is {run['state']}, not awaiting_approval"}
            key = self._approval_key(rid)
            st = self.db.get(key, {})
            followup = self.db.get(self._followup_key(rid)) is not None
            if principal != "owner":
                if self.clock() > st.get("expires", 0):
                    if followup:
                        self._drop_followup(run, "approval expired")
                        return 410, {"error": "approval expired; send the message again"}
                    self._finish(run, "cancelled", None, "approval expired")
                    return 410, {"error": "approval expired; submit again"}
                good = isinstance(code, str) and hmac.compare_digest(
                    self._code_hash(rid, code.strip()), st.get("hash", ""))
                if not good:
                    st["tries"] = st.get("tries", 0) + 1
                    self.db.set(key, st)
                    self.db.audit(principal, "run.approve.wrong", run=rid, tries=st["tries"])
                    if st["tries"] >= APPROVAL_TRIES:
                        if followup:
                            self._drop_followup(run, "too many wrong approval codes")
                            return 403, {"error": "wrong code; the follow-up is dropped"}
                        self._finish(run, "cancelled", None, "too many wrong approval codes")
                        return 403, {"error": "wrong code; the run is cancelled"}
                    return 403, {"error": "wrong code", "tries_left": APPROVAL_TRIES - st["tries"]}
            self.db.x("DELETE FROM kv WHERE k IN (?,?)", (key, self._followup_key(rid)))
            if followup:
                self.db.update_run(rid, state="queued", finished=None, error=None)
            else:
                self.db.update_run(rid, state="queued")
            self.db.audit(principal, "run.approve", run=rid, turn=run["turn"])
            return 202, self.view(self.db.run(rid))

    def _expire_approvals(self):
        for run in self.db.runs("state='awaiting_approval'"):
            if self.clock() > self.db.get(self._approval_key(run["id"]), {}).get("expires", 0):
                if self.db.get(self._followup_key(run["id"])) is not None:
                    self._drop_followup(run, "approval expired")
                else:
                    self._finish(run, "cancelled", None, "approval expired")

    def _over_cap(self, principal):
        """Submissions and follow-up turns both count toward the daily limit."""
        cap = self.cfg["principals"].get(principal, {}).get("daily_submits", 0)
        if not cap:
            return False
        since = self._midnight()
        runs = self.db.one("SELECT COUNT(*) AS n FROM runs WHERE caller=? AND created>=?",
                           (principal, since))["n"]
        msgs = self.db.one("SELECT COUNT(*) AS n FROM audit WHERE actor=? AND action='run.message'"
                           " AND ts>=?", (principal, since))["n"]
        return runs + msgs >= cap

    def message(self, principal, rid, text):
        with self._lock:
            status, res = self._message(principal, rid, text)
            if status != 202 or res.get("state") != "awaiting_approval":
                return status, res
            run = self.db.run(rid)
        err = self._request_approval(run, followup=text)  # outside the lock: HTTP to Telegram
        if err:
            with self._lock:
                run = self.db.run(rid)
                if run["state"] == "awaiting_approval":
                    self._drop_followup(run, err)
            return 503, {"error": err, "run": rid}
        return status, res

    def _message(self, principal, rid, text):
        run = self.db.run(rid)
        if not run or not self._may_see(principal, run):
            return 404, {"error": "no such run"}
        task = self.catalog.get(run["task"])
        if not task or not task.allowed(principal):
            return 404, {"error": "task no longer available"}
        if self.paused():
            return 503, {"error": "peerlink is paused (stop --all); try later"}
        if run["caller"] != principal and principal != "owner":
            return 403, {"error": "only the original caller can follow up"}
        if run["kind"] == "script":
            return 409, {"error": "script runs do not take messages"}
        if run["state"] not in ("needs_input", "succeeded"):
            return 409, {"error": f"run is {run['state']}; messages are accepted when it is"
                                  " needs_input or succeeded"}
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            return 400, {"error": "text must be 1..8000 chars"}
        if run["agent"] == "codex" and not run["session_id"]:
            return 409, {"error": "codex session id unknown; cannot resume"}
        if self._over_cap(principal):
            return 429, {"error": "daily submission limit reached"}
        turn = run["turn"] + 1
        with open(os.path.join(run["run_dir"], f"message.{turn}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        q = os.path.join(run["run_dir"], "question.txt")
        question_moved = os.path.exists(q)
        if question_moved:
            os.replace(q, os.path.join(run["run_dir"], f"question.{run['turn']}.txt"))
        if task.approve and principal != "owner":
            self.db.set(self._followup_key(rid), {"prev_state": run["state"],
                                                  "question_moved": question_moved})
            self.db.update_run(rid, turn=turn, state="awaiting_approval", pid=None)
        else:
            self.db.update_run(rid, turn=turn, state="queued", finished=None, error=None, pid=None)
        self.db.audit(principal, "run.message", run=rid, turn=turn)
        return 202, self.view(self.db.run(rid))

    def cancel(self, principal, rid):
        with self._lock:
            return self._cancel(principal, rid)

    def _cancel(self, principal, rid):
        run = self.db.run(rid)
        if not run or not self._may_see(principal, run):
            return 404, {"error": "no such run"}
        if run["caller"] != principal and principal != "owner":
            return 403, {"error": "only the original caller or the owner can cancel"}
        if run["state"] in ("queued", "needs_input", "awaiting_approval"):
            self._finish(run, "cancelled", None, "cancelled")
        elif run["state"] == "running":
            self.db.update_run(rid, cancel_requested=1)
        else:
            return 409, {"error": f"run already {run['state']}"}
        self.db.audit(principal, "run.cancel", run=rid)
        return 202, self.view(self.db.run(rid))

    def stop_all(self, actor="owner"):
        with self._lock:
            self.db.set("paused", True)
            for run in self.db.runs("state IN ('queued','running','needs_input','awaiting_approval')"):
                if run["state"] == "running":
                    self.db.update_run(run["id"], cancel_requested=1)
                else:
                    self._finish(run, "cancelled", None, "stop --all")
            self.db.audit(actor, "stop.all")

    def resume(self, actor="owner"):
        self.db.set("paused", False)
        self.db.audit(actor, "resume")

    # --- views ---------------------------------------------------------
    def _may_see(self, principal, run):
        return principal == "owner" or run["caller"] == principal

    def get(self, principal, rid):
        run = self.db.run(rid)
        if not run or not self._may_see(principal, run):
            return 404, {"error": "no such run"}
        v = self.view(run)
        if run["state"] == "running":
            files = executor.turn_files(run["run_dir"], run["turn"])
            v["log_tail"] = executor._tail(files["out"], 1500)
        return 200, v

    def list_runs(self, principal, state=None, limit=50):
        where, args = [], []
        if principal != "owner":
            where.append("caller=?")
            args.append(principal)
        if state:
            where.append("state=?")
            args.append(state)
        sql = " AND ".join(where) or "1=1"
        rows = self.db.runs(sql, tuple(args), order="created DESC", limit=limit)
        return [self.view(r, brief=True) for r in rows]

    def view(self, run, brief=False):
        v = {
            "run": run["id"], "task": run["task"], "kind": run["kind"], "agent": run["agent"],
            "caller": run["caller"], "state": run["state"], "turn": run["turn"],
            "created": run["created"], "started": run["started"], "finished": run["finished"],
        }
        if not brief:
            v.update(params=run["params"], result=run["result"], error=run["error"],
                     session_id=run["session_id"], run_dir=run["run_dir"])
        return v

    # --- engine loop -----------------------------------------------------
    def reconcile(self):
        """After a restart: runs marked running whose process is gone are finished from
        their exit file, or failed if there is none."""
        for run in self.db.runs("state='running'"):
            if run["pid"] and _ours(run):
                continue
            code = executor.read_exit(run)
            task = self.catalog.get(run["task"])
            if code is not None and task:
                self._complete(run, task, code)
            else:
                self._finish(run, "failed", None, "peerlink restarted and the run's process is gone")

    def tick(self):
        with self._lock:
            self.catalog.refresh()
            for run in self.db.runs("state='running'"):
                self._poll(run)
            self._expire_approvals()
            self._start_queued()
        # the doorbell may take seconds (HTTP); keep it out of the lock
        now = self.clock()
        if now - self._last_bell >= 15:
            self._last_bell = now
            self.doorbell.tick(now)
        if now - self._last_expire >= 3600:
            self._last_expire = now
            self.mail.expire(now)

    def loop(self, stop):
        self.reconcile()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep serving; record what broke
                self.db.audit("peerlink", "tick.error", error=repr(e))
            stop.wait(1.0)

    def _start_queued(self):
        if self.paused():
            return
        running = self.db.runs("state='running'")
        n_all = len(running)
        n_agent = sum(1 for r in running if r["kind"] != "script")
        held = set()
        for r in running:
            t = self.catalog.get(r["task"])
            if t:
                held.update(t.locks)
        for run in self.db.runs("state='queued'"):
            if n_all >= self.cfg["max_concurrent"]:
                break
            task = self.catalog.get(run["task"])
            if not task:
                self._finish(run, "failed", None, f"task {run['task']} no longer in the catalog")
                continue
            if task.is_agent and n_agent >= self.cfg["max_agent"]:
                continue
            if held & set(task.locks):
                continue
            try:
                proc, sid = executor.start(self.cfg, run, task)
            except Exception as e:
                self._finish(run, "failed", None, f"could not start: {e}")
                continue
            self.procs[run["id"]] = proc
            fields = {"state": "running", "pid": proc.pid, "cancel_requested": 0}
            if run["turn"] == 1:
                fields["started"] = self.clock()
            else:
                fields["started"] = run["started"] or self.clock()
            fields["timeout"] = self.clock() + task.timeout  # absolute deadline for this turn
            if sid:
                fields["session_id"] = sid
            self.db.update_run(run["id"], **fields)
            self.db.audit("peerlink", "run.start", run=run["id"], task=task.name, turn=run["turn"],
                          pid=proc.pid)
            n_all += 1
            if task.is_agent:
                n_agent += 1
            held.update(task.locks)

    def _poll(self, run):
        rid = run["id"]
        proc = self.procs.get(rid)
        if proc is not None:
            proc.poll()  # reap the wrapper if it exited
        code = executor.read_exit(run)
        task = self.catalog.get(run["task"])
        if code is not None:
            self.procs.pop(rid, None)
            self.vanished.pop(rid, None)
            if run["cancel_requested"]:
                self._finish(run, "cancelled", None, "cancelled")
            elif task:
                self._complete(run, task, code)
            else:
                self._finish(run, "failed", None, "task removed from catalog while running")
            return
        if run["cancel_requested"]:
            self._kill(run)
            self._finish(run, "cancelled", None, "cancelled while running")
            return
        if run["timeout"] and self.clock() > run["timeout"]:
            self._kill(run)
            self._finish(run, "timed_out", None, "exceeded the task timeout")
            return
        gone = (proc.poll() is not None) if proc is not None else not _ours(run)
        if run["pid"] and gone:
            first = self.vanished.setdefault(rid, self.clock())
            if self.clock() - first > 5:
                self.procs.pop(rid, None)
                self.vanished.pop(rid, None)
                self._finish(run, "failed", None, "run process vanished without an exit code")

    def _kill(self, run):
        pid = run["pid"]
        if not pid:
            return
        if run["id"] not in self.procs and not _ours(run):
            return  # pid was reused by something else (e.g. after a reboot)
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.time() + 5
        proc = self.procs.get(run["id"])
        while time.time() < deadline:
            if proc is not None:
                proc.poll()
            if not _alive(pid):
                break
            time.sleep(0.2)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        self.procs.pop(run["id"], None)

    def _reap(self, run):
        """Nothing a run starts outlives its turn: kill what it left behind (servers left
        running, nohup'd children). The wrapper itself is _poll's and _kill's to reap."""
        found = [p for p in executor.leftover_pids(run["id"], run["pid"]) if p != run["pid"]]
        if not found:
            return
        pids = found
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            deadline = time.time() + 3
            while sig == signal.SIGTERM and time.time() < deadline and any(_alive(p) for p in pids):
                time.sleep(0.2)
            pids = [p for p in pids if _alive(p)]
            if not pids:
                break
        self.db.audit("peerlink", "run.reap", run=run["id"], killed=found, survived=pids)

    def _complete(self, run, task, code):
        try:
            state, result, error, sid = executor.collect(self.cfg, run, task, code)
        except Exception as e:
            state, result, error, sid = "failed", None, f"could not read result: {e}", run["session_id"]
        self._finish(run, state, result, error, exit_code=code, session_id=sid)

    def _finish(self, run, state, result, error, exit_code=None, session_id=None):
        if run["pid"]:
            self._reap(run)
        if run["state"] == "awaiting_approval":
            self.db.x("DELETE FROM kv WHERE k IN (?,?)",
                      (self._approval_key(run["id"]), self._followup_key(run["id"])))
        fields = {"state": state, "finished": self.clock(), "result": result, "error": error}
        if exit_code is not None:
            fields["exit_code"] = exit_code
        if session_id:
            fields["session_id"] = session_id
        self.db.update_run(run["id"], **fields)
        self.db.audit("peerlink", "run.finish", run=run["id"], task=run["task"], state=state,
                      error=(error or "")[:300])
        run = self.db.run(run["id"])
        self._report(run)

    def _report(self, run):
        if run["caller"] == "owner":
            return
        task = self.catalog.get(run["task"])
        on = task.report_on if task else ["finished"]
        state = run["state"]
        body = {
            "run": run["id"], "task": run["task"], "state": state, "turn": run["turn"],
            "summary": (run["result"] or {}).get("summary") if isinstance(run["result"], dict) else None,
            "files": (run["result"] or {}).get("files") if isinstance(run["result"], dict) else None,
            "error": run["error"], "session_id": run["session_id"],
            "started": run["started"], "finished": run["finished"],
        }
        if state == "needs_input":
            body["question"] = (run["result"] or {}).get("question")
            self.mail.post(run["caller"], "run.question", body)
            return
        wanted = ("finished" in on and state in TERMINAL) or \
                 ("succeeded" in on and state == "succeeded") or \
                 ("failed" in on and state in FAILED_STATES)
        if wanted:
            self.mail.post(run["caller"], "run.result", body)

    # --- helpers -------------------------------------------------------
    def _today(self):
        return datetime.date.fromtimestamp(self.clock()).isoformat()

    def _midnight(self):
        d = datetime.datetime.fromtimestamp(self.clock())
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _ours(run):
    """True if run["pid"] is alive and is this run's wrapper (guards against pid reuse)."""
    pid = run["pid"]
    if not pid or not _alive(pid):
        return False
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True  # cannot tell; assume ours rather than fail a live run
    return "peerlink.wrap" in cmd and run["run_dir"] in cmd


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
