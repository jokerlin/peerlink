# peerlink

[English](README.md) | 简体中文

让远程 AI agent 经 Tailscale 在你自己的电脑上运行**你编进任务目录的任务**，结果以信件的形式寄回。有东西在等时，peerlink 调一个 webhook「门铃」把 agent 叫醒。

典型用法：一个云端 agent（比如 Grok 的云电脑）带着标签加入你的 tailnet，它只能访问你 Mac 上 peerlink 监听的那一个端口，也只能启动你写好的任务（固定脚本，或者交给 Pi / Codex 的 runbook），参数按 schema 校验。运行结束后，peerlink 把结果存成信件并调 agent 的 webhook；agent 醒来取信，再告诉你结果。

- 只用 Python 标准库（3.11+），外加 `tailscale` 命令行。
- 支持 script 任务，以及用你现有设置和登录运行 `pi --print` / `codex exec` 的 agent 任务。
- 支持追问：agent 可以向发起方提问，收到回答后接着同一个会话继续。
- 并发上限、资源锁、按天去重、每日提交上限、SQLite 状态、审计日志、急停。

## 工作方式

```
远程 agent（tag:agent）                       你的电脑
┌──────────────────────┐   tailnet，一个端口   ┌──────────────────────────────────────┐
│ peerlink.pyz submit  │ ───────────────────► │ peerlink serve                       │
│ peerlink.pyz sync    │ ◄─────────────────── │  ├ 身份：tailscale whois              │
└──────────▲───────────┘                      │  ├ 任务目录：~/.peerlink/tasks/*      │
           │ webhook：{"from": …, "pending": N}│  ├ 运行：script | pi | codex          │
           └──────────────────────────────────┤  └ 信件存到对方确认为止              │
                                              └──────────────────────────────────────┘
```

1. **身份**：每个请求都用 `tailscale whois` 对应到一个 principal。本机回环和你自己的登录是 `owner`；带标签的节点对应到你配置的 principal；其他一律 403。请求里自报的身份一概忽略。
2. **任务目录**：每个任务是一个带 `task.toml` 的目录。发起方按名字选任务、传参数，参数按任务的 schema 校验。目录热重载。
3. **运行**：每次运行有自己的目录和 `request.json`。script 任务运行任务自带的程序；agent 任务运行 Pi 或 Codex，提示词里只有 runbook 和 `request.json` 的**路径**，发起方的原文不进提示词。
4. **信件**：运行结束（或 agent 有问题要问）时，peerlink 给发起方存一封信，直到对方确认（最长 7 天）。
5. **门铃**：发起方配了 webhook 的话，peerlink 调它，只带 `{"from": "<节点>", "pending": N}`，不带内容。新信立即按，但 10 分钟内最多一次；没人取就 1 小时、3 小时、9 小时后再按。

## 安全模型

开放之前请先读这一节。

- **tailnet ACL 是第一道墙**：给远程 agent 的节点打标签，只允许这个标签访问本机的 peerlink 端口。
- **任务目录就是范围**：发起方只能运行 `~/.peerlink/tasks` 里的任务。写或改任务本身就是审批。建议把任务目录放进单独的 git 仓库，它的历史就是「发起方能做什么」的全部变更记录。
- **agent 任务没有沙箱**：`pi` / `codex` 任务以运行 peerlink 的用户的设置、权限和登录运行。runbook 里写的「不要做」只是请求，没有强制力。能事先写死的事优先做成 script 任务；像 `examples/tasks/ask` 这种自由发挥的任务，用 `approve = true` 加审批。
- **owner 审批**：`approve = true` 的任务被远程发起方提交后不会直接运行。peerlink 把请求原文和一个 6 位一次性批准码发到你的 Telegram，发起方只有拿到这个码才能启动运行，码由你在同意后告诉它。批准码绑定这一次运行，`approval_ttl` 后过期，错 5 次运行就取消。发起方看不到 Telegram 对话，所以理解错了或被劫持的 agent 没法自己批准自己。
- **文件经 peerlink 回传**：运行把文件放进 `out/`，发起方从同一个端口下载。任务不需要为了传东西去开端口、起服务。
- **运行起的进程不会比运行活得久**：一轮结束时，peerlink 杀掉运行留下的进程：运行会话里的所有进程（`nohup`、`&`），以及带着它的 `PEERLINK_RUN_ID` 的进程（`setsid` 也甩不掉），连同它们的子进程。离开会话、同时又清掉了环境变量的进程找不到；macOS 会隐藏 Apple 平台二进制（比如 `/bin/sh`）的环境变量，这类进程离开会话后也找不到。
- **不接受浏览器请求**：本机回环就是 owner，所以带浏览器请求头（`Origin`、`Sec-Fetch-Site`）的请求、不是 `application/json` 的 POST、以及陌生的 `Host` 一律拒绝。你 Mac 上打开的网页没法替你提交任务。
- **发起方的输入是数据**：参数按 schema 校验；自由文本写进 `request.json`，提示词要求 agent 把它当数据。
- **密钥只在 shell 环境文件里**：webhook 地址和 token 放在 `chmod 600` 的环境文件里，只被 shell `source`，peerlink 不会把它读进 Python 或写进日志，token 经标准输入交给 `curl`。
- **上限和刹车**：按 principal 的每日上限、并发上限、按任务的超时，以及 `peerlink stop --all`（取消全部运行并拒绝新任务，直到 `peerlink resume`）。

## 安装（macOS）

```bash
git clone https://github.com/jokerlin/peerlink ~/peerlink
mkdir -p ~/.peerlink/tasks && chmod 700 ~/.peerlink
cp ~/peerlink/deploy/config.example.toml ~/.peerlink/config.toml && chmod 600 ~/.peerlink/config.toml
$EDITOR ~/.peerlink/config.toml        # 你的 tailnet IP、你的登录、远程 agent 的标签

# 命令行包装
mkdir -p ~/.local/bin
printf '#!/bin/sh\nPYTHONPATH=%s exec python3 -m peerlink "$@"\n' ~/peerlink > ~/.local/bin/peerlink
chmod +x ~/.local/bin/peerlink

# 作为登录会话里的 LaunchAgent 常驻
sed -e "s|__PYTHON__|$(command -v python3)|" -e "s|__REPO__|$HOME/peerlink|g" -e "s|__HOME__|$HOME|g" \
  ~/peerlink/deploy/local.peerlink.plist > ~/Library/LaunchAgents/local.peerlink.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.peerlink.plist

peerlink doctor
```

用 LaunchAgent 而不是系统守护进程，是为了让任务能用你的登录会话：图形界面程序、钥匙串、Pi / Codex 的登录。核心是纯 Python，理论上别的系统也能跑，但目前只在 macOS 上用过。

> macOS 上，后台进程访问 `~/Documents`、`~/Desktop`、`~/Downloads` 会触发隐私弹窗，没人点就一直卡着。任务要用的东西请放在这些目录之外。

## 配置

`~/.peerlink/config.toml` 只需写和 `peerlink/config.py` 默认值不同的项，参考 `deploy/config.example.toml`。

| 键 | 默认 | 含义 |
|---|---|---|
| `node` | 主机名 | 给发起方看的节点名，也放在门铃里 |
| `listen` | `["127.0.0.1:8091"]` | 监听地址；加上你的 tailnet IP |
| `tasks_dir` / `work_dir` | `~/.peerlink/tasks` / `~/.peerlink/work` | 任务目录和运行目录 |
| `max_concurrent` / `max_agent` | 4 / 2 | 同时运行的总数 / agent 任务数 |
| `default_timeout` | `30m` | 没写 `timeout` 的任务用它 |
| `letter_ttl_days` | 7 | 未确认的信保留多久 |
| `doorbell_window` / `doorbell_retry` | 600 / `[3600, 10800, 32400]` | 门铃合并窗口和重按间隔（秒） |
| `env` | `{}` | 传给运行和门铃的额外环境变量（比如代理） |
| `pi_bin` / `codex_bin` | `pi` / `codex` | agent 命令行 |
| `approval_telegram` / `approval_ttl` | 无 / 1800 | 存 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 的环境文件，用来发批准码 / 批准码有效期（秒） |
| `principals.<名字>` | 只有 `owner` | `local`、`logins`、`tags`、`daily_submits`（0 = 不限）、`doorbell`（环境文件路径） |

## 任务

一个任务就是 `~/.peerlink/tasks/<名字>/task.toml` 加上它需要的文件，参考 `examples/tasks/`（`hello` 是 script 任务，`ask` 是 agent 任务）。

```toml
description = "在 GET /tasks 里给发起方看"
kind        = "script"            # script | pi | codex
command     = ["sh", "run.sh"]    # script 任务：程序，相对任务目录
# runbook   = "runbook.md"        # agent 任务：给 agent 的说明
# cwd       = "~/somewhere"       # 默认：任务目录（script）或运行目录（agent）
timeout     = "10m"
locks       = ["app:thing"]       # 声明同一把锁的运行排队执行
dedupe      = "per-day"           # 当天重复提交返回同一次运行（--retry 可以重跑失败的那次）
# allow     = ["grok/bot"]        # 默认：所有 principal
# enabled   = false
# approve   = true                # 远程发起方的运行要等你在 Telegram 上拿到的批准码
# env       = { KEY = "value" }

[params.count]                    # 参数会校验；未声明的参数直接拒绝
type = "integer"                  # string | integer | number | boolean
minimum = 1
maximum = 10
default = 3                       # 有默认值的参数是可选的
# 自由文本必须写 max_length；另有 enum、pattern、min_length、description

[report]
on = ["finished"]                 # 什么时候寄信：finished | succeeded | failed
```

- **script 任务**拿到 `PEERLINK_RUN_ID`、`PEERLINK_RUN_DIR`、`PEERLINK_TASK`、`PEERLINK_CALLER`、`PEERLINK_REQUEST`（`request.json` 的路径）和 `PEERLINK_PARAM_<NAME>`。结果取 `$PEERLINK_RUN_DIR/result.json` 和/或 `result.md`，都没有就取 stdout 末尾。退出码 0 表示成功。
- **agent 任务**把结论写进 `result.md`。要问发起方就写 `question.txt`：运行进入 `needs_input`，寄出 `run.question`，发起方 `POST /runs/<id>/messages` 回复后接着同一个会话继续（`pi --session <file>` / `codex exec resume`）。Pi 会话保存在每次运行目录的 `session.jsonl`。`ask` 默认使用 pi，传 `agent=codex` 可切换到 Codex。两种 CLI 都不支持 `max_turns`，请用 `timeout` 限制运行时长。
- **给发起方的文件**放进 `$PEERLINK_RUN_DIR/out/`：普通文件，文件名只含字母、数字、`.`、`_`、`-`，类型限 png、jpg、txt、log、md、json，单个不超过 10 MB，每次运行最多 50 个、合计 30 MB。一轮结束时，这些文件（名字、大小、sha256）列进结果和信件，发起方用 `GET /runs/<id>/files/<名字>` 下载；之后被改过的文件不再提供。
- `peerlink task lint [名字]` 检查任务格式；`peerlink task try <名字> key=value` 以非 owner 身份真跑一次，用临时数据库，不寄信。

## 命令行

在节点上：`serve`、`doctor`、`task lint`、`task try`、`log`、`inbox`、`stop --all`、`resume`、`telegram-test`、`approve <run>`（在节点上不需要批准码）。
在 tailnet 里任何地方（设置 `PEERLINK_PEER=http://<节点>:8091` 或用 `--peer`）：`info`、`tasks`、`submit <任务> k=v [--wait] [--retry]`、`status <run> [--wait]`、`runs`、`cancel <run>`、`msg <run> "内容"`、`approve <run> <批准码>`、`fetch <run> [--dir DIR]`、`sync [--save DIR] [--no-ack]`（`--save` 同时把信件的附件下载到 `DIR/<run>/`）。

## HTTP 接口

| 路由 | 谁能用 | 作用 |
|---|---|---|
| `GET /info` | 任何 principal | 节点名、协议版本、收信方式 |
| `GET /tasks` | 任何 principal | 调用方能看到的任务及参数 schema |
| `POST /runs` | 任何 principal | `{"task", "params", "dedupe_key"?, "retry"?}` → 运行 |
| `GET /runs`、`GET /runs/<id>` | 自己的运行（owner：全部） | 运行状态和结果 |
| `POST /runs/<id>/messages` | 运行的发起方 | 回答提问 / 追问 |
| `POST /runs/<id>/cancel` | 运行的发起方、owner | 取消 |
| `POST /runs/<id>/approve` | 运行的发起方（带 `{"code"}`）、owner | 启动处于 `awaiting_approval` 的运行 |
| `GET /runs/<id>/files/<名字>` | 运行的发起方、owner | 下载运行公开的文件 |
| `GET /mail`、`POST /mail/ack` | 任何 principal | 取信、确认 |
| `POST /msg` | 任何 principal | 给节点投一封信 |
| `GET /bundle.tar.gz` | 任何 principal | 给远程 agent 的客户端 zipapp 和 `SKILL.md` |
| `POST /admin/stop`、`POST /admin/resume` | owner | 急停 / 恢复 |

## 接入远程 agent（以 Grok 为例）

1. 在 Tailscale 里生成一个带标签的 auth key（比如 `tag:agent`，临时、可复用），并加一条 grant，只让这个标签访问本节点的端口：
   ```jsonc
   {"src": ["tag:agent"], "dst": ["host:<你的节点>"], "ip": ["tcp:8091"]}
   ```
2. 让 agent 在它的电脑上用这个 key 加入 tailnet（Grok 的云电脑可以在 `/workspace` 里以 userspace 模式运行 `tailscaled`）。
3. 在 `config.toml` 里给这个标签加一个 principal（见示例）。
4. agent 安装客户端：`curl -s http://<节点>:8091/bundle.tar.gz | tar xz -C /workspace/peerlink`。包里的 `SKILL.md`（来自 `skill/SKILL.md`，会填上你的节点名和地址）告诉它怎么提交任务、怎么处理信件。
5. 门铃：在 agent 那边建一个由 webhook 触发的 routine，提示词写明运行 `peerlink sync` 并汇总信件；把它的地址和 token 写进 `doorbell` 指向的环境文件（`WEBHOOK_URL=…`、`WEBHOOK_TOKEN=…`，`chmod 600`）。

## 用 Telegram 收批准码

1. 在 Telegram 里找 **@BotFather**，发 `/newbot`，起一个名字和一个以 `bot` 结尾的用户名。BotFather 会回复 bot token。
2. 打开和新 bot 的对话，随便发一条消息（bot 不能先给你发消息）。
3. 查你的 chat id：`curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"`，读 `result[0].message.chat.id`。
4. 写环境文件，不要放进 git：
   ```bash
   umask 077
   printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' '<TOKEN>' '<CHAT_ID>' > ~/.peerlink/secrets/telegram.env
   ```
5. 在 `config.toml` 里加 `approval_telegram = "~/.peerlink/secrets/telegram.env"`，重启 peerlink，运行 `peerlink telegram-test`。如果你那里访问 Telegram 要走代理，把代理写进 `[env]`。

## 开发

```bash
python3 -m unittest discover -s tests
```

测试用假的 `pi` / `codex` 和假的 `tailscale whois`，不需要联网。规则和发版步骤见 [CONTRIBUTING.md](CONTRIBUTING.md#简体中文)；报告漏洞见 [SECURITY.md](SECURITY.md#简体中文)。

## 许可证

[MIT](LICENSE)
