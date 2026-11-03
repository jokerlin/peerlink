"""Client bundle for remote agents that can only reach us on the peerlink port.

GET /bundle.tar.gz returns:
    bin/peerlink.pyz   the peerlink package as a zipapp (python3 bin/peerlink.pyz ...)
    SKILL.md           how the remote agent should use it (skill/SKILL.md with this node's
                       name and address filled in)
Install: curl -s $PEERLINK_PEER/bundle.tar.gz | tar xz -C /workspace/peerlink
"""
import io
import os
import tarfile
import time
import zipfile

PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG)
MAIN = "import sys\nfrom peerlink.cli import main\nsys.exit(main())\n"


def build_pyz():
    buf = io.BytesIO()
    buf.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", MAIN)
        for fn in sorted(os.listdir(PKG)):
            if fn.endswith(".py"):
                z.write(os.path.join(PKG, fn), f"peerlink/{fn}")
    return buf.getvalue()


def peer_url(cfg):
    """The address remote agents should use: the first non-loopback listen address."""
    addrs = (cfg or {}).get("listen") or ["127.0.0.1:8091"]
    remote = [a for a in addrs if not a.startswith(("127.", "localhost", "[::1]"))]
    return "http://" + (remote or addrs)[0]


def build_tar(cfg=None):
    files = {"bin/peerlink.pyz": (build_pyz(), 0o755)}
    skill = os.path.join(ROOT, "skill", "SKILL.md")
    if os.path.exists(skill):
        with open(skill, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("{{PEER_URL}}", peer_url(cfg))
        text = text.replace("{{NODE}}", (cfg or {}).get("node", "the node"))
        files["SKILL.md"] = (text.encode("utf-8"), 0o644)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()
