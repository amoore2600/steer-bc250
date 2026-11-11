#!/usr/bin/env bash
# resume_test.sh -- prove that an OpenCode session started by `opencode run --standalone --format json` inside
# agent/sandbox.sh can be continued by a second `opencode run --session <id>` against the same OpenCode state dir,
# and that a session whose turn was killed mid-way can be continued too.  Uses the BC-250 router (gpt-oss).
set -u
B=~/steer-bc250; OC=/opt/OpenCode/resources/opencode-cli; M=${MODEL:-gpt-oss-20b-MXFP4}
T=$B/agent_ws/resume_test; rm -rf "$T"; mkdir -p "$T/work" "$T/oc"
SK=$B/agent_ws/sock/rt$$; rm -rf "$SK"; mkdir -p "$SK"
cp $B/agent/opencode.sandbox.json "$SK/opencode.json"
git -C "$T/work" init -q && echo "# scratch" > "$T/work/README.md"
python3 $B/agent/fwd.py unix2tcp "$SK/llm.sock" "${BAKEOFF_INFER_HOST:-localhost}" "${BAKEOFF_INFER_PORT:-8080}" "$T/requests.jsonl" - 16384 &
FWD=$!; trap 'kill $FWD 2>/dev/null' EXIT
for i in $(seq 50); do [ -S "$SK/llm.sock" ] && break; sleep 0.1; done
run() { # run <outfile> <timeout> <opencode args...>
  local out=$1 to=$2; shift 2
  timeout -k 5 "$to" $B/agent/sandbox.sh "$T/work" "$T/oc" "$SK" $OC run --standalone --auto --format json --model bc250/$M "$@" > "$out" 2> "$out.err"
  echo "exit=$? lines=$(wc -l < "$out")"
}
echo "### run1: plant a word"
run "$T/r1.jsonl" 300 "Remember the code word ZEBRA-7731. Reply with exactly: OK"
SID=$(grep -oE '"sessionID":"[^"]+"' "$T/r1.jsonl" | head -1 | cut -d'"' -f4); echo "sid=$SID"
grep -oE '"type":"text".*' "$T/r1.jsonl" | grep -oE '"text":"[^"]{0,120}' | tail -2
echo "### run2: continue the session, ask for the word"
run "$T/r2.jsonl" 300 --session "$SID" "What was the code word I asked you to remember? Reply with only the word."
SID2=$(grep -oE '"sessionID":"[^"]+"' "$T/r2.jsonl" | head -1 | cut -d'"' -f4); echo "sid2=$SID2 same=$([ "$SID" = "$SID2" ] && echo yes || echo NO)"
grep -oE '"type":"text".*' "$T/r2.jsonl" | grep -oE '"text":"[^"]{0,120}' | tail -2
echo "### request bodies seen by the forwarder: messages per request, and whether ZEBRA is in run2's prompt"
python3 - "$T/requests.jsonl" <<'PY'
import json,sys
for i,l in enumerate(open(sys.argv[1])):
    r=json.loads(l); b=r.get("body") or r.get("received_body") or {}
    msgs=b.get("messages") if isinstance(b,dict) else None
    print(i, "keys=",list(r.keys())[:8], "n_messages=", len(msgs) if msgs else None,
          "zebra_in_body=", "ZEBRA" in json.dumps(b))
PY
echo "### run3: start a long turn, kill it after the first tool call"
setsid timeout -k 5 300 $B/agent/sandbox.sh "$T/work" "$T/oc" "$SK" $OC run --standalone --auto --format json --model bc250/$M \
   "Create ten files f1.txt .. f10.txt one at a time with the write tool, each containing its own number, then reply DONE." > "$T/r3.jsonl" 2> "$T/r3.err" &
P3=$!   # setsid: run3 is its own process group, so the kill below reaches only this test's sandbox tree
for i in $(seq 600); do grep -q '"type":"tool_use"' "$T/r3.jsonl" 2>/dev/null && break; sleep 0.5; done
sleep 2; echo "tool_use seen after ~$((i/2))s; killing run3's process group $P3 only"
kill -TERM -- -$P3 2>/dev/null; sleep 2; kill -KILL -- -$P3 2>/dev/null; wait $P3 2>/dev/null; echo "r3 lines=$(wc -l < "$T/r3.jsonl")"
SID3=$(grep -oE '"sessionID":"[^"]+"' "$T/r3.jsonl" | head -1 | cut -d'"' -f4); echo "sid3=$SID3"; ls "$T/work"
echo "### run4: continue the killed session with new orders"
run "$T/r4.jsonl" 300 --session "$SID3" "Stop creating files. Tell me in one line how many of the ten files exist in the current directory now (use ls), then reply STOPPED."
grep -oE '"type":"(text|tool_use)".*' "$T/r4.jsonl" | grep -oE '("tool":"[a-z]+"|"text":"[^"]{0,160})' | tail -4
echo "### sessions on disk"; find "$T/oc" -name '*.json' -path '*session*' | head -5; find "$T/oc" -type d -name 'session*' | head -3
