"""Task catalog: ~/.peerlink/tasks/<name>/task.toml, hot-reloaded on change."""
import os
import re
import threading
import tomllib

from . import schema
from .config import expand, parse_duration

KINDS = {"script", "pi", "codex"}
AGENT_KINDS = {"pi", "codex"}
TASK_KEYS = {"description", "kind", "command", "runbook", "cwd", "timeout", "max_turns",
             "locks", "dedupe", "params", "report", "enabled", "allow", "env", "approve"}
REPORT_ON = {"finished", "succeeded", "failed"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Task:
    def __init__(self, name, dir, raw, default_timeout=1800):
        self.name = name
        self.dir = dir
        self.raw = raw
        self.description = raw.get("description", "")
        self.kind = raw.get("kind", "script")
        self.command = list(raw.get("command") or [])
        self.runbook = raw.get("runbook")
        self.cwd = expand(raw["cwd"]) if raw.get("cwd") else None
        self.timeout = parse_duration(raw.get("timeout"), default_timeout)
        self.max_turns = raw.get("max_turns")
        self.locks = list(raw.get("locks") or [])
        self.dedupe = raw.get("dedupe")
        self.params = raw.get("params") or {}
        self.report_on = list((raw.get("report") or {}).get("on", ["finished"]))
        self.enabled = raw.get("enabled", True)
        self.allow = raw.get("allow")  # None = every known principal
        self.env = dict(raw.get("env") or {})
        self.approve = raw.get("approve", False)  # runs by non-owners wait for the owner's code

    @property
    def is_agent(self):
        return self.kind in AGENT_KINDS

    def allowed(self, principal):
        if not self.enabled:
            return False
        if principal == "owner":
            return True
        return self.allow is None or principal in self.allow

    def runbook_text(self):
        if not self.runbook:
            return ""
        with open(os.path.join(self.dir, self.runbook), encoding="utf-8") as f:
            return f.read()

    def public(self):
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "params": schema.describe(self.params),
            "timeout": self.timeout,
            "dedupe": self.dedupe,
            "approve": self.approve,
        }


def lint_raw(name, dir, raw):
    """Return (errors, warnings) for one task definition."""
    errs, warns = [], []
    if not NAME_RE.match(name):
        errs.append("task name must be lowercase letters, digits, - or _")
    unknown = set(raw) - TASK_KEYS
    if unknown:
        errs.append(f"unknown keys {sorted(unknown)}")
    kind = raw.get("kind", "script")
    if kind not in KINDS:
        errs.append(f"kind must be one of {sorted(KINDS)}")
    if not raw.get("description"):
        warns.append("no description (callers see it in GET /tasks)")
    try:
        t = parse_duration(raw.get("timeout"), 1800)
        if t <= 0 or t > 6 * 3600:
            errs.append("timeout must be between 1s and 6h")
    except (ValueError, IndexError):
        errs.append("timeout is not a duration like 30m")
    if kind == "script":
        cmd = raw.get("command")
        if not cmd or not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            errs.append("script task needs command = [\"prog\", \"arg\", ...]")
        else:
            errs += _check_command(dir, raw, cmd, warns)
        if raw.get("runbook"):
            warns.append("runbook is ignored for script tasks")
    if kind in AGENT_KINDS:
        rb = raw.get("runbook")
        if not rb:
            errs.append("agent task needs runbook = \"runbook.md\"")
        elif not os.path.isfile(os.path.join(dir, rb)):
            errs.append(f"runbook {rb} not found in task dir")
        mt = raw.get("max_turns")
        if mt is not None and (not isinstance(mt, int) or mt < 1 or mt > 500):
            errs.append("max_turns must be an integer 1..500")
        if mt is not None:
            warns.append("max_turns is not supported by pi/codex; use timeout to limit runs")
        if raw.get("command"):
            warns.append("command is ignored for agent tasks")
    if raw.get("cwd") and not os.path.isdir(expand(raw["cwd"])):
        errs.append(f"cwd {raw['cwd']} does not exist")
    d = raw.get("dedupe")
    if d not in (None, "per-day"):
        errs.append("dedupe must be \"per-day\" or absent")
    locks = raw.get("locks") or []
    if not isinstance(locks, list) or not all(isinstance(l, str) for l in locks):
        errs.append("locks must be a list of strings")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        errs.append("params must be a table")
    else:
        for pname, spec in params.items():
            errs += schema.check_spec(pname, spec)
        if "agent" in params and kind in AGENT_KINDS:
            bad = [a for a in params["agent"].get("enum", []) if a not in AGENT_KINDS]
            if bad or "enum" not in params["agent"]:
                errs.append("param agent must be an enum of pi/codex")
    on = (raw.get("report") or {}).get("on", ["finished"])
    if not isinstance(on, list) or set(on) - REPORT_ON:
        errs.append(f"report.on must be a subset of {sorted(REPORT_ON)}")
    if not isinstance(raw.get("approve", False), bool):
        errs.append("approve must be true or false")
    allow = raw.get("allow")
    if allow is not None and not (isinstance(allow, list) and all(isinstance(a, str) for a in allow)):
        errs.append("allow must be a list of principal names")
    return errs, warns


def _check_command(dir, raw, cmd, warns):
    """The program a script task runs should live in the task dir (or be an interpreter
    pointed at a file in the task dir), so editing the catalog is the only way to change it."""
    errs = []
    base = expand(raw["cwd"]) if raw.get("cwd") else dir

    def inside(p):
        full = os.path.realpath(os.path.join(base, expand(p)))
        return full, full.startswith(os.path.realpath(dir) + os.sep)

    prog = cmd[0]
    if "/" in prog:
        full, ok = inside(prog)
        if not os.path.exists(full):
            errs.append(f"command {prog} not found")
        elif not ok:
            warns.append(f"command {prog} is outside the task dir")
        return errs
    interpreters = {"python3", "python", "bash", "sh", "zsh", "node", "uv", "ruby", "perl"}
    if prog in interpreters:
        if len(cmd) > 2 and cmd[1] == "-m":
            warns.append(f"runs module {cmd[2]}; make sure it lives in the task dir or cwd")
            return errs
        files = [a for a in cmd[1:] if not a.startswith("-")]
        if files:
            full, ok = inside(files[0])
            if not os.path.exists(full):
                errs.append(f"script {files[0]} not found")
            elif not ok:
                warns.append(f"script {files[0]} is outside the task dir")
        return errs
    warns.append(f"command runs {prog} from PATH")
    return errs


class Catalog:
    def __init__(self, tasks_dir, default_timeout=1800):
        self.dir = tasks_dir
        self.default_timeout = default_timeout
        self._lock = threading.Lock()
        self._sig = None
        self.tasks = {}
        self.errors = {}

    def _signature(self):
        sig = []
        if not os.path.isdir(self.dir):
            return ()
        for name in sorted(os.listdir(self.dir)):
            for fn in ("task.toml",):
                p = os.path.join(self.dir, name, fn)
                try:
                    sig.append((p, os.stat(p).st_mtime_ns))
                except OSError:
                    pass
        return tuple(sig)

    def refresh(self, force=False):
        with self._lock:
            sig = self._signature()
            if not force and sig == self._sig:
                return False
            tasks, errors = {}, {}
            for p, _ in sig:
                tdir = os.path.dirname(p)
                name = os.path.basename(tdir)
                try:
                    with open(p, "rb") as f:
                        raw = tomllib.load(f)
                except (OSError, tomllib.TOMLDecodeError) as e:
                    errors[name] = [f"cannot parse task.toml: {e}"]
                    continue
                try:
                    errs, _ = lint_raw(name, tdir, raw)
                    if not errs:
                        tasks[name] = Task(name, tdir, raw, self.default_timeout)
                except Exception as e:  # a malformed task must not take the others down
                    errs = [f"invalid task.toml: {e!r}"]
                if errs:
                    errors[name] = errs
            self.tasks, self.errors, self._sig = tasks, errors, sig
            return True

    def get(self, name):
        self.refresh()
        return self.tasks.get(name)

    def visible(self, principal):
        self.refresh()
        return [t for t in self.tasks.values() if t.allowed(principal)]


def lint_dir(tdir):
    name = os.path.basename(os.path.normpath(tdir))
    p = os.path.join(tdir, "task.toml")
    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        return [f"cannot read task.toml: {e}"], []
    except tomllib.TOMLDecodeError as e:
        return [f"cannot parse task.toml: {e}"], []
    try:
        return lint_raw(name, tdir, raw)
    except Exception as e:
        return [f"invalid task.toml: {e!r}"], []
