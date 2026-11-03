"""peerlink command line.

On the node: peerlink serve | task lint | task try | stop --all | resume | doctor | log | telegram-test
Anywhere:  peerlink info | tasks | submit | status | runs | cancel | msg | approve | fetch | sync
Clients talk to PEERLINK_PEER (default http://127.0.0.1:8091).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import __version__
from . import catalog as catalog_mod
from . import config as config_mod


def _peer(args):
    return (args.peer or os.environ.get("PEERLINK_PEER") or "http://127.0.0.1:8091").rstrip("/")


def call(args, method, path, body=None, timeout=30):
    url = _peer(args) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": f"cannot reach {url}: {e}"}


def download(args, path, timeout=120):
    """GET raw bytes. Returns (status, bytes or error payload)."""
    url = _peer(args) + path
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": f"cannot reach {url}: {e}"}


def save_files(args, rid, files, dest):
    """Download a run's published files into dest/<run>/, checking each against its sha256.
    Returns [{name, path}] or [{name, error}]."""
    from .executor import OUT_NAME_RE
    got = []
    for f in files or []:
        name = f.get("name", "")
        if not OUT_NAME_RE.match(name):
            got.append({"name": name, "error": "bad file name"})
            continue
        status, data = download(args, f"/runs/{rid}/files/{name}")
        if status != 200:
            got.append({"name": name, "error": (data or {}).get("error", f"http {status}")})
            continue
        if hashlib.sha256(data).hexdigest() != f.get("sha256"):
            got.append({"name": name, "error": "sha256 mismatch"})
            continue
        d = os.path.join(dest, rid)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "wb") as fh:
            fh.write(data)
        got.append({"name": name, "path": path})
    return got


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _params(pairs):
    params = {}
    for p in pairs or []:
        if ":=" in p:
            k, v = p.split(":=", 1)
            params[k] = json.loads(v)
        elif "=" in p:
            k, v = p.split("=", 1)
            params[k] = v
        else:
            raise SystemExit(f"param {p!r} must be key=value (or key:=json)")
    return params


def _exit(status, payload):
    out(payload)
    return 0 if 200 <= status < 300 else 1


# --- client commands -------------------------------------------------------
def cmd_info(a):
    return _exit(*call(a, "GET", "/info"))


def cmd_tasks(a):
    return _exit(*call(a, "GET", "/tasks"))


def cmd_submit(a):
    body = {"task": a.task, "params": _params(a.params)}
    if a.dedupe:
        body["dedupe_key"] = a.dedupe
    if a.retry:
        body["retry"] = True
    status, res = call(a, "POST", "/runs", body)
    if not a.wait or not (200 <= status < 300):
        return _exit(status, res)
    return _wait(a, res["run"])


def _wait(a, rid):
    while True:
        status, res = call(a, "GET", f"/runs/{rid}")
        if status != 200 or res.get("state") not in ("queued", "running"):
            return _exit(status, res)
        time.sleep(2)


def cmd_status(a):
    if a.wait:
        return _wait(a, a.run)
    return _exit(*call(a, "GET", f"/runs/{a.run}"))


def cmd_runs(a):
    q = f"?limit={a.limit}" + (f"&state={a.state}" if a.state else "")
    return _exit(*call(a, "GET", "/runs" + q))


def cmd_cancel(a):
    return _exit(*call(a, "POST", f"/runs/{a.run}/cancel", {}))


def cmd_msg(a):
    return _exit(*call(a, "POST", f"/runs/{a.run}/messages", {"text": a.text}))


def cmd_approve(a):
    return _exit(*call(a, "POST", f"/runs/{a.run}/approve", {"code": a.code} if a.code else {}))


def cmd_fetch(a):
    status, res = call(a, "GET", f"/runs/{a.run}")
    if status != 200:
        return _exit(status, res)
    files = (res.get("result") or {}).get("files") if isinstance(res.get("result"), dict) else None
    got = save_files(a, a.run, files, a.dir)
    out({"run": a.run, "files": got})
    return 1 if any("error" in g for g in got) else 0


def cmd_sync(a):
    """Fetch letters addressed to me, print them, then ack (unless --no-ack)."""
    status, res = call(a, "GET", "/mail")
    if status != 200:
        return _exit(status, res)
    letters = res.get("letters", [])
    if a.save:
        os.makedirs(a.save, exist_ok=True)
        for l in letters:
            body = l.get("body") or {}
            if body.get("files") and body.get("run"):
                body["saved_files"] = save_files(a, body["run"], body["files"], a.save)
            with open(os.path.join(a.save, l["id"] + ".json"), "w", encoding="utf-8") as f:
                json.dump(l, f, ensure_ascii=False, indent=2)
    acked = []
    if letters and not a.no_ack:
        s2, r2 = call(a, "POST", "/mail/ack", {"ids": [l["id"] for l in letters]})
        acked = r2.get("acked", []) if s2 == 200 else []
    out({"letters": letters, "acked": acked})
    return 0


def cmd_stop(a):
    if not a.all:
        raise SystemExit("use: peerlink stop --all")
    return _exit(*call(a, "POST", "/admin/stop", {}))


def cmd_resume(a):
    return _exit(*call(a, "POST", "/admin/resume", {}))


# --- local commands ----------------------------------------------------------
def cmd_serve(a):
    from .engine import Engine
    from .server import serve
    cfg = config_mod.load(a.config)
    os.makedirs(cfg["home"], mode=0o700, exist_ok=True)
    os.makedirs(cfg["work_dir"], mode=0o700, exist_ok=True)
    engine = Engine(cfg)
    _, stop, t = serve(engine)
    print(f"peerlink serving on {', '.join(cfg['listen'])}", flush=True)
    try:
        while t.is_alive():
            t.join(3600)
    except KeyboardInterrupt:
        stop.set()
    return 0


def cmd_lint(a):
    cfg = config_mod.load(a.config)
    names = a.names or sorted(n for n in os.listdir(cfg["tasks_dir"])
                              if os.path.isfile(os.path.join(cfg["tasks_dir"], n, "task.toml")))
    bad = 0
    for n in names:
        tdir = n if os.path.isdir(n) and os.path.sep in n else os.path.join(cfg["tasks_dir"], n)
        errs, warns = catalog_mod.lint_dir(tdir)
        mark = "ok" if not errs else "FAIL"
        print(f"{mark:4}  {os.path.basename(os.path.normpath(tdir))}")
        for e in errs:
            print(f"      error: {e}")
        for w in warns:
            print(f"      warn:  {w}")
        bad += bool(errs)
    return 1 if bad else 0


def cmd_try(a):
    """Run a task once, for real, as the given principal, in a throwaway database."""
    from .engine import Engine
    cfg = config_mod.load(a.config)
    errs, warns = catalog_mod.lint_dir(os.path.join(cfg["tasks_dir"], a.task))
    for w in warns:
        print(f"warn: {w}", file=sys.stderr)
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 1
    tmp = tempfile.mkdtemp(prefix="peerlink-try-")
    cfg["home"] = tmp
    cfg["work_dir"] = os.path.join(cfg["work_dir"], "try")
    for rule in cfg["principals"].values():
        rule.pop("doorbell", None)
        rule["daily_submits"] = 0
    if not a.as_:  # default: the first remote principal, so the task's allow list is exercised
        a.as_ = next((n for n in cfg["principals"] if n != "owner"), "owner")
    engine = Engine(cfg)
    status, res = engine.submit(a.as_, a.task, _params(a.params))
    if status >= 300:
        out(res)
        return 1
    rid = res["run"]
    print(f"run {rid} as {a.as_}; dir {res['run_dir']}", file=sys.stderr)
    while True:
        engine.tick()
        run = engine.db.run(rid)
        if run["state"] not in ("queued", "running"):
            break
        time.sleep(1)
    view = engine.view(run)
    letters = engine.mail.pending(a.as_)
    out({"run": view, "letters_that_would_be_sent": letters})
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if run["state"] in ("succeeded", "needs_input") else 1


def cmd_log(a):
    from .db import DB
    cfg = config_mod.load(a.config)
    db = DB(os.path.join(cfg["home"], "peerlink.db"))
    rows = db.q("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (a.n,))
    for r in reversed(rows):
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        print(f"{ts}  {r['actor']:<10} {r['action']:<16} {r['detail']}")
    return 0


def cmd_inbox(a):
    from .db import DB
    from .mail import Mailbox
    cfg = config_mod.load(a.config)
    mb = Mailbox(DB(os.path.join(cfg["home"], "peerlink.db")), cfg["node"])
    letters = mb.inbox(include_read=a.all)
    out(letters)
    if a.mark_read:
        mb.mark_read([l["id"] for l in letters])
    return 0


def cmd_telegram_test(a):
    from html import escape
    from .mail import send_telegram
    cfg = config_mod.load(a.config)
    env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}
    node = escape(str(cfg['node']), quote=False)
    err = send_telegram(cfg["approval_telegram"], [
        "✅ <b>peerLink · Connected</b>\n\n"
        f"<b>Node</b>  {node}\n\n"
        "Approval requests and one-time codes will arrive here."
    ], env)
    print(err or "sent")
    return 1 if err else 0


def cmd_doctor(a):
    from .identity import tailscale_bin
    cfg = config_mod.load(a.config)
    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{'ok ' if good else 'BAD'}  {name}{('  ' + detail) if detail else ''}")

    check("tasks dir", os.path.isdir(cfg["tasks_dir"]), cfg["tasks_dir"])
    check("home dir mode 700", os.path.isdir(cfg["home"]) and
          (os.stat(cfg["home"]).st_mode & 0o077) == 0, cfg["home"])
    ts = subprocess.run([tailscale_bin(), "status", "--json"], capture_output=True, text=True)
    check("tailscale running", ts.returncode == 0)
    s, r = call(a, "GET", "/info", timeout=5)
    check("server answers on loopback", s == 200, json.dumps(r, ensure_ascii=False))
    for rule_name, rule in cfg["principals"].items():
        env = rule.get("doorbell")
        if env:
            exists = os.path.exists(env)
            mode_ok = exists and (os.stat(env).st_mode & 0o077) == 0
            check(f"doorbell file for {rule_name}", mode_ok,
                  env if exists else f"{env} missing (letters wait for sync)")
    tg = cfg.get("approval_telegram")
    if tg:
        exists = os.path.exists(tg)
        check("approval telegram file", exists and (os.stat(tg).st_mode & 0o077) == 0,
              tg if exists else f"{tg} missing")
    path = cfg["path"]
    for b in (cfg["pi_bin"], cfg["codex_bin"]):
        check(f"{b} on PATH", shutil.which(b, path=path), shutil.which(b, path=path) or "")
    names = [n for n in os.listdir(cfg["tasks_dir"])
             if os.path.isfile(os.path.join(cfg["tasks_dir"], n, "task.toml"))] \
        if os.path.isdir(cfg["tasks_dir"]) else []
    for n in sorted(names):
        errs, _ = catalog_mod.lint_dir(os.path.join(cfg["tasks_dir"], n))
        check(f"task {n}", not errs, "; ".join(errs))
        if not errs and not tg:
            raw = _task_raw(cfg["tasks_dir"], n)
            if raw.get("approve") and raw.get("enabled", True):
                check(f"task {n} approval", False, "approve = true but approval_telegram is not set;"
                      " every run by a non-owner will fail")
    return 0 if ok else 1


def _task_raw(tasks_dir, name):
    import tomllib
    try:
        with open(os.path.join(tasks_dir, name, "task.toml"), "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(prog="peerlink")
    p.add_argument("--version", action="version", version=f"peerlink {__version__}")
    p.add_argument("--peer", help="peer URL (default $PEERLINK_PEER or http://127.0.0.1:8091)")
    p.add_argument("--config", help="config file (default ~/.peerlink/config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("info").set_defaults(fn=cmd_info)
    sub.add_parser("tasks").set_defaults(fn=cmd_tasks)
    s = sub.add_parser("submit")
    s.add_argument("task")
    s.add_argument("params", nargs="*", help="key=value or key:=json")
    s.add_argument("--dedupe")
    s.add_argument("--retry", action="store_true", help="rerun even if today's run failed")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("status")
    s.add_argument("run")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("runs")
    s.add_argument("--state")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_runs)
    s = sub.add_parser("cancel")
    s.add_argument("run")
    s.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("msg")
    s.add_argument("run")
    s.add_argument("text")
    s.set_defaults(fn=cmd_msg)
    s = sub.add_parser("approve", help="approve a run waiting for the owner")
    s.add_argument("run")
    s.add_argument("code", nargs="?", help="the code the owner got on Telegram (not needed on the node)")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("fetch", help="download a run's files")
    s.add_argument("run")
    s.add_argument("--dir", default="peerlink-files", help="saves to DIR/<run>/ (default ./peerlink-files)")
    s.set_defaults(fn=cmd_fetch)
    s = sub.add_parser("sync", help="fetch and ack letters addressed to me")
    s.add_argument("--no-ack", action="store_true")
    s.add_argument("--save", help="also write each letter to DIR/<id>.json and its files to DIR/<run>/")
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("stop")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_stop)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    s = sub.add_parser("log")
    s.add_argument("-n", type=int, default=40)
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("inbox")
    s.add_argument("--all", action="store_true")
    s.add_argument("--mark-read", action="store_true")
    s.set_defaults(fn=cmd_inbox)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("telegram-test", help="send a test message to the approval chat").set_defaults(
        fn=cmd_telegram_test)

    t = sub.add_parser("task").add_subparsers(dest="tcmd", required=True)
    s = t.add_parser("lint")
    s.add_argument("names", nargs="*")
    s.set_defaults(fn=cmd_lint)
    s = t.add_parser("try")
    s.add_argument("task")
    s.add_argument("params", nargs="*")
    s.add_argument("--as", dest="as_", help="principal to run as (default: first non-owner principal)")
    s.set_defaults(fn=cmd_try)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
