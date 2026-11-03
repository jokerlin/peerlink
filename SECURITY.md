# Security policy

English | [简体中文](#简体中文)

## Supported versions

Security fixes go into the latest release and `main`.

## Report a vulnerability

Do not open a public issue for a security problem. Report it privately: open the repository's **Security** tab and click **Report a vulnerability** ([direct link](https://github.com/jokerlin/peerlink/security/advisories/new)).

Include the version (`peerlink --version`), what an attacker can do, and the steps to reproduce. You should get a reply within 7 days. Replace real tailnet IPs, host names, logins and tokens with examples.

## Scope

In scope:

- A caller that is not a configured principal gets anything other than 403.
- A principal runs a task it is not allowed to run, sees or cancels another caller's runs, or reaches an owner-only route.
- A run with `approve = true`, or a follow-up message to it, starts without the owner's code.
- Caller input (parameters, free text, file names) that leads peerlink to run a command, read or write a file outside the run directory, or change the prompt of an agent task.
- A web page in a browser on the node that gets a request through.
- Webhook URLs, tokens or Telegram credentials that end up in logs, letters, the database or a Python process.
- A process started by a run that survives the cleanup at the end of the turn, other than the cases listed in the README.

Out of scope:

- What an agent task does on your computer. Pi and Codex tasks run with your settings, permissions and login, and are not sandboxed (see "Security model" in the README). A runbook's "do not" is a request, not a limit.
- What a script task in your catalog does. Writing a task is the approval step.
- An attacker who can already edit `~/.peerlink`, the catalog, or the code peerlink runs.
- A tailnet ACL that lets more nodes reach the peerlink port than you intended.

---

## 简体中文

### 支持的版本

安全修复进入最新版本和 `main`。

### 报告漏洞

安全问题不要开公开 issue。请私下报告：打开仓库的 **Security** 标签页，点击 **Report a vulnerability**（[直达链接](https://github.com/jokerlin/peerlink/security/advisories/new)）。

写明版本（`peerlink --version`）、攻击者能做什么，以及复现步骤。7 天内会有回复。真实的 tailnet IP、主机名、登录名和 token 请换成示例值。

### 范围

属于范围内：

- 不在 principal 配置里的调用方，得到了 403 以外的响应。
- 某个 principal 运行了不允许它运行的任务，看到或取消了别人的 run，或者访问了只限 owner 的接口。
- `approve = true` 的 run 或发给它的追问，没有 owner 的验证码就开始执行。
- 调用方输入（参数、自由文本、文件名）导致 peerlink 执行命令、读写 run 目录以外的文件，或者改变 agent 任务的 prompt。
- 节点上浏览器里的网页，让请求通过了校验。
- webhook URL、token 或 Telegram 凭据出现在日志、信件、数据库或 Python 进程里。
- run 启动的进程在回合结束清理后仍然存活，README 里列出的情况除外。

不属于范围内：

- agent 任务在你电脑上做的事。Pi 和 Codex 任务使用你的设置、权限和登录，没有沙箱（见 README 的「安全模型」）。runbook 里的「不要做」只是请求，不是限制。
- 你任务目录里的 script 任务做的事。写任务这一步就是审批。
- 攻击者已经能修改 `~/.peerlink`、任务目录，或 peerlink 运行的代码。
- tailnet ACL 放行了比你预期更多的节点访问 peerlink 端口。
