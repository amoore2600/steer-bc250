# Claude-steered OpenCode on the BC-250

A Claude session steers OpenCode (gpt-oss-20b on the BC-250) through HDL tasks. The worker runs sandboxed; its JSON event
stream is watched; a decision engine answers wait / steer / redirect / done / blocked; steers go back into the same
OpenCode session with `opencode run --session <id>`. The loop design is adapted from
[skillpack-v2](https://github.com/clabrado/skillpack-v2) by Chris Labrado (MIT); see `../NOTICE`.

## Files

| File | What it is |
|---|---|
| `steer_agent.py` | The steerer: workspace, sandbox, forwarder, pre-gate, loop detector, checkpoints, engines, grading |
| `mailbox.py` | The Claude side of `--engine mailbox`: `pending`, `show`, `reply`, `watch` |
| `resume_test.sh` | Proof that an OpenCode session survives a kill and can be continued in the sandbox |

## Run it

Preflight: the BC-250 router must be up (`curl -s http://$BAKEOFF_INFER_HOST:8080/models`), and nothing else heavy should be
running on the desktop (`ls -d /tmp/hdl_heavy.lock*`).

```bash
cd ~/steer-bc250
python3 steer/steer_agent.py --run-id <name> --engine mailbox --tasks <t1,t2,...> --samples 1 \
    --provide-tests --auto-done --auto-first-steer --timeout 3600 --max-steers 6 --watchdog 600 --mailbox-timeout 900
```

- `--engine mailbox`: a Claude session answers each request (the claude CLI is not logged in on this machine).
- `--engine cli-sonnet` / `cli-opus`: unattended, needs a logged-in `claude` CLI.
- `--engine stub`: fixed rules, no model (dry runs).
- `--run-id <name> --kill-sid <opencode session id>`: kill one worker by its session id (pid and process group are in `workers.jsonl`).
- Resumable: rerunning the same `--run-id` skips sessions already graded.

## Answering the mailbox (Claude side)

```bash
python3 steer/mailbox.py watch results/steer_<run-id>          # one line per new request (use with Monitor)
python3 steer/mailbox.py show  <req.json>                      # the ruleset and the request prompt
python3 steer/mailbox.py reply <req.json> '{"action": "...", "message": "...", "reasoning": "...", "evidence": "..."}'
```

For a reproducible number, relay each request's `system` and `prompt` to the engine unedited and write its answer back
unedited. The 2026-11 run did not do that (the session added its own file analysis and edited some answers), so its
50/54 measures the session plus Sonnet, not the tool alone.

## Outputs

`results/steer_<run-id>/`: `results.jsonl` (grader verdict per session), `decisions/`, `events/`, `readout/`,
`goalpacks/`, `mailbox/`, `workers.jsonl`. Workspaces are in `agent_ws/steer_<run-id>/` (large; safe to delete after a
run).

## Known limits

- The fixes made late in the first run (real-execution PASS detection, snapshot checkpoints, queued-steer escalation and
  clear-on-redirect, stale-steer drop) are unit-tested offline only; verify them on the next run.
- The engine prompt does not yet include the worker's diff against the starting file or its last FAIL lines; the session
  supplied those by hand in the first run.

## Dependencies outside this repo

| What | Where | Notes |
|---|---|---|
| OpenCode | `/opt/OpenCode/resources/opencode-cli` | Run used v2.0.18. An app update can change `run --session` or the JSON events; rerun `resume_test.sh` after one |
| Worker config | `agent/opencode.sandbox.json` (in repo) | Bound into the sandbox as `OPENCODE_CONFIG`; points at the forwarder on 127.0.0.1:8080 |
| Desktop OpenCode config | `~/.config/opencode/` | Bound read-only into the sandbox |
| BC-250 router | `llama-server.service` on the BC-250 | Enabled system unit, starts on boot; models from `/etc/llama-server/models.ini`, one loaded at a time |
| Heavy lanes | `/tmp/hdl_heavy.lock*` | Shared with any other heavy jobs that use the same locks |
| Sandbox | `bwrap` | From apt |
