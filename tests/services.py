#!/usr/bin/env python3
"""Verify the real proxy/app/database path and counter-proven access failures."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile

from traffic import OUTSIDE, ROOT, denied_count, request, run, start_server, stop


def http(namespace, address, port, path, status=200):
    result = run(namespace, sys.executable, str(Path(__file__).resolve()),
                 "get", address, str(port), path)
    response = json.loads(result.stdout)
    assert response["status"] == status, response
    return response["body"]


def counter(namespace, name):
    result = run(namespace, "nft", "-j", "list", "counter", "ip", "vpc_workload", name)
    return next(item["counter"]["packets"] for item in json.loads(result.stdout)["nftables"]
                if "counter" in item)


def tcp_denied(namespace, address, port, filter_namespace, name):
    before = counter(filter_namespace, name)
    result = run(namespace, sys.executable, "-c",
                 "import socket; "
                 f"socket.create_connection(('{address}', {port}), timeout=2)", check=False)
    assert result.returncode != 0 and "TimeoutError" in result.stderr, result
    assert counter(filter_namespace, name) > before, "Connection failed without hitting the filter"


def test_services():
    with ExitStack() as stack:
        services = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "app/services.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stack.callback(services.stdout.close)
        stack.callback(services.stderr.close)
        stack.callback(stop, services)
        with selectors.DefaultSelector() as selector:
            selector.register(services.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=25), "Demo services did not start"
            if services.stdout.readline().strip() != "ready":
                raise AssertionError(services.stderr.read())

        assert http("vpc-web", "10.0.2.10", 8080, "/health") == {
            "status": "ok", "peer": "10.0.1.10"}
        message = http("vpc-internet", "203.0.113.20", 80, "/message")
        assert message == {"message": "Hello from the isolated subnet", "peer": "10.0.1.10"}, message
        http("vpc-internet", "203.0.113.20", 80, "/missing", status=404)
        # The stored value must be read again, not served from a canned HTTP reply.
        sql = "UPDATE demo_message SET message = 'A fresh database value' WHERE id = 1;"
        run("vpc-db", "/usr/lib/postgresql/16/bin/psql", "-h", "10.0.3.10", "-U", "postgres",
            "-d", "vpc_lab", "-v", "ON_ERROR_STOP=1", input=sql)
        assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        print("PASS: Nginx opens a new app connection; a real TCP PostgreSQL query returns fresh data")

        tcp_denied("vpc-web", "10.0.3.10", 5432, "vpc-web", "denied_output")
        # Prove database ingress independently of the proxy's outbound policy.
        run("vpc-web", "nft", "insert", "rule", "ip", "vpc_workload", "output",
            "ip", "daddr", "10.0.3.10", "tcp", "dport", "5432", "accept")
        try:
            tcp_denied("vpc-web", "10.0.3.10", 5432, "vpc-db", "denied_input")
        finally:
            run("vpc-web", "nft", "-f", "-", input=(
                "delete table ip vpc_workload\n" + (ROOT / "network/web.nft").read_text()))
        tcp_denied("vpc-db", "10.0.2.10", 8080, "vpc-db", "denied_output")
        run("vpc-db", "nft", "insert", "rule", "ip", "vpc_workload", "output",
            "ip", "daddr", "10.0.2.10", "tcp", "dport", "8080", "accept")
        try:
            tcp_denied("vpc-db", "10.0.2.10", 8080, "vpc-app", "denied_input")
        finally:
            run("vpc-db", "nft", "-f", "-", input=(
                "delete table ip vpc_workload\n" + (ROOT / "network/db.nft").read_text()))
        assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        print("PASS: proxy-to-database and new database-to-app connections hit host deny counters; replies work")

        # A healthy listener makes this a firewall test, not a closed-port test.
        start_server(stack, "vpc-web", "10.0.1.10", port=8081)
        before = denied_count()
        request("vpc-internet", "203.0.113.20", port=8081, denied=True)
        assert denied_count() > before, "Unused-port rejection did not hit the edge filter"
        print("PASS: external traffic to a healthy listener on an unused public port is denied at the edge")

        start_server(stack, "vpc-internet", OUTSIDE)
        request("vpc-app", OUTSIDE, peer="203.0.113.30")
        run("vpc-nat", "ip", "link", "set", "eth0", "down")
        try:
            request("vpc-app", OUTSIDE, denied=True)
            assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        finally:
            run("vpc-nat", "ip", "link", "set", "eth0", "up")
            run("vpc-nat", "ip", "route", "replace", "default", "via", "10.0.1.1", "dev", "eth0")
        request("vpc-app", OUTSIDE, peer="203.0.113.30")
        print("PASS: the proxy/app/PostgreSQL path survives lost NAT while private external HTTP fails and recovers")

        # Drop a fresh database connection before the established-state rule.
        run("vpc-db", "nft", "insert", "rule", "ip", "vpc_workload", "input",
            "ip", "saddr", "10.0.2.10", "tcp", "dport", "5432",
            "counter", "name", "denied_input", "drop")
        try:
            before = counter("vpc-db", "denied_input")
            assert http("vpc-internet", "203.0.113.20", 80, "/health")["status"] == "ok"
            assert http("vpc-internet", "203.0.113.20", 80, "/message", status=503)["error"] == "database unavailable"
            assert counter("vpc-db", "denied_input") > before, "Database fault did not reach the filter"
        finally:
            run("vpc-db", "nft", "-f", "-", input=(
                "delete table ip vpc_workload\n" + (ROOT / "network/db.nft").read_text()))
        assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        print("PASS: denying app-to-database TCP leaves health up, returns HTTP 503, and recovers after repair")

    for namespace in ("vpc-web", "vpc-app", "vpc-db"):
        pids = subprocess.run(["ip", "netns", "pids", namespace],
                              capture_output=True, text=True, check=True, timeout=5)
        assert not pids.stdout.strip(), "Service process survived"
    print("PASS: all demo services stop before namespace teardown")

    # Failure after starting PostgreSQL and the app must not strand either one.
    with tempfile.TemporaryDirectory() as directory:
        nginx = Path(directory) / "nginx"
        nginx.write_text("#!/bin/sh\nexit 23\n")
        nginx.chmod(0o755)
        result = subprocess.run(
            [sys.executable, str(ROOT / "app/services.py")],
            env={**os.environ, "PATH": directory + ":" + os.environ["PATH"]},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1, result
        assert "Service did not become ready" in result.stderr, result.stderr
    for namespace in ("vpc-web", "vpc-app", "vpc-db"):
        pids = subprocess.run(["ip", "netns", "pids", namespace],
                              capture_output=True, text=True, check=True, timeout=5)
        assert not pids.stdout.strip(), "Failed startup left a service process behind"
    assert not list(Path("/tmp").glob("vpc-services-*")), "Temporary service files survived"
    print("PASS: failed proxy startup stops app/database processes and removes temporary service files")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "get":
        from http.client import HTTPConnection
        connection = HTTPConnection(sys.argv[2], int(sys.argv[3]), timeout=6)
        try:
            connection.request("GET", sys.argv[4])
            response = connection.getresponse()
            print(json.dumps({"status": response.status, "body": json.loads(response.read())}))
        finally:
            connection.close()
    else:
        try:
            test_services()
        except (AssertionError, subprocess.SubprocessError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            if isinstance(error, subprocess.CalledProcessError):
                print(error.stderr, file=sys.stderr)
            sys.exit(1)
