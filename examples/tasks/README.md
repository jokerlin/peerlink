# Example tasks

Copy a task into the node's catalog and it is live (the catalog is hot-reloaded):

```bash
cp -R examples/tasks/hello ~/.peerlink/tasks/
peerlink task lint hello
peerlink task try hello text=hi
```

- `hello`: a `script` task. The program lives in the task directory, params arrive as `PEERLINK_PARAM_<NAME>`, and the result is taken from `$PEERLINK_RUN_DIR/result.md`, `result.json` or the tail of stdout.
- `ask`: a `pi` task by default; pass `agent=codex` to use Codex. The agent reads `runbook.md` and `request.json` and writes its answer to `result.md`; to ask the caller something it writes `question.txt`. The agent inherits the Pi / Codex permissions of the user running peerlink, so think before exposing it.

See the main README for all task fields.
