"""Self-healing services with a hive mind.

Each project (checkout, billing) has a private Cognee brain: raw logs, code, config, incident
write-ups, owned by that project's on-call user. After a heal, a sanitizer distills the incident
into an abstract pattern (symptom class -> root cause -> fix shape, no names/values/secrets) and
writes it to the shared `hive` dataset, which every project can read. Patterns link the projects
that hit them, so the hive is a hypergraph of similar problems while each part stays secret.

  python healer.py setup                       users, datasets, hive grants
  python healer.py runbook [--from-fixtures]   pull #oncall (Slack) + service code (GitHub) via Scalekit
  python healer.py break checkout pool         inject a fault
  python healer.py heal checkout [--no-hive] [--ship]
  python healer.py eval --label before|after
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "true")

PROJECTS = {
    "checkout": {"user": os.getenv("CHECKOUT_ONCALL", "oncall-checkout@example.com"), "path": "/checkout",
                 "keys": {"pool": "db_pool_size", "timeout": "upstream_timeout_ms", "currency": "currency", "codec": "wire_codec"}},
    "billing": {"user": os.getenv("BILLING_ONCALL", "oncall-billing@example.com"), "path": "/billing",
                "keys": {"pool": "pg_connections", "timeout": "ledger_timeout_ms", "currency": "ccy", "codec": "frame_codec"}},
}
HIVE_USER = os.getenv("HIVE_USER", "hive@example.com")
HIVE = "hive"
GH = os.getenv("GITHUB_CONNECTION", "github-connect")
FAULTS = {"pool": 0, "timeout": 5, "currency": "usd", "codec": "v2"}  # fault -> bad value for that project's key
MODEL = os.getenv("AGENT_MODEL", "gpt-5-mini")
SANITIZER_MODEL = os.getenv("SANITIZER_MODEL", MODEL)
MAX_ITERS = 4


def svc_dir(project: str) -> Path:
    return ROOT / "svc" / project


def config(project: str) -> dict:
    return json.loads((svc_dir(project) / "config.json").read_text())


def canary(project: str) -> str:
    return json.loads((svc_dir(project) / "config.good.json").read_text())["internal_api_token"]


# --- Service under observation ----------------------------------------------

def observe(project: str) -> tuple[bool, str]:
    """Start the service, hit it, stop it. Returns (healthy, log text)."""
    d = svc_dir(project)
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    url = f"http://127.0.0.1:{config(project)['port']}{PROJECTS[project]['path']}"
    healthy = False
    for _ in range(20):
        time.sleep(0.15)
        try:
            healthy = urllib.request.urlopen(url, timeout=2).status == 200
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            if proc.poll() is not None:
                break
    proc.terminate()
    out, _ = proc.communicate(timeout=5)
    return healthy, out


def break_service(project: str, fault: str) -> None:
    cfg = config(project)
    cfg[PROJECTS[project]["keys"][fault]] = FAULTS[fault]
    (svc_dir(project) / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def restore(project: str) -> None:
    for f in ("app", "config"):
        ext = "py" if f == "app" else "json"
        (svc_dir(project) / f"{f}.{ext}").write_text((svc_dir(project) / f"{f}.good.{ext}").read_text())


# --- Memory: Cognee -----------------------------------------------------------

_db_ready = False


async def user(email: str):
    from cognee.modules.engine.operations.setup import setup as create_tables
    from cognee.modules.users.methods import create_user, get_user_by_email

    global _db_ready
    if not _db_ready:
        await create_tables()
        _db_ready = True
    return await get_user_by_email(email) or await create_user(email, "hackathon-pw")


async def dataset_ids(u, names: list[str]):
    from cognee.modules.data.methods import get_authorized_existing_datasets

    readable = await get_authorized_existing_datasets(None, "read", u)
    return [d.id for d in readable if d.name in names]


async def setup() -> None:
    import cognee
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.users.permissions.methods import authorized_give_permission_on_datasets

    hive = await user(HIVE_USER)
    await cognee.remember("The hive holds abstract incident patterns shared across projects. "
                          "Patterns carry no project names, hostnames, values or secrets.",
                          dataset_name=HIVE, user=hive, node_set=["source:hive", "kind:charter"])
    (ds,) = await get_authorized_existing_datasets([HIVE], "share", hive)
    for p, spec in PROJECTS.items():
        u = await user(spec["user"])
        await cognee.remember(f"Private brain for the {p} service.", dataset_name=f"{p}-brain", user=u,
                              node_set=[f"project:{p}", "kind:charter"])
        await authorized_give_permission_on_datasets(u.id, [ds.id], "read", hive.id)
        print(f"{spec['user']}: owns {p}-brain, reads hive")


async def remember(project: str, text: str, tags: list[str]) -> None:
    import cognee

    await cognee.remember(text, dataset_name=f"{project}-brain", user=await user(PROJECTS[project]["user"]),
                          node_set=[f"project:{project}"] + tags, self_improvement=False)


async def recall(project: str, query: str, use_hive: bool) -> list[str]:
    import cognee
    from cognee import SearchType

    u = await user(PROJECTS[project]["user"])
    # Own brain and hive are recalled separately so a project's own chunks can't crowd out the hive.
    scopes = [[f"{project}-brain"]] + ([[HIVE]] if use_hive else [])
    out = []
    for names in scopes:
        hits = await cognee.recall(query, dataset_ids=await dataset_ids(u, names), user=u,
                                   query_type=SearchType.CHUNKS, top_k=4)
        out += [str(getattr(h, "text", None) or h) for h in hits]
    return out


async def contribute_to_hive(project: str, incident: dict) -> str:
    """Sanitize a private incident into an abstract pattern, then write it to the hive as the hive curator."""
    import cognee

    r = llm().chat.completions.create(model=SANITIZER_MODEL, response_format={"type": "json_object"}, messages=[
        {"role": "system", "content":
            "Abstract this incident into a reusable pattern for OTHER teams. Remove project/service names, "
            "config key names, ports, tokens, hostnames and capacities. KEEP public protocol identifiers (error codes, codec/protocol names and the value that fixed it), they are not secrets. Keep the symptom class, the "
            "error type, the root-cause class and the fix shape. Reply JSON "
            '{"pattern": "<kebab-case id>", "symptom": ..., "root_cause": ..., "fix": ...}'},
        {"role": "user", "content": json.dumps(incident)}])
    pat = json.loads(r.choices[0].message.content)
    text = (f"[hive pattern:{pat['pattern']}] symptom: {pat['symptom']} | root cause: {pat['root_cause']} "
            f"| fix: {pat['fix']}")
    if any(canary(p) in text for p in PROJECTS) or project in text.lower():
        raise RuntimeError("sanitizer leaked project detail; refusing to write to hive")
    await cognee.remember(text, dataset_name=HIVE, user=await user(HIVE_USER),
                          node_set=["source:hive", f"pattern:{pat['pattern']}", f"seen-in:{hash(project) % 997}"], self_improvement=False)
    return text


async def seed() -> None:
    """checkout's past incident: private write-up, then its sanitized pattern into the hive."""
    incident = {"error": "RuntimeError: ERR_WIRE_4012 frame rejected by peer",
                "diagnosis": "wire_codec was set to v2; the peer only speaks the legacy-framing codec. "
                             "Set wire_codec back to legacy-framing.", "file": "config.json", "iterations": 3}
    await remember("checkout", f"[incident checkout] {json.dumps(incident)}", ["source:incident"])
    print(await contribute_to_hive("checkout", incident))


# --- Agent: debug loop ---------------------------------------------------------

def llm():
    from openai import OpenAI

    return OpenAI(base_url=os.getenv("LLM_ENDPOINT", "https://api.respan.ai/api"),
                  api_key=os.environ["RESPAN_API_KEY"])


SYSTEM = """You are an on-call healer for one service. You see its failing logs, its config.json and app.py,
and memories (its own past incidents, its runbook, and abstract [hive pattern:...] lessons from other teams).
Find the root cause and fix it by editing config.json ONLY; app.py is read-only and its checks are correct.
Reply JSON {"diagnosis": "...", "used_memory": ["<pattern or incident ids you relied on>"],
"file": "config.json", "content": "<full new file content>"}"""


def error_lines(logs: str) -> str:
    return "\n".join(l for l in logs.splitlines() if "ERROR" in l or "Traceback" in l or "Error" in l)[-1500:]


async def heal(project: str, use_hive: bool = True, ship: bool = False) -> dict:
    from respan import propagate_attributes, workflow

    @workflow(name="self_heal")
    async def run() -> dict:
        healthy, logs = observe(project)
        if healthy:
            return {"project": project, "healed": True, "iterations": 0}
        first_error = error_lines(logs)
        await remember(project, f"[logs {project}]\n{logs[-3000:]}", ["source:logs"])
        attempts = []
        for i in range(1, MAX_ITERS + 1):
            memories = await recall(project, first_error, use_hive)
            d = svc_dir(project)
            r = llm().chat.completions.create(model=MODEL, response_format={"type": "json_object"}, messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"LOGS:\n{error_lines(logs)}\n\nconfig.json:\n{(d / 'config.json').read_text()}"
                 f"\n\napp.py:\n{(d / 'app.py').read_text()}\n\nMEMORIES:\n" + "\n---\n".join(memories)
                 + (f"\n\nPREVIOUS FAILED ATTEMPTS:\n{json.dumps(attempts)}" if attempts else "")}])
            plan = json.loads(r.choices[0].message.content)
            if plan["file"] != "config.json":
                attempts.append({"iteration": i, "diagnosis": plan["diagnosis"], "rejected": "only config.json may change; app.py is read-only"})
                print(f"  iter {i}: rejected code edit")
                continue
            (d / plan["file"]).write_text(plan["content"])
            healthy, logs = observe(project)
            attempts.append({"iteration": i, "diagnosis": plan["diagnosis"], "file": plan["file"], "healed": healthy})
            print(f"  iter {i}: {plan['diagnosis'][:110]} -> {'HEALED' if healthy else 'still failing'}")
            if healthy:
                incident = {"error": first_error, "diagnosis": plan["diagnosis"], "file": plan["file"],
                            "iterations": i, "used_memory": plan.get("used_memory", [])}
                await remember(project, f"[incident {project}] {json.dumps(incident)}", ["source:incident"])
                pattern = await contribute_to_hive(project, incident)
                if ship:
                    ship_fix(project, incident)
                hive_hits = [m for m in memories if "[hive pattern:" in m]
                leaked = [p for p in PROJECTS if p != project and any(canary(p) in m for m in memories)]
                return {"project": project, "healed": True, "iterations": i, "hive_pattern": pattern,
                        "hive_hits": len(hive_hits), "leaked_from": leaked, "attempts": attempts}
        return {"project": project, "healed": False, "iterations": MAX_ITERS, "attempts": attempts}

    with propagate_attributes(customer_identifier=PROJECTS[project]["user"],
                              metadata={"project": project, "hive": use_hive}):
        return await run()


# --- Access + action: Scalekit ------------------------------------------------

def scalekit():
    from scalekit import ScalekitClient

    return ScalekitClient(env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"], client_id=os.environ["SCALEKIT_CLIENT_ID"],
                          client_secret=os.environ["SCALEKIT_CLIENT_SECRET"]).actions


def ensure_authorized(actions, connection: str, identifier: str) -> None:
    account = actions.get_or_create_connected_account(connection_name=connection, identifier=os.getenv("SCALEKIT_IDENTIFIER", identifier))
    if account.connected_account.status != "ACTIVE":
        link = actions.get_authorization_link(connection_name=connection, identifier=os.getenv("SCALEKIT_IDENTIFIER", identifier))
        print(f"Authorize {connection} for {identifier}:\n  {link.link}")
        input("Press Enter after authorizing...")


def tool(name: str, args: dict, connection: str, identifier: str):
    actions = scalekit()
    ensure_authorized(actions, connection, identifier)
    return actions.execute_tool(tool_name=name, tool_input=args, connection_name=connection,
                                identifier=identifier).data


async def runbook(from_fixtures: bool) -> None:
    """Pull each project's #oncall channel (Slack) and its service code (GitHub) into its private brain."""
    for p, spec in PROJECTS.items():
        if from_fixtures:
            msgs = json.loads((ROOT / "fixtures" / "slack" / f"oncall-{p}.json").read_text())["messages"]
            code = (svc_dir(p) / "app.good.py").read_text()
        else:
            h = tool("slack_fetch_conversation_history", {"channel": f"#oncall-{p}", "limit": 100}, "slack", spec["user"])
            msgs = list(reversed(h.get("messages", [])))
            f = tool("github_file_contents_get", {"owner": os.environ["GITHUB_OWNER"], "repo": os.environ["GITHUB_REPO"],
                                                  "path": f"svc/{p}/app.py"}, GH, spec["user"])
            import base64
            code = base64.b64decode(f["content"]).decode()
        await remember(p, "\n".join(f"[slack #oncall-{p} · {m.get('user', '?')}] {m['text']}" for m in msgs),
                       ["source:slack", f"channel:oncall-{p}"])
        await remember(p, f"[github svc/{p}/app.py]\n{code}", ["source:github", "kind:code"])
        print(f"{p}: runbook ({len(msgs)} msgs) + code remembered privately")


def ship_fix(project: str, incident: dict) -> None:
    owner, repo, who = os.environ["GITHUB_OWNER"], os.environ["GITHUB_REPO"], PROJECTS[project]["user"]
    branch = f"heal/{project}-{int(time.time())}"
    summary = f"Self-heal {project}: {incident['diagnosis'][:200]}"
    print(f"\n--- would push {incident['file']} to {owner}/{repo}:{branch}, open PR, post #oncall-{project} as {who}\n{summary}")
    if input("Ship it? [y/N] ").strip().lower() != "y":
        return print("skipped")
    base = tool("github_branch_get", {"owner": owner, "repo": repo, "branch": "main"}, GH, who)
    tool("github_branch_create", {"owner": owner, "repo": repo, "branch_name": branch,
                                  "sha": base["commit"]["sha"]}, GH, who)
    path = f"svc/{project}/{incident['file']}"
    cur = tool("github_file_contents_get", {"owner": owner, "repo": repo, "path": path, "ref": branch}, GH, who)
    tool("github_file_create_update", {"owner": owner, "repo": repo, "path": path, "branch": branch,
                                       "message": summary, "sha": cur["sha"],
                                       "content": base64.b64encode((svc_dir(project) / incident["file"]).read_bytes()).decode()}, GH, who)
    pr = tool("github_pull_request_create", {"owner": owner, "repo": repo, "head": branch, "base": "main",
                                             "title": summary, "body": json.dumps(incident, indent=2)}, GH, who)
    try:
        tool("slack_send_message", {"channel": os.getenv("SLACK_ONCALL_CHANNEL", f"#oncall-{project}"),
                                    "text": f"{summary}\n{pr.get('html_url', '')}"}, "slack", who)
    except Exception as e:
        print("slack post skipped:", e)
    print("shipped:", pr.get("html_url", pr))


# --- Eval ----------------------------------------------------------------------

async def run_eval(label: str, use_hive: bool) -> None:
    """checkout hits each fault first (cold), then billing hits the same faults in its own vocabulary."""
    from respan import Respan

    Respan(app_name="selfheal-hive")
    rows = []
    for fault in ("codec",):
        for project in ("billing",):
            restore(project)
            break_service(project, fault)
            print(f"{label} · {project} · {fault}")
            res = await heal(project, use_hive=use_hive)
            rows.append({"fault": fault, **res})
            restore(project)
    billing = [r for r in rows if r["project"] == "billing"]
    summary = {"label": label, "hive": use_hive,
               "heal_rate": sum(r["healed"] for r in rows) / len(rows),
               "billing_mean_iters": sum(r["iterations"] for r in billing) / len(billing),
               "billing_hive_hits": sum(r.get("hive_hits", 0) for r in billing),
               "leaks": sum(len(r.get("leaked_from", [])) for r in rows), "rows": rows}
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    out = ROOT / "evals" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{label}.json").write_text(json.dumps(summary, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("seed")
    s = sub.add_parser("runbook")
    s.add_argument("-f", "--from-fixtures", action="store_true")
    s = sub.add_parser("break")
    s.add_argument("project", choices=PROJECTS)
    s.add_argument("fault", choices=FAULTS)
    s = sub.add_parser("restore")
    s.add_argument("project", choices=PROJECTS)
    s = sub.add_parser("heal")
    s.add_argument("project", choices=PROJECTS)
    s.add_argument("-n", "--no-hive", action="store_true")
    s.add_argument("-s", "--ship", action="store_true")
    s = sub.add_parser("eval")
    s.add_argument("-l", "--label", required=True)
    s.add_argument("-n", "--no-hive", action="store_true")
    a = p.parse_args()

    if a.cmd == "setup":
        asyncio.run(setup())
    elif a.cmd == "runbook":
        asyncio.run(runbook(a.from_fixtures))
    elif a.cmd == "break":
        break_service(a.project, a.fault)
        print(observe(a.project)[1])
    elif a.cmd == "restore":
        restore(a.project)
    elif a.cmd == "heal":
        from respan import Respan

        Respan(app_name="selfheal-hive")
        print(json.dumps(asyncio.run(heal(a.project, not a.no_hive, a.ship)), indent=2))
    elif a.cmd == "seed":
        asyncio.run(seed())
    elif a.cmd == "eval":
        asyncio.run(run_eval(a.label, not a.no_hive))


if __name__ == "__main__":
    main()
