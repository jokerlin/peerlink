"""Generate docs/flow.svg and docs/flow.zh-CN.svg, the README diagrams of one approved run.

usage: python3 docs/flow.py   (from the repository root)
"""
import os
from html import escape

W, H = 980, 720
X = {"you": 150, "grok": 490, "mac": 830}
TOP = 70

T = {
    "en": {
        "lanes": {"you": ("You", "Telegram + Grok chat"),
                  "grok": ("Grok Bot", "cloud computer"),
                  "mac": ("Your Mac", "peerlink")},
        "s1": ("1", "submit ask · tailnet, one port"),
        "s2": ("2", "the request + a one-time code"),
        "s3": ("3", "“the code is 482913”"),
        "s4": ("4", "approve <run> 482913"),
        "s5": ("5", "runs Pi / Codex on your Mac,", "keeps the result as a letter"),
        "s6": ("6", "doorbell {\"pending\": 1} · no content"),
        "s7": ("7", "sync · letters and files over the tailnet"),
        "s8": ("8", "summary and screenshots"),
        "foot1": "Grok Bot never sees the Telegram chat, so it cannot approve its own runs.",
        "foot2": "Results stay on the tailnet. The public webhook only says that mail is waiting.",
    },
    "zh": {
        "lanes": {"you": ("你", "Telegram + Grok 对话"),
                  "grok": ("Grok Bot", "云电脑"),
                  "mac": ("你的 Mac", "peerlink")},
        "s1": ("1", "提交 ask · tailnet，只开一个端口"),
        "s2": ("2", "请求原文 + 一次性批准码"),
        "s3": ("3", "“批准码是 482913”"),
        "s4": ("4", "approve <run> 482913"),
        "s5": ("5", "在你的 Mac 上运行 Pi / Codex，", "结果存成一封信"),
        "s6": ("6", "门铃 {\"pending\": 1} · 不带内容"),
        "s7": ("7", "sync · 经 tailnet 取信和文件"),
        "s8": ("8", "结果摘要和截图"),
        "foot1": "Grok Bot 看不到 Telegram，所以没法自己批准自己。",
        "foot2": "结果只走 tailnet。公网 webhook 上只有一句「有新信」。",
    },
}

STYLE = """
  .bg { fill: #fafaf9; stroke: #e7e5e4; }
  .t { fill: #1c1917; font: 15px -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Noto Sans CJK SC', sans-serif; }
  .h { font-size: 17px; font-weight: 650; }
  .m { fill: #57534e; font-size: 13px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13.5px; }
  .lane { fill: #ffffff; stroke: #d6d3d1; }
  .life { stroke: #d6d3d1; stroke-width: 1.5; stroke-dasharray: 4 5; }
  .net { stroke: #0f766e; fill: none; stroke-width: 2; }
  .tg { stroke: #2481cc; fill: none; stroke-width: 2; }
  .bell { stroke: #c2410c; fill: none; stroke-width: 2; stroke-dasharray: 7 5; }
  .chat { stroke: #78716c; fill: none; stroke-width: 2; }
  .dot-net { fill: #0f766e; } .dot-tg { fill: #2481cc; } .dot-bell { fill: #c2410c; } .dot-chat { fill: #78716c; }
  .num { fill: #fff; font: 600 12px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  .run { fill: #f0fdfa; stroke: #99f6e4; }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #1c1917; stroke: #44403c; }
    .t { fill: #f5f5f4; } .m { fill: #a8a29e; }
    .lane { fill: #292524; stroke: #57534e; }
    .life { stroke: #57534e; }
    .net { stroke: #2dd4bf; } .dot-net { fill: #0d9488; }
    .tg { stroke: #5aa9e6; } .dot-tg { fill: #2481cc; }
    .bell { stroke: #fb923c; } .dot-bell { fill: #c2410c; }
    .chat { stroke: #a8a29e; } .dot-chat { fill: #78716c; }
    .run { fill: #042f2e; stroke: #115e59; }
  }
"""


def text_width(s, mono=False):
    w = 0
    for ch in s:
        if ord(ch) > 0x2e80:
            w += 15
        else:
            w += 8.2 if mono else 7.9
    return w


def arrow(x1, x2, y, cls, num, label, mono=False):
    d = 1 if x2 > x1 else -1
    head = f"M{x2 - 2 * d} {y} l{-10 * d} -5 v10 z"
    lx = (x1 + x2) / 2 + 13
    cx = lx - text_width(label, mono) / 2 - 18
    lc = "t mono" if mono else "t"
    return (f'<path class="{cls}" d="M{x1 + 6 * d} {y} H{x2 - 9 * d}"/>'
            f'<path class="dot-{cls}" d="{head}"/>'
            f'<circle class="dot-{cls}" cx="{cx:.0f}" cy="{y - 15}" r="10"/>'
            f'<text class="num" x="{cx:.0f}" y="{y - 11}" text-anchor="middle">{num}</text>'
            f'<text class="{lc}" x="{lx:.0f}" y="{y - 10}" text-anchor="middle">{escape(label)}</text>')


def build(lang):
    t = T[lang]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="peerlink: one approved run between you, Grok Bot and your Mac">',
           f"<style>{STYLE}</style>",
           f'<rect class="bg" x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="16"/>']
    for k, x in X.items():
        name, sub = t["lanes"][k]
        out.append(f'<line class="life" x1="{x}" y1="{TOP + 62}" x2="{x}" y2="{H - 96}"/>')
        out.append(f'<rect class="lane" x="{x - 115}" y="{TOP - 30}" width="230" height="62" rx="10"/>')
        out.append(f'<text class="t h" x="{x}" y="{TOP - 3}" text-anchor="middle">{escape(name)}</text>')
        out.append(f'<text class="t m" x="{x}" y="{TOP + 19}" text-anchor="middle">{escape(sub)}</text>')
    y = TOP + 110
    you, grok, mac = X["you"], X["grok"], X["mac"]
    out.append(arrow(grok, mac, y, "net", *t["s1"]))
    y += 62
    out.append(arrow(mac, you, y, "tg", *t["s2"]))
    y += 62
    out.append(arrow(you, grok, y, "chat", *t["s3"]))
    y += 62
    out.append(arrow(grok, mac, y, "net", *t["s4"], mono=True))
    y += 30
    n, l1, l2 = t["s5"]
    out.append(f'<rect class="run" x="{mac - 165}" y="{y}" width="285" height="62" rx="10"/>')
    out.append(f'<circle class="dot-net" cx="{mac - 143}" cy="{y + 31}" r="10"/>'
               f'<text class="num" x="{mac - 143}" y="{y + 35}" text-anchor="middle">{n}</text>')
    out.append(f'<text class="t" x="{mac - 125}" y="{y + 26}">{escape(l1)}</text>'
               f'<text class="t" x="{mac - 125}" y="{y + 47}">{escape(l2)}</text>')
    y += 112
    out.append(arrow(mac, grok, y, "bell", *t["s6"], mono=False))
    y += 62
    out.append(arrow(grok, mac, y, "net", *t["s7"]))
    y += 62
    out.append(arrow(grok, you, y, "chat", *t["s8"]))
    fy = H - 52
    out.append(f'<text class="t m" x="{W / 2}" y="{fy}" text-anchor="middle">{escape(t["foot1"])}</text>')
    out.append(f'<text class="t m" x="{W / 2}" y="{fy + 22}" text-anchor="middle">{escape(t["foot2"])}</text>')
    out.append("</svg>\n")
    return "\n".join(out)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    open(f"{root}/docs/flow.svg", "w").write(build("en"))
    open(f"{root}/docs/flow.zh-CN.svg", "w").write(build("zh"))
