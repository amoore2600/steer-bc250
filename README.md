# steer-bc250

Tools for testing how well a local LLM writes HDL, and for having Claude steer that LLM through HDL tasks.

The model runs on an AMD BC-250 board under llama.cpp. The coding agent is OpenCode, run headless and sandboxed on a
desktop. Every answer is graded by hidden tests: equivalence testbenches against a known-good ("golden") design, or a
known-answer checker; then the repository gates (Verilator -Wall, file hygiene, pinned names) and a Gowin synthesis
check.

There are four ways to run it:

| Mode | Script | What happens |
|---|---|---|
| One-shot | `run_api.py` | Each task prompt goes straight to the llama.cpp server; the reply is graded |
| Agent | `run_agent.py` | OpenCode works on each task in a sandboxed workspace, then its output file is graded |
| Steered agent | `steer/steer_agent.py` | As agent mode, but a decision engine (Claude) watches the agent and can steer, redirect or stop it, and the harness enforces the worker rules |
| Project | `steer/coordinator.py` | A project goal split into jobs, one steered job per inference server at a time, merged into one repository |

Three small example tasks are included (see [Example tasks](#example-tasks)). The task set used in development was
built from private HDL and is not published. See [Making tasks](#making-tasks) to build your own from Verilog or VHDL
you own.

## Our setup

| Machine | OS | Runs |
|---|---|---|
| Desktop | Ubuntu 24.04 LTS | The Claude desktop app (Code tab), OpenCode, the sandbox, the runners and the grader |
| AMD BC-250 | Fedora 44 Server | llama.cpp (`llama-server`, Vulkan) serving the model over the LAN |

The Claude app session acts as the steering engine through the file mailbox (`--engine mailbox`), because the
`claude` CLI is not used on this desktop.

## Layout

| Path | Contents |
|---|---|
| `lib/` | Task builder, testbench and self-check generators, grader (`grade.py`), repository gates (`hygiene.py`), section and known-answer helpers (`sections.py`, `gen_kat.py`, `kat_sections.py`), validators, resource-capped tool wrappers and the local heavy-job queue (`queue_run.sh`) |
| `agent/` | Sandbox (`sandbox.sh`, bubblewrap), TCP forwarder into the sandbox (`fwd.py`), worker OpenCode config, optional agent rules |
| `steer/` | The steerer, the project coordinator, the mailbox tool, the fake-LLM probe, the optional heat guard, the worker lessons, the offline tests and the runbook ([steer/README.md](steer/README.md)) |
| `tasks/` | Example tasks |
| `src/examples/` | Golden sources of the example rebuild and section tasks |
| `run_api.py`, `run_agent.py` | One-shot and agent-mode runners |
| `sampling.json` | Sampling presets used by `--sampling` |
| `opencode-skills/hdl-check/` | OpenCode skill the agent uses to lint, simulate and synthesise its own work |

Runs write to `results/`, workspaces to `agent_ws/`, and grader scratch to `scratch/`. All three are git-ignored.

## Requirements

- Linux with Python 3, `iverilog`, `verilator`, `ghdl` and `bwrap` (bubblewrap), all from apt.
- Gowin EDA, for the synthesis check and the GW5A primitive models. The default location is `~/gowin/IDE`; set
  `GOWIN_HOME` for another one. `GOWIN_PART` and `GOWIN_DEVICE_VERSION` choose the synthesis part (default
  GW5AST-LV138PG484AC1/I0, version B). Without Gowin, grade with `--no-syn` / `--grade-args=--no-syn`.
- The OpenCode CLI at `/opt/OpenCode/resources/opencode-cli`, for agent and steered modes (tested with v2.0.18).
- A llama.cpp server reachable over HTTP. The default is `localhost:8080`; point it at your server. Override it
  with `BAKEOFF_URL` for `run_api.py`, and `BAKEOFF_INFER_HOST` / `BAKEOFF_INFER_PORT` for the agent and steered
  runners. Inside the sandbox the agent always talks to `127.0.0.1:8080`, which the forwarder relays to that host.
  Model names are listed in `agent/opencode.sandbox.json`. The coordinator takes its servers from `--boards
  name=host:port,...` (default `$STEER_BOARDS`, else `board1=localhost:8080`).
- The scripts find the repository from their own location; `STEER_BC250_BASE` points them at another checkout.
- Install the `hdl-check` OpenCode skill, then generate its Gowin primitive stubs from your own Gowin install (they are
  derived from Gowin's library, so they are not shipped). `run_agent.py --preload-skills` also names a
  `gowin-primitives` skill on Gowin tasks; that skill is not included, so write your own or skip the flag:

```bash
cp -r opencode-skills/hdl-check ~/.config/opencode/skills/
python3 lib/prim_stubs.py ~/.config/opencode/skills/hdl-check/gw_prim_stubs.v OSER10 ELVDS_OBUF PLL DPB SDPB
```

## Example tasks

| Task | Kind | What the worker writes |
|---|---|---|
| `v_rebuild_ce_frac` | rebuild | A fractional clock enable (first-order accumulator), graded by equivalence with the golden |
| `v_section_pwm` | rebuild with sections | One of three sections of a PWM generator (`--tasks v_section_pwm@s2`) |
| `kat_debounce` | kat | A switch debouncer, graded by a known-answer testbench instead of a golden |

They are small on purpose: they exercise every grader stage and the offline tests use them. Their sources are MIT
(`src/examples/`, `tasks/kat_debounce/source/`).

## Making tasks

A task is a folder `tasks/<id>/` with a `task.json` and a `spec.md`. `lib/build_task.py tasks/<id>` turns it into a
runnable task: it copies the golden source from `src/` (or the task's `golden_local` file), writes the starting input,
the mutants, the equivalence testbench, the prompt and a `grade.sh`.

The task id prefix and `type` set the kind:

- **Rebuild** (`v_rebuild_*`, `vh_rebuild_*`, `gw_rebuild_*`): write a module from its spec, starting from an empty shell.
- **Section** (a rebuild with `sections`): write one labelled section of a module; the others are given.
- **Bug fix** (`v_bugfix_*`, `vh_bugfix_*`): a working module with one injected bug and a symptom report.
- **Testbench** (`tb_*`): write a testbench that passes on the golden design and catches the mutants.
- **Known answer** (`type: kat`): write a module that a supplied checker (a self-checking testbench, or an external
  reference run in lockstep) accepts; no golden is needed. A `kat.mode: sections` task fills the wiring sections of a
  top-level skeleton against a section spec.
- `v` is Verilog, `vh` is VHDL, `gw` uses Gowin GW5A primitives.

The main `task.json` fields are `id`, `type`, `language`, `top`, `source`, `golden_files`, `context_files`, `equiv`
(the equivalence-testbench setup) and `mutants`. Bug-fix tasks add `bug` and `symptom`; section tasks `sections`; kat
tasks `kat` and `reference`. The repository gates are set per task with `lint_wall`, `allow_initial`,
`allow_dead_code`, `allow_display`, `lint_waivers`, `pinned_names` and `max_mult`. Read `lib/build_task.py` and
[steer/README.md](steer/README.md) for the full set.

Then check that the task is sound:

```bash
python3 lib/validate.py tasks/<id> --no-syn    # golden passes, start fails, every mutant fails (drop --no-syn with Gowin)
python3 lib/gen_selfcheck.py tasks/<id>        # optional agent-visible self-check testbench (rebuild and bug fix)
python3 lib/validate_selfcheck.py <id>         # proves that self-check is sound
```

## Quick start

Grade a file by hand:

```bash
bash tasks/v_rebuild_ce_frac/grade.sh path/to/candidate.v --no-syn
```

One-shot run of every task:

```bash
python3 run_api.py --run-id oneshot_test --models gpt-oss-20b-MXFP4 --samples 1
```

Agent run with the self-check tests visible to the agent:

```bash
python3 run_agent.py --run-id agent_test --model gpt-oss-20b-MXFP4 --provide-tests --samples 1
```

Steered run, with a Claude session answering the decision mailbox. See [steer/README.md](steer/README.md) for the
engines, the mailbox commands, the harness rules and how to stop a worker.

```bash
python3 steer/steer_agent.py --run-id steer_test --engine mailbox --tasks v_rebuild_ce_frac --samples 1 \
    --provide-tests --auto-done --auto-first-steer --timeout 3600 --max-steers 6 --watchdog 600
```

All runners are resumable. Running again with the same `--run-id` skips the samples already graded.

## How the steerer works

1. The worker is `opencode run --format json` inside the sandbox; its JSON event stream is the observation.
2. A pre-gate scores each new event for importance. A loop detector watches for repeated identical calls.
3. When the gate fires, or a turn ends, the engine gets a goal pack (task statement, rules, recent events, the last
   self-check FAIL lines, a short diff of the graded file) and answers with one of `wait`, `steer`, `redirect`, `done`
   or `blocked`.
4. A steer is sent into the same OpenCode session with `opencode run --session <id>`.
5. Every passing self-check is committed in the workspace, so a later bad edit can be rolled back to the best version.
6. The harness enforces the worker rules itself, with one-line notes that do not count as steers: whole-file writes
   (an edit after a failed one is undone), a write guard against fragments and truncated files, lint and file checks
   on every write, a note when the same first failure repeats, and a done gate that runs the grader's own checks before
   it accepts a pass. The worker also gets curated lessons (`steer/worker-skills/hdl-lessons/SKILL.md`) in its
   AGENTS.md.

The engine can be a fixed rule set (`stub`), the `claude` CLI (`cli-sonnet`, `cli-opus`), or a file mailbox that a
Claude app session answers (`mailbox`).

## Results so far

On 18 Verilog and VHDL tasks with 3 samples each, gpt-oss-20b passed 36 of 54 alone and 50 of 54 when steered. In that
run the Claude session also read the worker's files and edited some engine answers, so the number measures the session
and the tool together. A run that relays the engine's answers unedited has not been done yet.

## Resource use

Grading wraps every tool in a memory cap and timeout (`lib/light.sh`). Gowin synthesis and other heavy jobs take a
lane lock (`/tmp/hdl_heavy.lock*`, `lib/heavy.sh`), so no more than three run at once. Synthesis that cannot wait
(a project's SYN_PENDING jobs, the kat-sections gate) goes through a queue runner, `lib/queue_run.sh` by default, over
the same lanes; `STEER_HEAVY_RUN` replaces it. An optional heat guard (`steer/heatguard.sh`, configured from
`steer/heatguard.conf.example`) steps the boards' GPU clocks down and stops a run when they run hot.

## Tests

The offline tests use a scripted fake LLM, the stub engine, private lock directories and a fake `gw_sh`; no board is
needed. See [steer/README.md](steer/README.md#tests).

## Credits

The steerer's design comes from [skillpack-v2](https://github.com/clabrado/skillpack-v2) by Chris Labrado, in
particular its `latch` steering loop (`latch/client/steerer.py`). From it this project takes:

- the goal pack handed to the decision engine;
- the materiality pre-gate that decides when to consult the engine (adapted from its `SteerGate._score`);
- the loop detector (adapted from its `LoopDetector`);
- the structured decision with `action`, `message`, `reasoning` and `evidence`, and the anti-thrash rules.

The transport is new here: OpenCode's JSON event stream and `run --session` replace latch's terminal driver, and the
Mac and zsh parts were not used. skillpack-v2 is under the MIT licence, Copyright (c) 2026 Chris Labrado; its notice
is reproduced in [NOTICE](NOTICE). Thanks to Chris for publishing it.

## Licence

MIT; see [LICENSE](LICENSE).
