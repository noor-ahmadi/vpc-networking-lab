#!/usr/bin/env python3
"""Run disposable demo services in an already-created lab; Ctrl+C stops them."""

from contextlib import ExitStack
import os
from pathlib import Path
import pwd
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parent.parent
PG_BIN = Path("/usr/lib/postgresql/16/bin")


def run(namespace, *command, input=None, check=True):
    return subprocess.run(
        ["ip", "netns", "exec", namespace, *map(str, command)],
        input=input, text=True, capture_output=True, timeout=15, check=check,
    )


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def wait_ready(process, probe):
    deadline = time.monotonic() + 10
    while process.poll() is None:
        if probe().returncode == 0:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    raise RuntimeError("Service did not become ready")


def http_probe(namespace, address, port):
    return run(namespace, sys.executable, "-c",
               "from http.client import HTTPConnection; "
               f"c = HTTPConnection('{address}', {port}, timeout=1); "
               "c.request('GET', '/health'); r = c.getresponse(); "
               "assert r.status == 200; r.read(); c.close()", check=False)


def serve():
    if os.geteuid() != 0:
        raise RuntimeError("Run as root in a disposable Linux environment")
    subprocess.run(["bash", str(ROOT / "lab.sh"), "status"], check=True,
                   stdout=subprocess.DEVNULL, timeout=10)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())

    with tempfile.TemporaryDirectory(prefix="vpc-services-") as directory:
        scratch = Path(directory)
        scratch.chmod(0o755)
        data = scratch / "postgres"
        data.mkdir(mode=0o700)
        owner = pwd.getpwnam("postgres")
        os.chown(data, owner.pw_uid, owner.pw_gid)
        logs = []
        try:
            with ExitStack() as stack:
                def start(namespace, *command, env=None):
                    log_path = scratch / f"{namespace}.log"
                    logs.append(log_path)
                    log = stack.enter_context(log_path.open("w"))
                    process = subprocess.Popen(
                        ["ip", "netns", "exec", namespace, *map(str, command)],
                        stdout=log, stderr=log, env=env, start_new_session=True,
                    )
                    stack.callback(stop, process)
                    return process

                run("vpc-db", "setpriv", "--reuid=postgres", "--regid=postgres", "--init-groups", PG_BIN / "initdb",
                    "-D", data, "--auth-local=trust", "--auth-host=scram-sha-256")
                with (data / "postgresql.conf").open("a") as config:
                    config.write("\nlisten_addresses = '10.0.3.10'\n"
                                 "unix_socket_directories = ''\n")
                (data / "pg_hba.conf").write_text(
                    "host all postgres 10.0.3.10/32 trust\n"
                    "host vpc_lab vpc_app 10.0.2.10/32 scram-sha-256\n")
                database = start("vpc-db", "setpriv", "--reuid=postgres", "--regid=postgres", "--init-groups",
                                 PG_BIN / "postgres", "-D", data)
                wait_ready(database, lambda: run("vpc-db", PG_BIN / "pg_isready",
                           "-h", "10.0.3.10", "-U", "postgres", check=False))
                password = secrets.token_hex(24)
                psql = [PG_BIN / "psql", "-h", "10.0.3.10", "-U", "postgres",
                        "-v", "ON_ERROR_STOP=1"]
                run("vpc-db", *psql, "-d", "postgres", input=(
                    f"CREATE ROLE vpc_app LOGIN PASSWORD '{password}';\n"
                    "CREATE DATABASE vpc_lab;\n"))
                run("vpc-db", *psql, "-d", "vpc_lab",
                    input=(ROOT / "app/seed.sql").read_text())
                application = start("vpc-app", sys.executable, ROOT / "app/server.py",
                                    env={**os.environ, "VPC_DB_PASSWORD": password})
                wait_ready(application, lambda: http_probe("vpc-web", "10.0.2.10", 8080))
                proxy_dir = scratch / "nginx"
                proxy_dir.mkdir()
                proxy = start("vpc-web", "nginx", "-p", f"{proxy_dir}/",
                              "-c", ROOT / "app/nginx.conf",
                              "-g", "daemon off; master_process off;")
                wait_ready(proxy, lambda: http_probe("vpc-web", "10.0.1.10", 80))
                print("ready", flush=True)
                while not stopped.wait(0.2):
                    if any(process.poll() is not None for process in (database, application, proxy)):
                        raise RuntimeError("A demo service exited")
        except Exception:
            for log_path in logs:
                print(log_path.read_text(), file=sys.stderr)
            raise


if __name__ == "__main__":
    try:
        serve()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
