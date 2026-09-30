#!/usr/bin/env python3
"""Exercise real HTTP flows and NAT faults in the disposable integration lab."""

from contextlib import ExitStack
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import selectors
from socketserver import TCPServer
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
OUTSIDE = "203.0.113.10"
PORT = 8080


def lab(*command):
    return subprocess.run(["bash", str(ROOT / "lab.sh"), *command],
                          capture_output=True, text=True, timeout=10, check=True)


def report_fault(name, source, destination, started, evidence):
    # Emit only after the fault and its repair have both passed their assertions.
    print("RESULT: " + json.dumps({
        "case": name, "source": source, "destination": destination,
        "expected": "denied during fault; reachable after repair",
        "observed": "denied during fault; reachable after repair",
        "duration_seconds": round(time.monotonic() - started, 3), "evidence": evidence,
    }))


def run(namespace, *command, check=True, input=None):
    return subprocess.run(
        ["ip", "netns", "exec", namespace, *command],
        input=input, capture_output=True, text=True, timeout=8, check=check,
    )


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class PeerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"peer": self.client_address[0],
                           "peer_port": self.client_address[1]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(address, port):
    # HTTPServer performs a reverse DNS lookup during bind; the lab needs no DNS.
    TCPServer.allow_reuse_address = True
    with TCPServer((address, port), PeerHandler) as server:
        print("ready", flush=True)
        server.serve_forever()


def client(address, port):
    connection = HTTPConnection(address, port, timeout=2)
    try:
        connection.connect()
        local = connection.sock.getsockname()
        connection.request("GET", "/")
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status: {response.status}")
        result = json.loads(response.read())
        result["client"] = list(local)
        print(json.dumps(result))
    except (OSError, HTTPException) as error:
        print(json.dumps({"network_error": str(error)}), file=sys.stderr)
        return 1
    finally:
        connection.close()
    return 0


def request(namespace, address, peer=None, denied=False, port=PORT):
    result = run(namespace, sys.executable, str(Path(__file__).resolve()),
                 "request", address, str(port), check=False)
    if denied:
        assert result.returncode == 1, f"Unexpected HTTP result: {result.stdout} {result.stderr}"
        assert "network_error" in json.loads(result.stderr), result.stderr
        return None
    assert result.returncode == 0, f"{namespace} -> {address}: {result.stderr}"
    body = json.loads(result.stdout)
    if peer is not None:
        assert body["peer"] == peer, body
    return body


def start_server(stack, namespace, address, port=PORT):
    process = subprocess.Popen(
        ["ip", "netns", "exec", namespace, sys.executable, str(Path(__file__).resolve()),
         "serve", address, str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stack.callback(process.stdout.close)
    stack.callback(process.stderr.close)
    stack.callback(stop, process)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=5), f"{namespace} HTTP fixture did not start"
        assert process.stdout.readline().strip() == "ready", f"{namespace} HTTP fixture failed"
    request(namespace, address, peer=address, port=port)


def capture_nat():
    # Capture the same new connection at each translation and at final delivery.
    syn = f"tcp dst port {PORT} and dst host {OUTSIDE} and tcp[13] & 2 != 0"
    reply = f"tcp src port {PORT} and src host {OUTSIDE} and tcp[13] & 18 == 18"
    points = [
        ("vpc-app", "eth0", "out", syn, "10.0.2.10", OUTSIDE),
        ("vpc-nat", "eth0", "out", syn, "10.0.1.20", OUTSIDE),
        ("vpc-edge", "internet", "out", syn, "203.0.113.30", OUTSIDE),
        ("vpc-app", "eth0", "in", reply, OUTSIDE, "10.0.2.10"),
    ]
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        captures = []
        for index, (namespace, interface, direction, packet_filter, source, target) in enumerate(points):
            packets = Path(directory) / f"{index}.packets"
            errors = Path(directory) / f"{index}.stderr"
            process = subprocess.Popen(
                ["ip", "netns", "exec", namespace, "tcpdump", "-n", "-l", "-i", interface,
                 "-Q", direction, "-c", "1", packet_filter],
                stdout=stack.enter_context(packets.open("w")),
                stderr=stack.enter_context(errors.open("w")),
            )
            stack.callback(stop, process)
            captures.append((process, packets, errors, source, target))
        deadline = time.monotonic() + 5
        while not all("listening on" in errors.read_text() for _, _, errors, _, _ in captures):
            assert time.monotonic() < deadline, "Packet captures did not become ready"
            assert all(process.poll() is None for process, *_ in captures), "Packet capture exited early"
            time.sleep(0.02)
        body = request("vpc-app", OUTSIDE, peer="203.0.113.30")
        for process, packets, errors, source, target in captures:
            assert process.wait(timeout=5) == 0, errors.read_text()
            packet = packets.read_text().strip()
            assert f"IP {source}." in packet and f" > {target}." in packet, packet
            print(f"PACKET: {packet}")
        print(f"HTTP: client {body['client']} observed as {body['peer']}:{body['peer_port']}")

    for namespace, source, translated in [
        ("vpc-nat", "10.0.2.10", "10.0.1.20"),
        ("vpc-edge", "10.0.1.20", "203.0.113.30"),
    ]:
        entries = run(namespace, "conntrack", "-L", "-p", "tcp",
                      "--orig-src", source, "--orig-dst", OUTSIDE).stdout.splitlines()
        matches = [entry for entry in entries
                   if f"src={source} dst={OUTSIDE} " in entry
                   and f"src={OUTSIDE} dst={translated} " in entry]
        assert matches, f"Missing {namespace} original/reply tuples: {entries}"
        for entry in matches:
            print(f"CONNTRACK {namespace}: {entry}")
    print("PASS: HTTP, four packet captures, and conntrack prove both NAT translations and the reply")


def denied_count():
    result = run("vpc-edge", "nft", "-j", "list", "counter", "ip", "vpc_edge", "denied_forward")
    return next(item["counter"]["packets"] for item in json.loads(result.stdout)["nftables"]
                if "counter" in item)


def test_traffic():
    # Refuse absent/replaced namespaces before starting or changing anything.
    subprocess.run(["bash", str(ROOT / "lab.sh"), "status"], check=True,
                   stdout=subprocess.DEVNULL, timeout=10)
    with ExitStack() as stack:
        for namespace, address, port in [
            ("vpc-internet", OUTSIDE, PORT), ("vpc-web", "10.0.1.10", 80),
            ("vpc-app", "10.0.2.10", 8080), ("vpc-db", "10.0.3.10", 5432),
            ("vpc-nat", "10.0.1.20", PORT),
        ]:
            start_server(stack, namespace, address, port)

        # A missing return route makes any untranslated private source unusable.
        result = run("vpc-internet", "ip", "route", "get", "10.0.2.10", check=False)
        assert result.returncode != 0, "External fixture has a VPC return route"
        request("vpc-web", OUTSIDE, peer="203.0.113.20")
        request("vpc-internet", "203.0.113.20", peer=OUTSIDE, port=80)
        capture_nat()
        request("vpc-db", OUTSIDE, denied=True)
        request("vpc-app", "10.0.3.10", peer="10.0.2.10", port=5432)
        print("PASS: public HTTP works in both directions; isolated egress is denied")

        # Verify the NAT-local listener before testing its blocked public mapping.
        request("vpc-nat", "10.0.1.20", peer="10.0.1.20")
        before = denied_count()
        request("vpc-internet", "203.0.113.30", denied=True)
        assert denied_count() > before, "NAT ingress failed without reaching the edge filter"

        # Deliberately supply an attacker-side route: the edge must still reject direct ingress.
        run("vpc-internet", "ip", "route", "add", "10.0.0.0/16", "via", "203.0.113.1")
        try:
            for address, port in (("10.0.1.10", 80), ("10.0.2.10", 8080), ("10.0.3.10", 5432)):
                before = denied_count()
                request("vpc-internet", address, denied=True, port=port)
                assert denied_count() > before, "Direct ingress did not hit the edge filter"
        finally:
            run("vpc-internet", "ip", "route", "delete", "10.0.0.0/16")
        print("PASS: healthy private services and NAT reject unsolicited external HTTP at the edge")

        run("vpc-nat", "ip", "link", "set", "eth0", "down")
        try:
            request("vpc-app", OUTSIDE, denied=True)
            request("vpc-app", "10.0.3.10", peer="10.0.2.10", port=5432)
            request("vpc-web", OUTSIDE, peer="203.0.113.20")
        finally:
            run("vpc-nat", "ip", "link", "set", "eth0", "up")
            # Taking the interface down also removes its static default route.
            run("vpc-nat", "ip", "route", "replace", "default", "via", "10.0.1.1", "dev", "eth0")
        request("vpc-app", OUTSIDE, peer="203.0.113.30")
        print("PASS: losing NAT breaks private egress while internal HTTP and public egress survive")

        for namespace, table, filename in [
            ("vpc-nat", "vpc_nat", "nat.nft"),
            ("vpc-edge", "vpc_edge", "edge.nft"),
        ]:
            started = time.monotonic()
            if namespace == "vpc-nat":
                lab("fault", "nat-snat")
            else:
                run(namespace, "nft", "flush", "chain", "ip", table, "postrouting")
            try:
                if namespace == "vpc-nat":
                    lab("fault", "nat-snat")
                    assert "snat to" not in run(namespace, "nft", "list", "chain", "ip", table, "postrouting").stdout
                    request("vpc-web", OUTSIDE, peer="203.0.113.20")
                    before = denied_count()
                request("vpc-internet", OUTSIDE, peer=OUTSIDE)
                request("vpc-app", OUTSIDE, denied=True)
                if namespace == "vpc-nat":
                    assert denied_count() > before, "Untranslated private traffic did not hit the edge filter"
                request("vpc-app", "10.0.3.10", peer="10.0.2.10", port=5432)
            finally:
                if namespace == "vpc-nat":
                    lab("repair", "nat-snat")
                    lab("repair", "nat-snat")
                else:
                    run(namespace, "nft", "-f", "-", input=(
                        f"delete table ip {table}\n" + (ROOT / "network" / filename).read_text()))
            request("vpc-app", OUTSIDE, peer="203.0.113.30")
            print(f"PASS: removing {namespace} SNAT breaks new HTTP flows; restoring it repairs them")
            if namespace == "vpc-nat":
                report_fault("nat-snat", "vpc-app 10.0.2.10", f"{OUTSIDE}:{PORT}", started,
                             "empty vpc_nat postrouting chain; vpc_edge denied_forward increases; HTTP peer after repair")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "serve":
        serve(sys.argv[2], int(sys.argv[3]))
    elif len(sys.argv) == 4 and sys.argv[1] == "request":
        sys.exit(client(sys.argv[2], int(sys.argv[3])))
    elif len(sys.argv) == 1:
        try:
            test_traffic()
        except (AssertionError, subprocess.SubprocessError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            if isinstance(error, subprocess.CalledProcessError):
                print(error.stderr, file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit("Run through tests/integration.sh in a disposable Linux environment")
