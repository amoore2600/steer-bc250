# Claude-steered OpenCode

A Claude session steers OpenCode (a local model such as gpt-oss-20b served by llama.cpp, in our setup on an AMD BC-250)
through HDL tasks. The worker runs sandboxed; its JSON event stream is watched; a decision engine answers wait / steer /
redirect / done / blocked; steers go back into the same OpenCode session with `opencode run --session <id>`. The loop
design is adapted from [skillpack-v2](https://github.com/clabrado/skillpack-v2) by Chris Labrado (MIT); see `../NOTICE`.

## Files

| File | What it is |
|---|---|
| `steer_agent.py` | The steerer: workspace, sandbox, forwarder, pre-gate, loop detector, checkpoints, engines, grading |
| `coordinator.py` | One project goal split into jobs, one job per inference server at a time (see Projects below) |
| `mailbox.py` | The Claude side of `--engine mailbox`: `pending`, `show`, `reply`, `watch` |
| `oc_probe.py` | Runs the real sandbox + OpenCode against a scripted fake LLM; records request bodies and events |
| `resume_test.sh` | Proof that an OpenCode session survives a kill and can be continued in the sandbox |
| `heatguard.sh`, `heatguard.conf.example` | Optional thermal guard for the inference boards of a run (see Heat guard) |
| `worker-skills/hdl-lessons/SKILL.md` | Master copy of the worker lessons (curated; goes into every worker's AGENTS.md) |
| `test_*.py` | Offline regression tests (fake LLM, private lock directories, no board, no Gowin) |

## Run it

Preflight: the llama.cpp server must be up (`curl -s http://$BAKEOFF_INFER_HOST:8080/models`), and nothing else heavy
should be running on the desktop (`ls -d /tmp/hdl_heavy.lock*`).

```bash
python3 steer/steer_agent.py --run-id <name> --engine mailbox --tasks <t1,t2,...> --samples 1 \
    --provide-tests --auto-done --auto-first-steer --timeout 3600 --max-steers 6 --watchdog 600 --mailbox-timeout 900
```

- `--engine mailbox`: a Claude session answers each request.
- `--engine cli-sonnet` / `cli-opus`: unattended, needs a logged-in `claude` CLI.
- `--engine stub`: fixed rules, no model (dry runs).
- `--run-id <name> --kill-sid <opencode session id>`: kill one worker by its session id (pid and process group are in
  `workers.jsonl`).
- `--tasks <task>@s<n>`: one section of a section task (see Task types).
- Resumable: rerunning the same `--run-id` skips sessions already graded.
- Progress is judged by actions and results (edits, self-check runs and mismatch counts), never by reply length: long
  thinking that ends in a correct edit is real work. When a reply hits the output cap (step finish reason `length`) the
  engine is told so at the turn end. `--runaway-tokens N` (default 0 = off) is for experiments only.

## What the worker is told (worker rules)

OpenCode runs on the desktop (`/opt/OpenCode/resources/opencode-cli`, tested with v2.0.18); the inference server only
serves the model. Measured with `oc_probe.py`:
- The worker's system prompt includes `~/.config/opencode/AGENTS.md` and the workspace `/srv/work/AGENTS.md`. The
  config key `instructions` is not read by 2.0.18. Skills under `~/.config/opencode/skills` are listed
  (`<available_skills>`), but gpt-oss never called the `skill` tool in our runs, so a skill alone is never loaded.
- So the harness appends the worker rules to the workspace AGENTS.md: the whole-file write rule (below) and the
  curated lessons from `steer/worker-skills/hdl-lessons/SKILL.md` (frontmatter stripped). `--no-lessons` drops them.
  Edit the master copy in the repo; `~/.config/opencode/skills/hdl-lessons` is not what the worker sees.

**Whole-file writes (default on).** The worker must not edit the graded file with edit/patch; it rewrites it with one
write of the whole file (edits fail on repeated text and leave duplicated blocks). OpenCode 2.0.18 cannot enforce this
in config: write, edit and patch all ask permission action `edit`, so denying edit on the file also blocks write. The
harness therefore (1) puts the rule in AGENTS.md and TASK.md, (2) detects edit/patch calls on the graded file in the
event stream and sends a one-line note ("use write with the whole file") with the next message, or alone at the turn
end, (3) after the first failed edit of the graded file in a turn, or the first blocked one (an edit after which the
pre-gate reported a declaration in an unnamed block, a lint error, a changed header or a broken section file), the note
becomes "write the whole file with Write now", and every further edit call on the graded file in that turn is
rejected: the harness stops the turn, restores the file to its state before that edit (from its own copy, kept from the
write contents and the edit arguments in the event stream) and redirects with the same note (`edit_rejected`).
OpenCode cannot refuse a call before it runs, so a rejected edit is undone after the fact. The lock ends with the turn.
`--edit-redirect` (3 failed edits in one turn) remains as a backstop. Notes do not count against `max_steers` and are
shown to the engine as harness notes. `--allow-edit` (or `"write_whole": false` on a task or project job) restores the
old behaviour.

## Harness enforcement (pre-gate) and mistake counters

Lessons are placed strongest first: harness enforcement, then task/spec-writing rules, then steering rules, then
worker rules. The harness enforces, each with a one-line note riding on the next message to the worker:
- every successful write of the graded file (Verilog) is checked at once: reg/wire/integer declarations inside an
  unnamed begin/end block (`block_declarations`; from the second such write in a session the note quotes the
  module-level declaration rule of hdl-lessons and lists the offending lines), then the first Verilator lint error, then
  (lint-clean files of a `lint_wall` task) the first -Wall warning on the worker's lines (`lint_wall_warnings`;
  UNUSEDPARAM keeps its own note), and the file checks of `lib/hygiene.py` (`missing_header`, `nettype_leak`,
  `non_ascii`, `initial_blocks`, `dead_code`, `debug_left`), one note per kind (see Lint -Wall and file checks below);
- a write of any other new file in the workspace, when the task provides its own self-check, is deleted
  (`side_files`); files present at the start are never touched;
- write guard (`write_guard`, default on, `--no-write-guard` turns it off; Verilog and SystemVerilog, not testbench
  tasks): a write of the graded file, or the whole-file result of an edit of it, that drops the `module <top>` /
  `endmodule` pair, or that shrinks the file to less than half the size of the previous version when that version
  compiled (the pre-gate's Verilator lint of it had no %Error; unknown = the size rule is off, so the untouched start
  file never arms it), is undone at once as the edit lock undoes an edit: the turn is stopped, the harness's copy
  written back, and an uncounted redirect carries a one-line note naming the reason, the restored size, the best
  snapshot (`git show best:<file>`: the workspace is a git repository and `best` is the tag `checkpoint` moves) and the
  rule. The undone write never reaches the harness's copy or the pre-gate. In section-file mode the file is
  `section_s<n>.v`: it must keep its marker line, keep some code after it once it has any, and the same size rule
  applies. A file that fails the structural test is never ranked as `best`, and at the end of a session the candidate
  is taken from the harness's copy (else the best snapshot) when the worker's last file fails it
  (`write_guard_candidate` in results.jsonl). Writes through the shell (`> file`, `sed -i`) are not seen;
- a repeated first failure (`repeat_first_fail`, `RepeatFailWatch`): the self-check's first failure (its cycle and
  first wrong output; the sample point, values and counts are not compared) is the same on two runs in a row and the
  graded file's code (comments and whitespace ignored) did not change between them, or the same first failure comes
  back after a side file was written, or a side file is written after the first failure repeated. The harness sends
  one note (an uncounted redirect while the worker runs, which stops the turn; else with the next message): the exact
  FAIL lines, the wrong output with expected / got and the wrong bits (XOR), up to 4 lines of TASK.md / spec.md (and
  the read-only context in section-file mode) naming that output (rule lines first, port declarations last), and "fix
  it in <file> now with one Write call holding the whole file". Once per distinct first failure; runs whose output
  shows no simulation result keep the chain, a PASS ends it. The loop detector and the watchdog are unchanged;
- a turn that ends without a self-check run after the last change of the graded file: the harness runs
  `run_selfcheck.sh` itself and either declares the pass (auto-done) or sends the first FAIL lines
  (`turn_end_no_selfcheck`; replaces the auto-first-steer when tests are provided). `--no-pregate`,
  `--no-harness-selfcheck` turn these off;
- auto-done does not trust the self-check alone: before it declares done, the harness runs the grader's checks after
  the simulation (`lib/hygiene.grade_checks`: lint_wall, hygiene, pinned) on the graded file itself, with the
  task.json the grader will read (the job directory's, read at that moment). If they fail, the PASS evidence is dropped
  and the first problem goes to the worker as a harness note (`done_gate_blocked`); when the workspace's
  `hygiene_check.py` no longer matches task.json it is reinstalled. After `--done-gate-max` (3) such notes in a task
  the engine decides at the turn end, with the problem in its trigger.

Also enforced: a reply that contains code but no write of the graded file since the last message
(`reply_code_no_write`); edit search text with the Read tool's `NNN: ` line prefixes (`edit_line_prefix`); a module
header (name, parameters, ports) that differs from the given one after a write (`header_violations`; such a file is
never ranked); a constant the logic never uses (Verilator UNUSEDPARAM, `unused_params`); a queued steer goes out at
once when the worker is in an error loop (`--error-loop` failing tool calls in a row). Refusals ("cannot complete" and
the like) are counted (`refusals`).

A steer the engine gives while the worker is mid-turn is queued for the turn end; after `--steer-queue-max` seconds
(default 120) without a turn end it goes out as a redirect (`--steer-queue-max 0` = the old `--queue-max-s` limit,
default 480). A steer that replaces a queued one, or a coordinator message appended to it, keeps the first one's queue
time. Every delivery logs its wait: the decision row and results.jsonl `queued_steer_waits` [{waited_s, limit_s,
delivered: redirect | turn_end | error_loop}].

"Best" is ranked by the first failing cycle (later is better), then by mismatches; a coordinator retry starts from the
previous attempt's best snapshot. The grader skips synthesis after a failed simulation, writes the simulation verdict
before synthesis (`STEER_GRADE_PROGRESS`), and bounds the synthesis lane wait below the grading timeout
(`HEAVY_WAIT_S`). A simulation pass whose synthesis found no lane in time (or whose grade timed out during synthesis)
is SYN_PENDING, neither a FAIL nor a harness error (see Synthesis pending below). A harness ERROR does not count as a
coordinator attempt (`--max-harness-errors`, default 2). One steering job per inference server across coordinators
(`/tmp/steer_board_<name>.job` with an owner file naming the pid; a lock whose pid is gone is taken over;
`STEER_BOARD_LOCK_DIR` moves them). With `--lane` the task's wall-clock budget starts after the lane is taken. Tests:
`steer/test_harness_rules.py`.

results.jsonl `mistakes` per session: `failed_edits`, `turn_end_no_selfcheck`, `block_declarations`, `lint_errors`,
`side_files`, `peg_native`, `refusals`, `reply_code_no_write`, `edit_line_prefix`, `header_violations`, `unused_params`,
`context_writes`, `section_file_errors`, `out_of_section_edits` (section jobs, see Section-file mode), `steers`,
`lint_wall_warnings`, `missing_header`, `nettype_leak`, `non_ascii`, `initial_blocks`, `dead_code`, `debug_left`
(writes that had them), `edit_rejected`, `done_gate_blocked`, `repeat_first_fail`, `write_guard`; `lessons_version` =
the commit and blob of the hdl-lessons text the worker saw (also stamped in its AGENTS.md), so a lesson's effect can be
compared before and after. Each run writes `mistakes_summary.{json,md}` (per session and the run total).

## Self-check output

On a failure the provided self-check prints the FIRST mismatch only: the cycle, every input, expected / got / wrong
bits for each differing output, the outputs that match, and any listed internal state (task.json `selfcheck_state`:
`[{name, expr, width, compare}]`, Verilog). Then one line per failing output (mismatch count, first cycle), the
passing outputs, and `FAIL: N mismatches`. About 20 lines; every line survives hdl-check's filter. For Verilog
modules the output then passes through `hygiene_check.py --merge` (below): its problems are printed as FAIL lines before
the RESULT line, and a run whose simulation passed but has problems ends `FAIL: N lint/file problems (everything else
passed)` and `RESULT: FAIL (lint/file checks; everything else passed)`. Such a run ranks below a clean pass and above
every simulation failure (fewer problems is better); it is never pass evidence. The engine prompt carries these FAIL
lines. The grader's MISMATCH detail carries the inputs at the first mismatch.

## Task types

| Type | The worker writes | Graded by | Example |
|---|---|---|---|
| rebuild | the module body | equivalence with a golden (tb_equiv) | `tasks/v_rebuild_ce_frac` |
| rebuild + `sections` | ONE section of a skeleton | that section spliced into the golden, equivalence incl. the section's registers | `tasks/v_section_pwm` |
| bugfix / refactor | a fix / a restructure | equivalence with the golden | |
| testbench | a testbench | golden must pass, mutants must fail | |
| kat | the module | a known-answer checker: a supplied bench, or an external reference in lockstep; no golden needed | `tasks/kat_debounce` |

The rebuild and section examples take their goldens from `src/examples/`; the kat example keeps its reference and
bench in `tasks/kat_debounce/source/` (all MIT). Rebuild a task with `python3 lib/build_task.py tasks/<id>`.

**Section jobs.** The golden has `// SECTION n: <name>` ... `// END SECTION n` markers (the open form without END
lines also parses: a section then runs to the next marker or endmodule). task.json `sections: [{n, name, drives:
[{name, width}]}]`; every register and wire is declared at module level, outside the sections. `lib/build_task.py`
makes `start/s<n>.v` (the golden with only section n empty), `skeleton.v` (all empty) and section mutants
(`"section": n`, checked to be confined to that section). A job is `--tasks <task>@s<n>` or `{"task": ..., "section":
n}` in project.json; it is graded with `grade.py --section=n`; the coordinator splices each merged section into the
project's file. `lib/validate.py`: per section, the golden passes, the empty section fails, every confined mutant
fails. Verilog only.

**Section-file mode (default on for section and kat-sections jobs; `--section-file off`, or `"section_file": false` on
a task or project job, restores the old mode).** The worker's file is `section_s<n>.v`: the section's marker line, then
the section's current body (the start file's, or the project's merged one; spec comments included). The rest of the
module is `context/<file>`, read-only, with that section replaced by `// <<< your section goes here, see
section_s<n>.v >>>`; there is no full module file at the workspace root. `section_splice.py` (read-only, generated from
`lib/sections.py`) writes the assembled module to `.assembled/<file>` (git-excluded); `run_selfcheck.sh` runs it first,
then the task's check on the assembled module, and maps line numbers back to `section_s<n>.v:<line>` or
`context/<file>:<line>`. The pre-gate lints the same assembly (from the harness's own copy of the context in
`.git/steer_context_<file>`; a write to `context/` is undone with a note, `context_writes`). The header rule is the
first line: it must stay the marker line (`header_violations`; such a snapshot is never ranked). A section file holding
a module header, `endmodule` or another SECTION marker is refused by the splice (`section_file_errors`). Write-whole
applies to the small file. At the end the harness splices the section file into its context copy: that full module is
the candidate (`cand/<tag>.v`; the raw section file is `cand/<tag>.section.v`), so `grade.py`, the coordinator merge,
holds and the integration gate are unchanged; the best snapshot is a full module too. results.jsonl `section_file`.

**Old (whole-file) mode** still assembles: the start file is kept as `.section_base/<file>`, and the self-check and the
pre-gate take ONLY SECTION n from the worker's full file into it, exactly as the grader does. Changes elsewhere are
ignored and reported once per write with "(harness) Only lines inside SECTION n count; your changes to SECTION m are
ignored." (`out_of_section_edits`; the self-check prints the same as a `NOTE:`). Tests: `steer/test_section_file.py`.

**kat tasks** (`lib/gen_kat.py`): task.json `type: kat`, `start` (or the header of `reference`), `spec.md` or
`spec_from` extracts, and `kat`:
- `mode: testbench`: `tb`, `tb_top`, `support_files` (compiled with the candidate), `pass_regex`, `timeout`;
  `derived_checks` prove a support file is a documented one-edit copy of another. Sources can be pinned as
  `git:<repo>@<rev>:<path>`; they are copied into the task. The bench is the worker's self-check (`kat_check.sh`: first
  failing lines plus a count per kind, not the bench's flood).
- `mode: reference`: `ref_files`, `ref_top`, `ref_inputs` (reference input -> expression), `ref_outputs`,
  `compare` [{name, cand: c_<port>, ref: r_<port>, mask}], `enable`, `extra_checks`, and clocks/init/decls/stim/
  cycles/seeds as in `equiv`. A lockstep bench is generated (`kat_ref_tb.v`).
- optional `reference` (an implementation of your own): golden/, replacement `mutants`, a recorded self-check.
- validation: licence checks, the start file must FAIL at the checker stage, a reference must PASS, every mutant (and
  every file of an external `mutants_dir`) must FAIL. Without a reference the task is valid but reported
  CHECKER UNPROVEN: the first passing module is the first proof of the checker; then add it as `reference` and turn
  `mutant_notes` into replacement mutants against it.
- **Licence rule for private references** (`"private": true`, e.g. a third-party netlist you may not redistribute):
  paths must be absolute and resolve outside this repository (checked), never copied into tasks/, src/ or a workspace,
  never named in prompt.md/TASK.md, compiled only in a work directory outside the repository
  (`$STEER_PRIVATE_SCRATCH`, default `/tmp/steer_private_grade`); the task directory is scanned for byte copies.
  Mutants derived from such a reference go in an external `mutants_dir`. Self-check vectors are recorded only from a
  non-private `reference`.
- A kat build never removes `golden/` or `mutants/`: they may hold promoted references (a worker-written passing module,
  hand-made mutants); only files generated from task.json `reference` (golden and `m<N>.v` mutants) are rewritten.

**kat sections** (`lib/kat_sections.py`; section jobs with NO reference, e.g. the instance and wiring sections of a
top-level wrapper): task.json `type: kat`, `kat.mode: sections`, `skeleton` (pinned source), `tree` ({repo, rev, paths,
submodules}: materialized once per rev under `$STEER_TREE_CACHE`, default `~/.cache/steer-bc250`), `sections_spec`
(per section: instances with port -> net `connects`, `assigns`, `must_drive`, `must_use`, `after`),
`worker_sections`, `lint` (top, incdirs, defines, flags). The candidate is the skeleton plus every section already
merged in the project plus this job's section (`section_base.v` in the job directory). Grading in layers:
1. always: Verilator lint of the assembled file against ports-only stubs of every module it instantiates (stubs from
   the real sources in the tree; `ifdef`s resolved with the lint defines), and the section's connectivity
   (instances, connections, assigns, used nets; UNDRIVEN / UNUSED on must-drive / must-use nets; a partial select
   of a bus, Verilator's "Bits of signal are not used", counts as a use);
2. optional: `kat.checkers["<n>"]` command on the assembled file;
3. `kat.integration` ({cmd with `{cand}`, pass_regex, section_hints, claude_regex, at_section}): with `at_section`
   (the last section) the gate runs as part of that section's grading, on the full assembly, always after layers 1-2;
   otherwise the coordinator runs it once every worker section is merged. It runs through the queue runner (below)
   with `--kind sim --mem 6G --desktop-only` in an overlay of the pinned tree (outside every git work tree) with the
   project's merged modules in `kat.modules_dir` (default `rtl`). A gate FAIL is held for the integrating session
   (`results/proj_<id>/hold/<job>.md`, not counted as an attempt) when it is the task's first failure on the full
   assembly (`hold_first_fail`) or matches `claude_regex` (default `INTEGRATION CONTRACT`: a `$fatal` in code the
   integrator owns); release it with `hold/<job>.release` = `retry`, `reopen <job>` or `fail`. A run without a verdict
   line is a harness error. The coordinator keeps running while a job is held. The lane wait is bounded
   (`kat.integration.wait_budget`, default 1800 s): past it the queued ticket is cancelled, the process group killed
   and the overlay removed (a harness result). A task with a long gate carries its own `grade_timeout`, which
   steer_agent and the coordinator use instead of `--grade-timeout`. The per-section module checker (sections.json
   `kat_cmd`) runs on the project's merged module (copied to `<job dir>/modules/`) or the tree's
   `<modules_dir>/<m>.v`; while neither exists it is reported as pending (`kat.checker_required` makes it a failure).
The worker's self-check (`run_selfcheck.sh` -> `section_check.py`) is layer 1 in the workspace (stubs, includes,
the section's spec). Jobs wait for the sections their spec lists under `after`. Validation: every empty worker
section fails layer 1; the integration command (when not a placeholder) fails the empty skeleton; no mutants without
a reference. No example is included (it needs a pinned source tree of your own).

## Lint -Wall, file checks and pinned names (lib/hygiene.py)

The checks a repository's own gates apply beyond simulation: a module that passes simulation can still be refused for
-Wall warnings, or for missing register names that other benches read. `grade.py` runs them on Verilog modules
(rebuild, bugfix, refactor, kat, section and kat-section jobs; not testbench or VHDL tasks) after the simulation or
known-answer run passes and before synthesis, so a dirty candidate is never SYN_PENDING. Stages, in order:
- `lint_wall`: `verilator --lint-only -Wall` gives 0 warnings in the candidate; a section job counts only lines inside
  its section (kat sections: on the assembled module, with the task's own lint flags). DECLFILENAME never counts.
  Warnings the file waives with a `/* verilator lint_off RULE */` ... `/* verilator lint_on RULE */` pair (or the `//`
  form) are not reported; a `lint_off` naming no rule, or without its `lint_on`, fails. task.json `"lint_wall": false`
  turns it off (default true; `build_task.py` writes it into new tasks). Set false only for a task whose golden fails it.
- `hygiene`: an `SPDX-License-Identifier` line and a copyright line in the first 5 lines (each only when the golden or
  start file has one); a file that sets `` `default_nettype none `` ends with `` `default_nettype wire `` after
  `endmodule`; no non-ASCII character; no `initial` block (some synthesis flows drop them) unless task.json
  `"allow_initial": true`; no dead code (`dead_code`): an unused-bits sink (`wire`/`logic`/`assign` of a name
  containing `unused` from a `&`/`|`/`^` reduction of a concatenation) holding only constants, e.g.
  `wire unused = &{1'b0, 28'd0};`, or an empty `begin end` block (`if (unused) begin end`, an empty case item; an
  empty case default is written `default: ;`), unless task.json `"allow_dead_code": true`; no debug leftovers
  (`debug_left`): `$display`, `$write`, `$monitor` (and their b/h/o forms) or `$finish` in the code (comments and strings
  ignored, `$writememh` and `$fatal` are not matched) unless task.json `"allow_display": true`; and, when task.json
  `"lint_waivers": ["RULE", ...]` is set, a `verilator lint_off` for a rule outside that list (an empty list allows no
  waiver; without the key, any named, paired lint_off is allowed). In a section job non-ASCII, `initial`, dead code and
  debug leftovers count only inside the section.
- `pinned`: task.json `"pinned_names": [{"name": "acc", "width": 18}, ...]`: each signal exists at module scope with
  that width (benches read them by hierarchical name), probed with iverilog as `$bits(dut.<name>)` with the module's
  default parameters (or `"pinned_params"`). Failure: "internal signal acc [17:0] required by the spec is missing or has
  the wrong width".
The grade JSON carries `lint_wall` ({checked, warnings, first, waiver_problems}), `hygiene` (count per kind) and
`pinned` (problems) for every graded candidate that reached them. `lint_warnings_raw` is informational and never
graded: every `%Warning` line of the first Verilator -Wall lint on the candidate file, DECLFILENAME included, so an
equivalence candidate (linted as `candidate.v`) counts 1 even when clean, a kat candidate (linted under the golden's
file name) 0, and a section job counts lines outside its section too (VHDL: `ghdl -s` warnings; testbench tasks:
warnings in `candidate_tb.v`). The graded count is `lint_wall.warnings`.

The worker sees the same checks: `run_agent.install_hygiene` writes a read-only `hygiene_check.py` (the CORE functions of
`lib/hygiene.py` and the task's settings) into the workspace, `run_selfcheck.sh` pipes the self-check through it (in a
section job before `section_splice.py --map`, so line numbers point at `section_s<n>.v`), and TASK.md lists the rules.
`build_task.py` adds the same rules to new tasks' prompts: a sentence in rebuild prompts, and in kat and kat-section
prompts a "Repository rules (checked)" paragraph generated by `hygiene.repository_rules` from the task's flags.

`validate.py` grades the golden through these stages (a golden that fails them makes the task invalid); `--no-gate`
skips a kat-sections task's integration gate (also `grade.py --no-gate`). `validate_selfcheck.py` runs
`run_selfcheck.sh`, so it checks the merged output too.

## DSP resources (stage `resources`)

`lib/gowin_syn.sh` reads GowinSynthesis's `*_syn_rsc.xml` (the top module's `T_<primitive>` totals) into the synthesis
summary: `resources` = {`dsp` (MULT*, ALU54*, PADD* primitives summed), `bsram`, `prims` (every non-logic primitive by
name)}. `validate.py` grades the golden with `--no-resources` (record only), so `validation.json` keeps the golden's
counts under `golden.syn.resources` whenever validate runs synthesis. After a passing synthesis (full grade and
`--syn-only`), `grade.py` fails at stage `resources` when the candidate uses more DSP blocks than the golden's recorded
count, or than task.json `"max_mult": N` when set; `"allow_more_dsp": true` records the counts only. No recorded golden
count and no `max_mult`: recorded, not checked. BSRAM is recorded, not gated. The grade JSON carries `resources`
({candidate, golden, dsp_limit, limit_from, checked}). The limit is in the worker's TASK.md rules. The coordinator
treats a `resources` failure like the other post-simulation stages (an attempt, resumed from the candidate). Why: a
simulation whose stimulus drives only the real cadence cannot tell one shared multiplier from one per slot.

## Synthesis pending (SYN_PENDING) and the queue runner

When the hidden simulation passes but no heavy lane frees for Gowin synthesis within the grading time, `grade.py`
returns stage `syn_pending` (`pass` null, `sim_pass` true, exit code 3). steer_agent records status `SYN_PENDING`: the
worker is done (it is stopped before grading), the candidate is kept as the job's candidate and best snapshot
(`cand/<tag>.best.<ext>`), and `syn_finish_cmd` is the command that finishes the grade. A grade that times out while
the progress file says `syn_running` is SYN_PENDING too. run_agent.py and run_api.py count it as ERROR.

The coordinator never restarts such a job. It marks it `syn_pending`, frees its server at once (the job thread ends,
so the board lock goes and the next job can start), and queues the synthesis without a deadline:
`grade.sh <candidate> --syn-only --who="steer <project>"` (plus `--section=n` and `--grade-args`). `--syn-only` prepares
the candidate exactly as the full grade does, skips lint and simulation, and runs only the task's synthesis through the
queue runner: the inputs, `gowin_syn.sh` and `heavy.sh` are staged in a small committed git repository and
`run_syn.sh` prints `STEER_SYN_RESULT <json>`. Inside the lane, `HEAVY_LANE_HELD=1` keeps `lib/heavy.sh` from taking a
second lane. Outcomes (a second results.jsonl row with `syn_deferred: true`):
- synthesis PASS: the grade is complete (PASS), the job is merged as usual, and the project log says so;
- synthesis FAIL (Gowin ERROR lines): a normal failed attempt; the retry starts from the simulation-passing candidate
  with the synthesis error as its feedback;
- no verdict (queue or lane failure, no Gowin error): queued again twice, then a harness ERROR.
The coordinator does not finish while a synthesis is queued. On SIGTERM it stops the queued synthesis (the queue runner
withdraws its ticket) and writes `STOPPED with job <id> SYN_PENDING` with the candidate path and the exact `--syn-only`
command to the project log, plus a results row with `stopped: true`; run that command by hand and merge on PASS.

**The queue runner** is `lib/queue_run.sh` by default: a first-come-first-served queue on this machine (tickets in
`$HEAVY_QUEUE_DIR`, default `/tmp/hdl-queue`) over the same lane directories as `lib/heavy.sh`. `STEER_HEAVY_RUN=<path>`
(or `kat.integration.heavy_run` for a gate) replaces it, e.g. with a runner that sends jobs to other machines. A
replacement must accept `[--who <name>] [--kind gowin|sim] [--mem <size>] [--timeout <s>] [--desktop-only]
[--env NAME=VALUE]... -- <cmd...>` and `--cancel <ticket> [--who <name>]`, print `[queue] q<digits> ...` when it
queues a job and a line containing `dispatched to <where> (` when the job starts, run the command in the current
directory with the --env values, and exit with the job's exit code (3 for a cancelled ticket). `--desktop-only` is
passed for tasks with private paths, task.json `"syn_desktop_only": true`, or `STEER_SYN_DESKTOP_ONLY=1`.

## Lessons loop (review before promotion)

When a job passes the hidden grader after at least one counted steer, the steers and harness notes that preceded the
pass, with the self-check mismatch trajectory, are appended to `results/<run>/lessons_candidates.{jsonl,md}`. The
harness never edits the lessons. After a run, review the candidates: a steer that states a rule another module would
need becomes ONE short rule in `steer/worker-skills/hdl-lessons/SKILL.md` (keep its sections and style), in its own
commit; one-off fixes stay in the candidates file.

## Metrics

results.jsonl adds `harness_notes`, `edit_attempts_on_graded`, `peg_native_errors` (gpt-oss tool-format failures:
"does not match the expected peg-native format"; `--peg-stall` (2) of them since the last decision consult the
engine), `write_whole`, `lessons`, `section`, `section_file`.

## Dry runs without a board

`python3 steer/oc_probe.py --serve-tcp <port> --out <dir> --script steps.json` serves a scripted fake LLM; then run
`BAKEOFF_INFER_HOST=127.0.0.1 BAKEOFF_INFER_PORT=<port> python3 steer/steer_agent.py ... --engine stub
--grade-args=--no-syn` (or coordinator.py with `--boards a=127.0.0.1:<p1>,b=127.0.0.1:<p2>`). `STEER_BC250_BASE`
points the harness, graders and task loader at another checkout. `validate.py --no-syn` skips Gowin synthesis.

## Heavy lanes

A steering task does NOT hold a heavy lane (`/tmp/hdl_heavy.lock*`): workers and iverilog self-checks are light, and
grading takes its own lane only for Gowin synthesis (`lib/heavy.sh`). `--lane` restores a task-long lane: then the
steerer logs "waiting for a desktop heavy lane (all N held: <owners>)" at once and every 3 minutes, writes an owner
file, and releases the lane on exit, SIGTERM and SIGINT (the coordinator also kills every worker and frees every lane
its job threads hold).

## Tests

All offline: a scripted fake LLM (`oc_probe.py`), the stub engine, private lock directories, a fake `gw_sh`; no board.
They need iverilog, verilator, bwrap and the OpenCode CLI, and use the example tasks.

```bash
export STEER_HEAVY_LOCKS=/tmp/t_lane1,/tmp/t_lane2 STEER_BOARD_LOCK_DIR=/tmp/t_boards HEAVY_QUEUE_DIR=/tmp/t_queue
mkdir -p /tmp/t_boards
python3 steer/test_units.py           # coordinator rules, grader stages, self-check, heat guard, write guard
python3 steer/test_harness_rules.py   # pre-gate counters, SYN_PENDING, done gate, edit lock, repeat note, steer queue
python3 steer/test_section_file.py    # section-file mode: workspace, self-check, merge, steered sessions
python3 steer/test_launch.py          # a coordinator job of each kind starts its worker while every lane is busy
python3 steer/test_lane_release.py    # --lane: waits, owner file, release on SIGTERM
```

Tests and repros never use the real lanes: `STEER_HEAVY_LOCKS=<dir1>,<dir2>` points the steerer, `lib/heavy.sh` and
`lib/queue_run.sh` at private lock directories, `HEAVY_QUEUE_DIR` the queue, `STEER_BOARD_LOCK_DIR` the board locks,
and `GOWIN_SH` replaces gw_sh with a fake (see `syn_env` in `steer/test_harness_rules.py`).

## Answering the mailbox (Claude side)

```bash
python3 steer/mailbox.py watch results/steer_<run-id>          # one line per new request (use with Monitor)
python3 steer/mailbox.py show  <req.json>                      # the ruleset and the request prompt
python3 steer/mailbox.py reply <req.json> '{"action": "...", "message": "...", "reasoning": "...", "evidence": "..."}'
```

For a reproducible number, relay each request's `system` and `prompt` to the engine unedited and write its answer back
unedited. `mailbox.py reply` refuses a steer of more than 15 code lines or one holding a whole module, so the engine
points at the fix instead of writing the module for the worker; `--allow-big` accepts it and marks the decision
`big_steer`.

## Projects (coordinator)

`steer/coordinator.py` runs ONE project goal on one or more inference servers: one job per server at a time, each job
steered like a single task. Jobs come from existing task directories (`projects/<name>/project.json`: name, goal, jobs
[{id, task, summary, section?, max_steers?}]; a job depends on the jobs whose files its task compiles with).
- Launch: `python3 steer/coordinator.py --project projects/<name> --run-id <id> --boards a=host1:8080,b=host2:8080
  --engine mailbox --provide-tests --auto-done --auto-first-steer --timeout 3600 --max-steers 12 --watchdog 600
  --mailbox-timeout 900 --max-attempts 2 > results/proj_<id>.log 2>&1`. `--boards` defaults to `$STEER_BOARDS`, else
  `board1=localhost:8080`.
- Answer the mailbox exactly as for single tasks, on `results/proj_<id>` (several workers can have requests at once).
- How it coordinates: a job that compiles with another job's file gets a STUB of it until that job passes; a passing
  job is merged into `results/proj_<id>/repo` and the real file is handed to the dependent workers with an inbox
  message; `PROJECT_STATUS.md` is kept current in every workspace; at the end the integration is graded on a hidden
  seed with the team's own dependencies (`project_result.json`).
- In our runs gpt-oss-20b handled modules up to about 200 lines with a known-answer test, and refused or stalled on a
  700-line module. Split big work into small jobs. A job can carry its own steer budget: `"max_steers": N` on the job
  in `project.json` (or in a task's config) overrides `--max-steers`.
- Task config per attempt: every job and attempt start copies the task directory into `jobs/<job>/` and reads the
  job's config from that copy's task.json and prompt.md, the files its `grade.sh` reads, so a task changed while the
  project runs takes effect at the next attempt for the workspace and the grader alike (single runs re-read each task
  before each session). Right after the workspace is built, `run_task` compares the lint/file-check settings of the
  config and of the workspace's `hygiene_check.py` with the job directory's task.json and stops with a harness ERROR
  (not a worker attempt) when they differ.
- Retry feedback: a failed attempt's grade stage and detail become the retry's first message, `(harness) This is a
  retry. On your previous attempt the hidden grade failed at <stage>: <detail>`, and a `## Previous attempt` section
  at the end of TASK.md. A failure at lint_wall, hygiene or pinned (the simulation passed) resumes from that attempt's
  candidate; otherwise from its best snapshot.
- What the engine sees: events, all its own earlier steers (up to 6, so it keeps one plan), and facts about the graded
  file (lines, still the untouched start file or not, +/- lines since its last decision, repeated 4-line blocks, a short
  diff). The untouched start file is never tagged `best`.

## Heat guard (optional)

`--heatguard` on steer_agent.py or coordinator.py starts `steer/heatguard.sh <its stdout log> <its own pgid> <results
dir>` in a session of its own, writes its output to `results/<run dir>.heat.log`, and stops it when the run exits. By
hand: `steer/heatguard.sh <run log> <run pgid> <results dir> &`, where the pgid is the Python run's own group
(`ps -o pgid= -p <python pid>`). The guard reads each board's temperatures every 10 s through `read_temps`; GPU edge
>= 79 C or Tctl >= 92 C lowers that board's GPU cap one step through `set_cap`; 3 dangerous readings in a row (edge >=
85 / Tctl >= 97, or still edge >= 82 / Tctl >= 95 at the lowest step) stop the run and every worker and unload the
model. It exits when the run log ends with "done" or the run's process group is gone. The boards, steps, URLs, the
model and the two functions come from `steer/heatguard.conf` (copy `heatguard.conf.example`; `HEATGUARD_CONF` points
elsewhere); without it the guard refuses to start. A pgid that is not a number > 1 is refused (exit 2).

## Outputs

`results/steer_<run-id>/`: `results.jsonl` (grader verdict per session), `decisions/`, `events/`, `readout/`,
`goalpacks/`, `mailbox/`, `workers.jsonl`, `mistakes_summary.{json,md}`, `lessons_candidates.{jsonl,md}`. Workspaces
are in `agent_ws/steer_<run-id>/` (large; safe to delete after a run).

## Known limits

- A kat task grades with the same bench the worker runs (no hidden variant yet); keep benches scenario-based.
- Writes through the shell are invisible to the pre-gate and the write guard.

## Dependencies outside this repo

| What | Where | Notes |
|---|---|---|
| OpenCode | `/opt/OpenCode/resources/opencode-cli` | v2.0.18. An app update can change `run --session`, the JSON events, where instructions load or the permission model; rerun `resume_test.sh` and `oc_probe.py --worker-rules --write-whole` after one |
| Worker config | `agent/opencode.sandbox.json` (in repo) | Bound into the sandbox as `OPENCODE_CONFIG`; points at the forwarder on 127.0.0.1:8080 |
| Desktop OpenCode config | `~/.config/opencode/` | Bound read-only into the sandbox |
| Inference server | llama.cpp `llama-server` | `BAKEOFF_INFER_HOST` / `BAKEOFF_INFER_PORT` (default localhost:8080) |
| Heavy lanes | `/tmp/hdl_heavy.lock*` | Shared with any other heavy jobs that use the same locks |
| Sandbox | `bwrap` | From apt |
