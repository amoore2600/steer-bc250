#!/usr/bin/env python3
"""fwd.py -- byte-stream forwarder between a TCP port and a unix socket.
  fwd.py tcp2unix <listen_host> <listen_port> <unix_path>
      inside the sandbox: 127.0.0.1:8080 -> socket, bytes copied unchanged.
  fwd.py unix2tcp <unix_path> <host> <port> [<log> [<sampling> [<max_tokens>]]]
      outside: socket -> the inference server.  With <log>, every HTTP request from the client is parsed (request line,
      headers, Content-Length body) and one JSON line per request is appended to <log> with its sampling fields as
      received and as sent.  <sampling> is "-" (unchanged), a number (temperature) or a JSON object of sampling
      fields; a JSON body that has a "messages" or "prompt" field gets those fields set (overriding the client's)
      before it is forwarded (Content-Length rewritten).  With <max_tokens>, such a
      body that carries no max_tokens / max_completion_tokens gets max_tokens = that value (the model's output limit).
"""
import asyncio, json, os, re, sys, time


async def copy(r, w):
    try:
        while True:
            b = await r.read(65536)
            if not b:
                break
            w.write(b)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


def sampling(j):
    return {k: j.get(k, "<absent>") for k in ("model", "temperature", "top_p", "top_k", "min_p", "presence_penalty",
                                               "frequency_penalty", "seed", "max_tokens", "stream")}


async def requests(r, w, log, temp, maxtok=None, marks=()):
    """Client -> server: forward request by request, optionally rewriting the JSON body."""
    buf = b""
    try:
        while True:
            b = await r.read(65536)
            if not b:
                break
            buf += b
            while True:
                h = buf.find(b"\r\n\r\n")
                if h < 0:
                    break
                head = buf[:h].decode("latin-1")
                if re.search(r"^transfer-encoding:\s*chunked", head, re.I | re.M):
                    w.write(buf)                         # not parsed: passed through unchanged
                    buf = b""
                    break
                m = re.search(r"^content-length:\s*(\d+)", head, re.I | re.M)
                n = int(m.group(1)) if m else 0
                if len(buf) < h + 4 + n:
                    break
                body = buf[h + 4:h + 4 + n]
                buf = buf[h + 4 + n:]
                rec = {"t": round(time.time(), 3), "request": head.splitlines()[0][:120]}
                if n:
                    try:
                        j = json.loads(body)
                        rec["received"] = sampling(j)
                        for mk in marks:
                            rec.setdefault("marker_in_body", {})[mk] = mk.encode() in body
                        if isinstance(j, dict) and ("messages" in j or "prompt" in j) and (temp is not None or maxtok):
                            if temp is not None:
                                j.update(temp)
                            if maxtok and "max_tokens" not in j and "max_completion_tokens" not in j:
                                j["max_tokens"] = maxtok
                            body = json.dumps(j).encode()
                            head = re.sub(r"(?im)^content-length:\s*\d+", "Content-Length: %d" % len(body), head)
                        rec["sent"] = sampling(j)
                    except ValueError:
                        pass
                w.write(head.encode("latin-1") + b"\r\n\r\n" + body)
                if log:
                    with open(log, "a") as f:
                        f.write(json.dumps(rec) + "\n")
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def main():
    mode = sys.argv[1]
    if mode == "tcp2unix":
        host, port, path = sys.argv[2], int(sys.argv[3]), sys.argv[4]

        async def handle(r, w):
            ur, uw = await asyncio.open_unix_connection(path)
            await asyncio.gather(copy(r, uw), copy(ur, w))
        srv = await asyncio.start_server(handle, host, port)
    else:
        path, host, port = sys.argv[2], sys.argv[3], int(sys.argv[4])
        log = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None
        temp = None
        if len(sys.argv) > 6 and sys.argv[6] != "-":
            temp = json.loads(sys.argv[6]) if sys.argv[6].lstrip().startswith("{") else {"temperature": float(sys.argv[6])}
        maxtok = int(sys.argv[7]) if len(sys.argv) > 7 and sys.argv[7] != "-" else None
        marks = tuple(sys.argv[8:])                        # substrings whose presence in each request body is logged
        if os.path.exists(path):
            os.unlink(path)

        async def handle(r, w):
            tr, tw = await asyncio.open_connection(host, port)
            if log or temp is not None:
                await asyncio.gather(requests(r, tw, log, temp, maxtok, marks), copy(tr, w))
            else:
                await asyncio.gather(copy(r, tw), copy(tr, w))
        srv = await asyncio.start_unix_server(handle, path)
    async with srv:
        await srv.serve_forever()


asyncio.run(main())
