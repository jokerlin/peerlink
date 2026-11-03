"""Who is calling: resolve a peer IP to a principal via `tailscale whois`."""
import json
import os
import shutil
import subprocess
import threading
import time

TAILSCALE_CANDIDATES = [
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
]


def tailscale_bin():
    for p in TAILSCALE_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("tailscale") or "tailscale"


class Peer:
    __slots__ = ("ip", "node", "tags", "login")

    def __init__(self, ip, node="", tags=(), login=""):
        self.ip = ip
        self.node = node
        self.tags = list(tags or ())
        self.login = login

    def as_dict(self):
        return {"ip": self.ip, "node": self.node, "tags": self.tags, "login": self.login}


def whois_raw(ip, timeout=5):
    """Return parsed `tailscale whois --json` for ip, or None if not a tailnet peer."""
    try:
        out = subprocess.run(
            [tailscale_bin(), "whois", "--json", ip],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


class Resolver:
    """Caches whois results for a short time; loopback is the local owner."""

    def __init__(self, ttl=60, whois=whois_raw):
        self.ttl = ttl
        self.whois = whois
        self._cache = {}
        self._lock = threading.Lock()

    def peer(self, ip):
        if ip in ("127.0.0.1", "::1"):
            return Peer(ip, node="localhost", login="local")
        now = time.time()
        with self._lock:
            hit = self._cache.get(ip)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        raw = self.whois(ip)
        if raw is None:
            p = Peer(ip)
        else:
            node = raw.get("Node") or {}
            prof = raw.get("UserProfile") or {}
            p = Peer(
                ip,
                node=node.get("ComputedName") or (node.get("Name") or "").split(".")[0],
                tags=node.get("Tags") or [],
                login="" if node.get("Tags") else prof.get("LoginName", ""),
            )
        with self._lock:
            self._cache[ip] = (now, p)
        return p


def principal_for(peer, principals):
    """Map a Peer to a principal name using config.

    principals: {name: {"tags": [...], "logins": [...], "local": bool}}
    Returns the principal name, or None for unknown callers.
    """
    for name, rule in principals.items():
        if peer.login == "local" and rule.get("local"):
            return name
        if peer.login and peer.login in rule.get("logins", []):
            return name
        if any(t in rule.get("tags", []) for t in peer.tags):
            return name
    return None
