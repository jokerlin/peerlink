# Contributing

English | [简体中文](#简体中文)

Bug reports, fixes and small features are welcome. For a larger change, or anything that touches the security model, open an issue first so we can agree on the approach.

## Develop

peerlink uses only the Python standard library (3.11+). There is nothing to install. Run the CLI from a checkout:

```sh
PYTHONPATH=. python3 -m peerlink --help
PYTHONPATH=. python3 -m peerlink task lint     # checks the tasks in ~/.peerlink/tasks
```

To try a server without touching your real setup, point `--config` at a throwaway config whose `home`, `tasks_dir` and `work_dir` are in a temporary directory, and listen on loopback only.

## Test

```sh
python3 -m unittest discover -s tests
```

The tests use fake `pi`, `codex` and `tailscale whois`, and make no network requests. They need `ps`, which macOS and the GitHub Linux runners have; minimal containers such as `python:*-slim` need the `procps` package. CI runs the tests on Ubuntu and macOS with Python 3.11 and 3.13.

Add a test for every behavior you change. A change to identity, approval, file serving or cleanup needs a test in `tests/test_safety.py`.

## Diagrams

The README diagrams `docs/flow.svg` and `docs/flow.zh-CN.svg` are generated. Edit `docs/flow.py`, then run `python3 docs/flow.py` and check both files in a browser, in light and dark mode.

## Rules

- Standard library only. Do not add third-party dependencies.
- Caller input is data. It must never reach a shell command line or the prompt of an agent task.
- Secrets stay in shell env files. Python code must not read, log or store them.
- Use example values in code, tests, docs and issues: tailnet IPs from `100.64.0.0/24`, `you@example.com`, `home-mac`, `tag:agent`. Never commit a real IP, host name, login, path under your home directory, or token.
- Update both READMEs and `CHANGELOG.md` under `[Unreleased]` when settings, commands or behavior change.

## Release

Users install from `main`; a release marks a version and lists its changes.

1. Move the `[Unreleased]` entries in `CHANGELOG.md` under a new heading `## [X.Y.Z] - YYYY-MM-DD`, update the links at the bottom, set `__version__ = "X.Y.Z"` in `peerlink/__init__.py`, and merge that through a pull request.
2. Tag the merge commit on `main` and push the tag:

   ```sh
   git tag -a vX.Y.Z -m "peerlink X.Y.Z" && git push origin vX.Y.Z
   ```

The `Release` workflow then runs the tests and publishes a GitHub release with the `[X.Y.Z]` section of `CHANGELOG.md` plus a compare link to the previous tag. It stops without a release when the tests fail, the section is missing, or `peerlink/__init__.py` does not have version `X.Y.Z`.

To check the notes first, run the workflow by hand: **Actions → Release → Run workflow** with the version (or `gh workflow run release.yml -f version=X.Y.Z`). This dry run creates no release. `sh scripts/release-notes.sh X.Y.Z` prints the same section locally.

---

## 简体中文

欢迎报告 bug、提交修复和小功能。较大的改动，或涉及安全模型的改动，请先开 issue，商定做法后再动手。

### 开发

peerlink 只用 Python 标准库（3.11+），不需要安装依赖。在仓库目录里直接运行命令行：

```sh
PYTHONPATH=. python3 -m peerlink --help
PYTHONPATH=. python3 -m peerlink task lint     # 检查 ~/.peerlink/tasks 里的任务
```

想试运行服务又不影响现有部署：用 `--config` 指向一份临时配置，把 `home`、`tasks_dir` 和 `work_dir` 放在临时目录里，只监听 loopback。

### 测试

```sh
python3 -m unittest discover -s tests
```

测试使用假的 `pi`、`codex` 和 `tailscale whois`，不访问网络。测试需要 `ps`：macOS 和 GitHub 的 Linux runner 自带；`python:*-slim` 这类精简容器要装 `procps`。CI 在 Ubuntu 和 macOS 上用 Python 3.11 和 3.13 运行测试。

每个改动的行为都要加测试。涉及身份识别、审批、文件下载或进程清理的改动，要在 `tests/test_safety.py` 里加测试。

### 流程图

README 里的 `docs/flow.svg` 和 `docs/flow.zh-CN.svg` 是生成的。修改 `docs/flow.py` 后运行 `python3 docs/flow.py`，再在浏览器里用亮色和暗色模式检查两个文件。

### 规则

- 只用标准库，不引入第三方依赖。
- 调用方输入是数据，不得进入 shell 命令行，也不得进入 agent 任务的 prompt。
- 密钥只放在 shell env 文件里。Python 代码不得读取、记录或保存密钥。
- 代码、测试、文档和 issue 里只用示例值：tailnet IP 用 `100.64.0.0/24`，以及 `you@example.com`、`home-mac`、`tag:agent`。不得提交真实 IP、主机名、登录名、家目录下的路径或 token。
- 设置项、命令或行为变化时，同时更新两份 README，并在 `CHANGELOG.md` 的 `[Unreleased]` 下记录。

### 发版

用户直接从 `main` 安装；Release 用来标记版本、列出改动。

1. 把 `CHANGELOG.md` 里 `[Unreleased]` 的条目移到新标题 `## [X.Y.Z] - YYYY-MM-DD` 下，更新底部链接，把 `peerlink/__init__.py` 里的 `__version__` 改成 `"X.Y.Z"`，通过 PR 合入。
2. 在 `main` 的合并提交上打 tag 并推送：

   ```sh
   git tag -a vX.Y.Z -m "peerlink X.Y.Z" && git push origin vX.Y.Z
   ```

之后 `Release` 工作流会跑测试，并用 `CHANGELOG.md` 里 `[X.Y.Z]` 那一段加上和上一个 tag 的对比链接，发布 GitHub Release。测试失败、找不到这一段，或 `peerlink/__init__.py` 的版本不是 `X.Y.Z` 时，工作流中止，不发 Release。

想先看 notes，手动运行工作流：**Actions → Release → Run workflow**，填入版本号（或 `gh workflow run release.yml -f version=X.Y.Z`）。这是 dry run，不会创建 Release。本地运行 `sh scripts/release-notes.sh X.Y.Z` 也能看到同一段内容。
