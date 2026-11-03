"""HTTP API on the tailnet address (and loopback for the local CLI).

Identity comes from `tailscale whois` on the connecting address; anything the
caller claims about itself in the request is ignored.

Callers are command-line clients, never browsers. Loopback is the owner, so a web page
open on this machine must not be able to call us: requests that carry browser headers
(Origin, Sec-Fetch-Site), POSTs that are not application/json, and Host names that are not
ours (DNS rebinding) are refused before anything else.
"""
import ipaddress
import json
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import executor
from .identity import Resolver, principal_for

PROTOCOL = 1
MAX_BODY = 256 * 1024
RUN_RE = re.compile(r"^/runs/([A-Za-z0-9_]+)(/messages|/cancel|/approve)?$")
FILE_RE = re.compile(r"^/runs/([A-Za-z0-9_]+)/files/([^/]+)$")
MAX_INBOUND_PER_DAY = 500


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind, which can
    hang for minutes when started by launchd."""
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Api:
    def __init__(self, engine, resolver=None):
        self.engine = engine
        self.resolver = resolver or Resolver()

    def principal(self, ip):
        peer = self.resolver.peer(ip)
        return peer, principal_for(peer, self.engine.cfg["principals"])

    def handle(self, method, path, body, ip):
        """Returns (status, payload)."""
        peer, who = self.principal(ip)
        url = urlparse(path)
        route = url.path.rstrip("/") or "/"
        if who is None:
            self.engine.db.audit(peer.node or ip, "denied", path=route, ip=ip)
            return 403, {"error": "unknown caller"}
        try:
            data = json.loads(body) if body else {}
            json.dumps(data, ensure_ascii=False).encode("utf-8")  # rejects lone surrogates
        except (ValueError, RecursionError, UnicodeEncodeError):
            return 400, {"error": "body must be valid UTF-8 JSON"}
        e = self.engine
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}

        if method == "GET" and route == "/bundle.tar.gz":
            from .bundle import build_tar
            e.db.audit(who, "bundle.fetch")
            return 200, build_tar(e.cfg)
        if method == "GET" and route == "/info":
            return 200, {"node": e.cfg["node"], "protocol": PROTOCOL, "receive": "direct",
                         "you": who, "paused": e.paused()}
        if method == "POST" and route == "/msg":
            n = e.db.one("SELECT COUNT(*) AS n FROM letters WHERE direction='in' AND sender=?"
                         " AND created>=?", (who, e._midnight()))["n"]
            if n >= MAX_INBOUND_PER_DAY:
                return 429, {"error": "too many letters today"}
            stored, err = e.mail.receive(who, data)
            if err:
                return 400, {"error": err}
            return 202, {"stored": stored}
        if method == "GET" and route == "/mail":
            return 200, {"letters": e.mail.pending(who)}
        if method == "POST" and route == "/mail/ack":
            ids = data.get("ids") if isinstance(data, dict) else None
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) > 500:
                return 400, {"error": "body must be {\"ids\": [\"...\"]}"}
            return 200, {"acked": e.mail.ack(who, ids)}
        if method == "GET" and route == "/tasks":
            return 200, {"tasks": [t.public() for t in e.catalog.visible(who)]}
        if method == "GET" and route == "/runs":
            try:
                limit = max(1, min(200, int(q.get("limit", 50))))
            except ValueError:
                return 400, {"error": "limit must be an integer"}
            return 200, {"runs": e.list_runs(who, q.get("state"), limit)}
        if method == "POST" and route == "/runs":
            if not isinstance(data, dict) or not isinstance(data.get("task"), str):
                return 400, {"error": "body must be {\"task\": name, \"params\": {...}}"}
            dk = data.get("dedupe_key")
            if dk is not None and not isinstance(dk, (str, int)):
                return 400, {"error": "dedupe_key must be a string"}
            return e.submit(who, data["task"], data.get("params") or {}, data.get("dedupe_key"),
                            bool(data.get("retry")))
        m = RUN_RE.match(route)
        if m:
            rid, sub = m.group(1), m.group(2)
            if method == "GET" and not sub:
                return e.get(who, rid)
            if method == "POST" and sub == "/cancel":
                return e.cancel(who, rid)
            if method == "POST" and sub == "/messages":
                if not isinstance(data, dict):
                    return 400, {"error": "body must be {\"text\": \"...\"}"}
                return e.message(who, rid, data.get("text"))
            if method == "POST" and sub == "/approve":
                code = data.get("code") if isinstance(data, dict) else None
                return e.approve(who, rid, code)
        m = FILE_RE.match(route)
        if m and method == "GET":
            run = e.db.run(m.group(1))
            if not run or not e._may_see(who, run):
                return 404, {"error": "no such run"}
            got = executor.read_output(run, m.group(2))
            if got is None:
                return 404, {"error": "no such file in the run's published files"}
            e.db.audit(who, "file.fetch", run=run["id"], name=m.group(2))
            return 200, got
        if who == "owner" and method == "POST" and route == "/admin/stop":
            e.stop_all()
            return 200, {"paused": True}
        if who == "owner" and method == "POST" and route == "/admin/resume":
            e.resume()
            return 200, {"paused": False}
        return 404, {"error": "not found"}


def refuse_browser(method, headers, cfg):
    """An error string if the request looks like it came from a web browser, else None."""
    if headers.get("Origin") is not None or headers.get("Sec-Fetch-Site") is not None:
        return "browser requests are not accepted"
    host = (headers.get("Host") or "").strip().lower()
    if host:
        name = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if name not in ("localhost", str(cfg.get("node", "")).lower()) and \
                    not name.endswith(".ts.net"):
                return f"unknown Host {name!r}"
    if method == "POST":
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return "POST body must be sent as Content-Type: application/json"
    return None


def make_handler(api):
    class H(BaseHTTPRequestHandler):
        server_version = "peerlink/1"
        protocol_version = "HTTP/1.1"

        def _go(self, method):
            refused = refuse_browser(method, self.headers, api.engine.cfg)
            if refused:
                api.engine.db.audit("peerlink", "http.refused", path=self.path[:200],
                                    ip=self.client_address[0], why=refused)
                self.close_connection = True
                return self._send(403, {"error": refused})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if n < 0:
                return self._send(400, {"error": "bad Content-Length"})
            if n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            body = self.rfile.read(n) if n else b""
            try:
                status, payload = api.handle(method, self.path, body, self.client_address[0])
            except Exception as e:
                api.engine.db.audit("peerlink", "http.error", path=self.path, error=repr(e))
                status, payload = 500, {"error": "internal error"}
            self._send(status, payload)

        def _send(self, status, payload):
            if isinstance(payload, tuple):  # a run's published file: (data, content type)
                data, ctype = payload
            elif isinstance(payload, bytes):
                data, ctype = payload, "application/gzip"
            else:
                data = json.dumps(payload, ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            if isinstance(payload, (tuple, bytes)):
                self.send_header("Content-Disposition", "attachment")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

        def log_message(self, *a):
            pass

    return H


def serve(engine, stop=None):
    stop = stop or threading.Event()
    api = Api(engine)
    servers = []
    for addr in engine.cfg["listen"]:
        host, port = addr.rsplit(":", 1)
        s = Server((host, int(port)), make_handler(api))
        servers.append(s)
        threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{addr}").start()
    engine.db.audit("peerlink", "serve.start", listen=engine.cfg["listen"])
    t = threading.Thread(target=engine.loop, args=(stop,), daemon=True, name="engine")
    t.start()
    return servers, stop, t
