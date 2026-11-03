"""Letters: the sender keeps each letter until the recipient acks it.

Outgoing letters for peers without a resident process (e.g. a cloud agent) wait here; the doorbell
tells them to come and fetch. Incoming letters (POST /msg) are stored by id, so a
redelivered letter is ignored.
"""
import json
import os
import subprocess
import tempfile
import time

from .db import new_id

LETTER_KINDS = {"run.result", "run.question", "note"}


def view(r):
    return {
        "id": r["id"],
        "from": r["sender"],
        "to": r["recipient"],
        "kind": r["kind"],
        "created": r["created"],
        "body": json.loads(r["body"]),
    }


class Mailbox:
    def __init__(self, db, node, ttl_days=7, clock=time.time):
        self.clock = clock
        self.db = db
        self.node = node
        self.ttl = ttl_days * 86400

    def post(self, recipient, kind, body, sender=None):
        lid = new_id("l")
        now = self.clock()
        self.db.x(
            "INSERT INTO letters(id, direction, sender, recipient, kind, body, created, expires)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (lid, "out", sender or self.node, recipient, kind,
             json.dumps(body, ensure_ascii=False), now, now + self.ttl),
        )
        self.db.audit(self.node, "letter.post", id=lid, to=recipient, kind=kind)
        return lid

    def pending(self, recipient):
        return [view(r) for r in self.db.q(
            "SELECT * FROM letters WHERE direction='out' AND recipient=? AND acked IS NULL"
            " AND expired IS NULL ORDER BY created", (recipient,))]

    def ack(self, recipient, ids):
        done = []
        now = self.clock()
        for lid in ids or []:
            n = self.db.x(
                "UPDATE letters SET acked=? WHERE id=? AND recipient=? AND direction='out'"
                " AND acked IS NULL", (now, lid, recipient))
            if n:
                done.append(lid)
        if done:
            self.db.audit(recipient, "letter.ack", ids=done)
        return done

    def receive(self, sender, letter):
        """Store an incoming letter. Returns (stored_new, error)."""
        if not isinstance(letter, dict):
            return False, "letter must be an object"
        lid = letter.get("id")
        kind = letter.get("kind")
        if not isinstance(lid, str) or not (1 <= len(lid) <= 80):
            return False, "letter needs an id"
        if kind not in LETTER_KINDS:
            return False, f"kind must be one of {sorted(LETTER_KINDS)}"
        body = letter.get("body", {})
        raw = json.dumps(body, ensure_ascii=False)
        if len(raw) > 65536:
            return False, "letter body too large"
        now = self.clock()
        with self.db.lock:
            exists = self.db.one("SELECT id FROM letters WHERE id=?", (lid,))
            if exists:
                return False, None
            self.db.x(
                "INSERT INTO letters(id, direction, sender, recipient, kind, body, created, expires)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (lid, "in", sender, self.node, kind, raw, now, now + self.ttl))
        self.db.audit(sender, "letter.receive", id=lid, kind=kind)
        return True, None

    def inbox(self, include_read=False):
        cond = "" if include_read else " AND acked IS NULL"
        return [view(r) for r in self.db.q(
            f"SELECT * FROM letters WHERE direction='in'{cond} ORDER BY created")]

    def mark_read(self, ids):
        now = self.clock()
        for lid in ids:
            self.db.x("UPDATE letters SET acked=? WHERE id=? AND direction='in'", (now, lid))

    def expire(self, now=None):
        now = now or self.clock()
        rows = self.db.q("SELECT id, recipient FROM letters WHERE acked IS NULL AND expired IS NULL"
                         " AND expires < ?", (now,))
        for r in rows:
            self.db.x("UPDATE letters SET expired=? WHERE id=?", (now, r["id"]))
            self.db.audit(self.node, "letter.expired", id=r["id"], to=r["recipient"])
        return len(rows)


# The webhook secret is only ever sourced by a shell, never read into Python, and the token
# reaches curl on stdin so it never shows up in the process list. The env file defines
# WEBHOOK_URL and WEBHOOK_TOKEN (GROK_WEBHOOK_URL / GROK_WEBHOOK_TOKEN are accepted too).
RING_SCRIPT = r'''
set -a; . "$1"; set +a
url=${WEBHOOK_URL:-$GROK_WEBHOOK_URL}
token=${WEBHOOK_TOKEN:-$GROK_WEBHOOK_TOKEN}
printf 'Authorization: Bearer %s\n' "$token" |
  curl -s -m 20 -o /dev/null -w '%{http_code}' -X POST -H @- \
    -H 'Content-Type: application/json' -d "$2" "$url"
'''


def ring_webhook(env_file, payload, extra_env=None):
    """Returns the HTTP status code (0 on transport failure)."""
    env = {**os.environ, **(extra_env or {})}
    try:
        out = subprocess.run(["/bin/zsh", "-c", RING_SCRIPT, "ring", env_file,
                              json.dumps(payload)], capture_output=True, text=True, timeout=30,
                             env=env)
        return int((out.stdout or "0").strip()[-3:] or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


# Same rule for the Telegram bot token: the env file (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) is
# sourced by the shell, and the token reaches curl through a config on stdin, not argv.
TELEGRAM_SCRIPT = r'''
set -a; . "$1"; set +a
shift
for f in "$@"; do
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" |
    curl -s -m 20 -o /dev/null -w '%{http_code}\n' -K - \
      --data-urlencode 'parse_mode=HTML' --data-urlencode 'link_preview_options={"is_disabled":true}' \
      --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text@$f" || echo 000
done
'''


def send_telegram(env_file, texts, extra_env=None):
    """Send each HTML-formatted text in order. Escape dynamic content before calling.

    Returns None or an error string.
    """
    if not env_file or not os.path.exists(env_file):
        return f"telegram env file {env_file or '(approval_telegram not set)'} not found"
    env = {**os.environ, **(extra_env or {})}
    with tempfile.TemporaryDirectory(prefix="peerlink-tg-") as tmp:
        paths = []
        for i, text in enumerate(texts):
            path = os.path.join(tmp, f"{i}.txt")
            with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8") as f:
                f.write(text)
            paths.append(path)
        try:
            out = subprocess.run(["/bin/zsh", "-c", TELEGRAM_SCRIPT, "telegram", env_file, *paths],
                                 capture_output=True, text=True, timeout=30 * len(paths) + 10,
                                 env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"telegram send failed: {e}"
    codes = out.stdout.split()
    if len(codes) != len(texts) or any(c != "200" for c in codes):
        return f"telegram send failed (http {' '.join(codes) or 'none'})"
    return None


class Doorbell:
    """Rings a principal's webhook when letters are waiting for it.

    - New letters ring at once, but at most once per `window` seconds, so letters
      arriving close together share one ring.
    - If letters stay unacked, ring again after each `retry` delay (1h, 3h, 9h).
    - The ring carries no content: {"from": node, "pending": N}.
    """

    def __init__(self, db, mailbox, principals, node, window=600, retry=(3600, 10800, 32400),
                 ringer=ring_webhook, env=None):
        self.env = env or {}
        self.db = db
        self.mail = mailbox
        self.principals = principals
        self.node = node
        self.window = window
        self.retry = list(retry)
        self.ringer = ringer

    def tick(self, now=None):
        now = now or time.time()
        rung = []
        for name, rule in self.principals.items():
            env = rule.get("doorbell")
            if not env:
                continue
            pending = self.mail.pending(name)
            key = f"doorbell:{name}"
            st = self.db.get(key, {"last_ring": 0, "attempt": 0, "rung_upto": 0})
            if not pending:
                if st["attempt"]:
                    st["attempt"] = 0
                    self.db.set(key, st)
                continue
            newest = max(l["created"] for l in pending)
            due = False
            if newest > st["rung_upto"]:
                due = now - st["last_ring"] >= self.window
                attempt = 1
            elif 1 <= st["attempt"] <= len(self.retry):
                due = now - st["last_ring"] >= self.retry[st["attempt"] - 1]
                attempt = st["attempt"] + 1
            if not due:
                continue
            if not os.path.exists(env):
                if not st.get("missing_logged"):
                    self.db.audit(self.node, "doorbell.missing", principal=name, env=env)
                    st["missing_logged"] = True
                    self.db.set(key, st)
                continue
            payload = {"from": self.node, "pending": len(pending)}
            code = self.ringer(env, payload, self.env) if self.env else self.ringer(env, payload)
            st.update(last_ring=now, attempt=attempt, rung_upto=newest, missing_logged=False,
                      last_code=code)
            self.db.set(key, st)
            self.db.audit(self.node, "doorbell.ring", principal=name, pending=len(pending),
                          attempt=attempt, http=code)
            rung.append((name, code))
        return rung
