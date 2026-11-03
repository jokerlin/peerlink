"""Node configuration: defaults merged with ~/.peerlink/config.toml."""
import copy
import os
import socket
import tomllib

DEFAULTS = {
    "node": socket.gethostname().split(".")[0] or "peerlink",
    "listen": ["127.0.0.1:8091"],  # add your tailnet address, e.g. "100.x.y.z:8091"
    "home": "~/.peerlink",
    "tasks_dir": "~/.peerlink/tasks",
    "work_dir": "~/.peerlink/work",
    "max_concurrent": 4,
    "max_agent": 2,
    "result_max_bytes": 32768,
    "letter_ttl_days": 7,
    "doorbell_window": 600,                  # merge letters within 10 minutes
    "doorbell_retry": [3600, 10800, 32400],  # ring again after 1h, 3h, 9h
    "default_timeout": "30m",
    "path": "/opt/homebrew/bin:/usr/local/bin:~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "env": {},                  # extra environment for runs and the doorbell (e.g. proxy)
    # Tasks with approve = true wait for the owner: peerlink sends the request and a one-time
    # code to Telegram (env file with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, chmod 600).
    "approval_telegram": "",
    "approval_ttl": 1800,
    "pi_bin": "pi",
    "codex_bin": "codex",
    # Who may call us, matched on `tailscale whois`. Loopback is always the owner; add your
    # own tailnet login to "owner" and one table per remote agent (see deploy/config.example.toml).
    "principals": {
        "owner": {
            "local": True,
            "logins": [],
            "daily_submits": 0,  # 0 = unlimited
        },
    },
}


def expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None, overrides=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = path or os.environ.get("PEERLINK_CONFIG") or "~/.peerlink/config.toml"
    path = expand(path)
    if os.path.exists(path):
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    if overrides:
        _merge(cfg, overrides)
    for k in ("home", "tasks_dir", "work_dir", "approval_telegram"):
        cfg[k] = expand(cfg[k])
    cfg["path"] = ":".join(expand(p) for p in cfg["path"].split(":"))
    for rule in cfg["principals"].values():
        if rule.get("doorbell"):
            rule["doorbell"] = expand(rule["doorbell"])
    return cfg


def parse_duration(s, default=None):
    """'110m' -> 6600; '30s', '2h', '1d'; bare numbers are seconds."""
    if s is None or s == "":
        return default
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)
