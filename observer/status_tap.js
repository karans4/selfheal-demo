// Kernel-side observer for the healer, run by yeet. Reuses the TCX probe from the public
// github.com/yeet-src/httpinspect (cloned at run time, not vendored). For a few seconds it reads
// HTTP responses leaving PORT straight off the wire and prints one JSON line per segment:
//   {"port": 8098, "status": 500}          response head
//   {"port": 8098, "body": "{\"error\"..."} response body (sent in its own segment)
import { RingBuf } from "yeet:bpf";
import { control } from "./probes/probe.js";

const PORT = Number(globalThis.TAP_PORT ?? 0);
const RESP = /^HTTP\/\d\.\d (\d{3})/;
const seen = new Set();
await new RingBuf(control, "events").subscribe((raw) => {
  const ev = raw.http_event ?? raw;
  if (PORT && Number(ev.sport) !== PORT) return;
  const k = `${ev.sport}>${ev.dport}#${ev.seq}`;
  if (seen.has(k)) return;
  seen.add(k);
  const d = ev.data instanceof Uint8Array ? ev.data : Uint8Array.from(Object.values(ev.data));
  let t = "";
  for (let i = 0; i < Number(ev.captured); i++) { if (d[i] === 0) break; t += String.fromCharCode(d[i]); }
  const m = RESP.exec(t);
  if (m) {
    const body = t.split("\r\n\r\n")[1] || "";
    console.log(JSON.stringify(body ? { port: Number(ev.sport), status: Number(m[1]), body: body.slice(0, 300) }
                                    : { port: Number(ev.sport), status: Number(m[1]) }));
  } else if (t.trim()) {
    console.log(JSON.stringify({ port: Number(ev.sport), body: t.slice(0, 300) }));
  }
});
await new Promise((r) => setTimeout(r, 4000));
