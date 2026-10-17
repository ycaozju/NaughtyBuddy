#!/usr/bin/env python3
"""Start/stop only this project's local servers; do not touch other services."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
PIDFILE = RUNTIME / "processes.json"
PYTHON = ROOT / ".venv" / "bin" / "python"
LLAMA = RUNTIME / "llama-cpu" / "bin" / "llama-server"


def fetch(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        return json.load(response)


def identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        return stat[21] if stat[2] != "Z" else None
    except (OSError, IndexError):
        return None


def owned(record):
    return identity(record["pid"]) == record["start"]


def records():
    return json.loads(PIDFILE.read_text()) if PIDFILE.exists() else {}


def write_records(value):
    temporary = PIDFILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(PIDFILE)


def stop():
    value = records()
    for name in ("app", "llm"):
        record = value.get(name)
        if record and owned(record):
            os.kill(record["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 12
            while owned(record) and time.monotonic() < deadline:
                time.sleep(.2)
            if owned(record):
                os.kill(record["pid"], signal.SIGKILL)
            print(f"Stopped {name}")
    PIDFILE.unlink(missing_ok=True)


def start(args):
    RUNTIME.mkdir(exist_ok=True)
    value = {name: record for name, record in records().items() if owned(record)}
    for path in (PYTHON, LLAMA, ROOT / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"):
        if not path.exists():
            raise SystemExit(f"Missing {path}. Follow README setup first.")
    specs = {
        "llm": [str(LLAMA), "-m", str(ROOT / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"), "--host", "127.0.0.1", "--port", "8081", "-c", "8192", "-np", "2", "-t", "4", "-tb", "4", "--poll", "0", "--poll-batch", "0", "--log-disable", "--alias", "babybot-local"],
        "app": [str(PYTHON), "-m", "uvicorn", "babybot.app:create_app", "--factory", "--host", args.host, "--port", str(args.port), "--ws-max-size", "700000", "--no-access-log", "--workers", "1"],
    }
    if args.cert or args.key:
        if not (args.cert and args.key):
            raise SystemExit("Both --cert and --key are required")
        specs["app"] += ["--ssl-certfile", str(Path(args.cert).resolve()), "--ssl-keyfile", str(Path(args.key).resolve())]
    for name, command in specs.items():
        if name in value:
            print(f"{name} already running")
            continue
        # Refuse to adopt/kill an unrelated service at the default endpoint.
        if name == "llm":
            try:
                fetch("http://127.0.0.1:8081/health")
            except Exception:
                pass
            else:
                raise SystemExit("Port 8081 is already serving an unmanaged model. Stop it yourself or use the manual commands in the documentation.")
        else:
            with socket.socket() as probe:
                probe.settimeout(1)
                if probe.connect_ex(("127.0.0.1", args.port)) == 0:
                    raise SystemExit(f"Port {args.port} is occupied by an unmanaged service; no process was replaced.")
        with (RUNTIME / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(.4)
        if process.poll() is not None:
            raise SystemExit(f"{name} exited; inspect .runtime/{name}.log")
        value[name] = {"pid": process.pid, "start": identity(process.pid)}
        write_records(value)
        if name == "llm":
            for _ in range(60):
                try:
                    if fetch("http://127.0.0.1:8081/health").get("status") == "ok":
                        break
                except Exception:
                    pass
                time.sleep(.5)
            else:
                raise SystemExit("Model readiness timed out; inspect .runtime/llm.log")
        else:
            for _ in range(40):
                if process.poll() is not None:
                    raise SystemExit("App exited; inspect .runtime/app.log")
                with socket.socket() as probe:
                    probe.settimeout(1)
                    if probe.connect_ex(("127.0.0.1", args.port)) == 0:
                        break
                time.sleep(.25)
            else:
                raise SystemExit("App readiness timed out; inspect .runtime/app.log")
    print(f"Open {'https' if args.cert else 'http'}://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    print("Family code and adult PIN: .runtime/credentials.json (never share the adult PIN with children).")


def status():
    for name, record in records().items():
        print(name, "running" if owned(record) else "stopped", f"pid={record['pid']}")
    try:
        print("HTTP", fetch("http://127.0.0.1:8765/api/health"))
    except Exception:
        print("Default HTTP endpoint is not reachable (or HTTPS/custom port is configured).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status", "restart"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    args = parser.parse_args()
    if args.command in {"stop", "restart"}:
        stop()
    if args.command in {"start", "restart"}:
        start(args)
    elif args.command == "status":
        status()
