#!/usr/bin/env python3
"""Verify the real proxy/app/database path and counter-proven access failures."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time

from traffic import OUTSIDE, ROOT, denied_count, lab, report_fault, request, run, start_server, stop


def http(namespace, address, port, path, status=200):
    result = run(namespace, sys.executable, str(Path(__file__).resolve()),
                 "get", address, str(port), path)
    response = json.loads(result.stdout)
    assert response["status"] == status, response
    return response["body"]


def counter(namespace, name, table="vpc_workload"):
    result = run(namespace, "nft", "-j", "list", "counter", "ip", table, name)
    return next(item["counter"]["packets"] for item in json.loads(result.stdout)["nftables"]
                if "counter" in item)


def tcp_denied(namespace, address, port, filter_namespace, name):
    before = counter(filter_namespace, name)
    result = run(namespace, sys.executable, "-c",
                 "import socket; "
                 f"socket.create_connection(('{address}', {port}), timeout=2)", check=False)
    assert result.returncode != 0 and "TimeoutError" in result.stderr, result
    assert counter(filter_namespace, name) > before, "Connection failed without hitting the filter"


def capture_return_fault():
    syn = "tcp dst port 5432 and src host 10.0.2.10 and dst host 10.0.3.10 and tcp[13] & 18 == 2"
    reply = "tcp src port 5432 and src host 10.0.3.10 and dst host 10.0.2.10 and tcp[13] & 18 == 18"
    points = [("vpc-app", "out", syn), ("vpc-db", "out", reply), ("vpc-app", "in", reply)]
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        captures = []
        for index, (namespace, direction, packet_filter) in enumerate(points):
            packets = Path(directory) / f"{index}.packets"
            errors = Path(directory) / f"{index}.stderr"
            process = subprocess.Popen(
                ["ip", "netns", "exec", namespace, "timeout", "-k", "1", "5",
                 "tcpdump", "-nn", "-l", "-i", "eth0", "-Q", direction, "-c", "1", packet_filter],
                stdout=stack.enter_context(packets.open("w")),
                stderr=stack.enter_context(errors.open("w")),
            )
            stack.callback(stop, process)
            captures.append((process, packets, errors))
        deadline = time.monotonic() + 3
        while not all("listening on" in errors.read_text() for _, _, errors in captures):
            assert time.monotonic() < deadline, "Return-path captures did not become ready"
            assert all(process.poll() is None for process, *_ in captures), "Packet capture exited early"
            time.sleep(0.02)
        before = counter("vpc-router", "denied_return", "vpc_fault")
        assert http("vpc-internet", "203.0.113.20", 80, "/health")["status"] == "ok"
        assert http("vpc-internet", "203.0.113.20", 80, "/message", status=503)["error"] == "database unavailable"
        after = counter("vpc-router", "denied_return", "vpc_fault")
        assert after > before, "Reply failure did not hit the stateless filter"
        observed = []
        for index, (process, packets, errors) in enumerate(captures):
            expected_exit = 124 if index == 2 else 0
            assert process.wait(timeout=6) == expected_exit, errors.read_text()
            observed.append(packets.read_text().strip())
        source_port = re.search(r"IP 10\.0\.2\.10\.(\d+) > 10\.0\.3\.10\.5432: Flags \[S\]", observed[0])
        assert source_port, observed[0]
        port = int(source_port.group(1))
        assert 1024 <= port <= 65535, port
        assert f"IP 10.0.3.10.5432 > 10.0.2.10.{port}: Flags [S.]" in observed[1], observed[1]
        assert not observed[2], f"Blocked reply reached the app: {observed[2]}"
        print(f"PACKET app outgoing: {observed[0]}")
        print(f"PACKET database outgoing: {observed[1]}")
        print(f"PACKET app incoming: no SYN-ACK during the 5-second capture; denied_return {before} -> {after}")


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

        started = time.monotonic()
        lab("fault", "private-route")
        try:
            lab("fault", "private-route")
            assert "unreachable default" in run("vpc-router", "ip", "route", "show", "table", "102").stdout
            lookup = run("vpc-router", "ip", "route", "get", OUTSIDE,
                         "from", "10.0.2.10", "iif", "private", check=False)
            assert lookup.returncode != 0, lookup
            print(f"ROUTE private to {OUTSIDE}: {lookup.stderr.strip()}")
            request("vpc-internet", OUTSIDE, peer=OUTSIDE)
            request("vpc-app", OUTSIDE, denied=True)
            request("vpc-web", OUTSIDE, peer="203.0.113.20")
            assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        finally:
            lab("repair", "private-route")
            lab("repair", "private-route")
        request("vpc-app", OUTSIDE, peer="203.0.113.30")
        print("PASS: a missing private default rejects egress without main-table fallback; public HTTP and database queries survive; repair restores egress")
        report_fault("private-route", "vpc-app 10.0.2.10", f"{OUTSIDE}:8080", started,
                     "table 102 unreachable default; failed route lookup above; fresh HTTP after repair")

        started = time.monotonic()
        lab("fault", "database")
        try:
            lab("fault", "database")
            before = counter("vpc-db", "denied_database", "vpc_fault")
            assert http("vpc-internet", "203.0.113.20", 80, "/health")["status"] == "ok"
            assert http("vpc-internet", "203.0.113.20", 80, "/message", status=503)["error"] == "database unavailable"
            assert counter("vpc-db", "denied_database", "vpc_fault") > before, "Database fault did not reach the filter"
        finally:
            lab("repair", "database")
            lab("repair", "database")
        assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        print("PASS: denying app-to-database TCP leaves health up, returns HTTP 503, and recovers after repair")
        report_fault("database", "vpc-app 10.0.2.10", "10.0.3.10:5432", started,
                     "vpc-db denied_database increases; /health 200 and /message 503; fresh database value after repair")

        started = time.monotonic()
        lab("fault", "return-ports")
        try:
            lab("fault", "return-ports")
            capture_return_fault()
        finally:
            lab("repair", "return-ports")
            lab("repair", "return-ports")
        assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "A fresh database value"
        assert run("vpc-router", "nft", "list", "table", "ip", "vpc_fault", check=False).returncode != 0
        print("PASS: stateless reply filtering loses SYN-ACKs to the app's ephemeral port; health stays up and database queries recover after repair")
        report_fault("return-ports", "vpc-db 10.0.3.10:5432", "vpc-app 10.0.2.10 ephemeral TCP port", started,
                     "matched SYN/SYN-ACK captures and denied_return above; no delivered SYN-ACK; fresh query after repair")

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
