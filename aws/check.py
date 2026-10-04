#!/usr/bin/env python3
"""Check the deployed lab or its teardown using AWS CLI and local SSH aliases."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


ROLES = {"web": "10.0.1.10", "app": "10.0.2.10", "db": "10.0.3.10"}
ALIASES = {"web": "vpc-lab-proxy", "app": "vpc-lab-app", "db": "vpc-lab-db"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def aws_error(stderr):
    match = re.search(r"An error occurred \(([\w.]+)\)", stderr)
    return match[1] if match else "command failed"


class Lab:
    def __init__(self, inventory, profile, ssh_config=None):
        self.inventory = inventory
        self.profile = profile
        self.ssh_config = ssh_config
        require(re.fullmatch(r"\d{12}", inventory["account_id"]), "Invalid account ID")
        require(re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", inventory["region"]), "Invalid region")
        for name, prefix in (("vpc_id", "vpc"), ("internet_gateway_id", "igw"),
                             ("nat_gateway_id", "nat")):
            self.validate_id(inventory[name], prefix)
        for name, prefix, roles in (("instance_ids", "i", set(ROLES)),
                                    ("root_volume_ids", "vol", set(ROLES)),
                                    ("elastic_ip_ids", "eipalloc", {"web", "nat"})):
            require(set(inventory[name]) <= roles, f"Unexpected roles in {name}")
            require(len(set(inventory[name].values())) == len(inventory[name]), f"Duplicate {name}")
            for value in inventory[name].values():
                self.validate_id(value, prefix)
        require(set(inventory["root_volume_ids"]) == set(inventory["instance_ids"]),
                "Every saved instance needs its root volume ID")
        require(set(inventory["elastic_ip_ids"]) == {"web", "nat"}, "Save both EIP IDs")
        for name in ("proxy_ip", "nat_ip"):
            ipaddress.IPv4Address(inventory[name])
        require(inventory["proxy_ip"] != inventory["nat_ip"], "Public mappings must differ")

    @staticmethod
    def validate_id(value, prefix):
        require(re.fullmatch(prefix + r"-(?:[0-9a-f]{8}|[0-9a-f]{17})", value),
                f"Invalid {prefix} ID")

    def aws(self, *args, missing=None):
        result = subprocess.run(
            ["aws", *args, "--profile", self.profile, "--region", self.inventory["region"],
             "--output", "json", "--no-cli-pager"],
            capture_output=True, text=True, encoding="utf-8", timeout=45,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        if result.returncode:
            code = aws_error(result.stderr)
            if missing is not None and code == missing:
                return None
            raise RuntimeError(f"AWS {args[0]} {args[1]}: {code}")
        return json.loads(result.stdout)

    def identity(self):
        require(self.aws("sts", "get-caller-identity")["Account"] == self.inventory["account_id"],
                "AWS account differs from the saved inventory")

    def ssh(self, role, command, script=None):
        result = subprocess.run(
            ["ssh", "-F", Path(self.ssh_config).resolve().as_posix(), "-o", "BatchMode=yes", "-o",
             "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5", ALIASES[role], command],
            input=script, capture_output=True, text=True, encoding="utf-8", timeout=45,
        )
        require(result.returncode == 0, f"SSH check failed on {role} (exit {result.returncode}); rerun SSH privately for diagnostics")
        return result.stdout

    def python(self, role, script):
        return json.loads(self.ssh(role, "python3 -", script))

    def topology(self):
        vpc = self.inventory["vpc_id"]
        nat = self.aws("ec2", "describe-nat-gateways", "--nat-gateway-ids",
                       self.inventory["nat_gateway_id"])["NatGateways"]
        require(len(nat) == 1 and nat[0]["State"] == "available" and nat[0]["VpcId"] == vpc,
                "Saved NAT is not available in the lab VPC")
        require(any(a.get("PublicIp") == self.inventory["nat_ip"]
                    and a.get("AllocationId") == self.inventory["elastic_ip_ids"]["nat"]
                    and a.get("PrivateIp") == "10.0.1.20" for a in nat[0]["NatGatewayAddresses"]),
                "Unexpected NAT mapping")
        acls = self.aws("ec2", "describe-network-acls", "--filters", f"Name=vpc-id,Values={vpc}")["NetworkAcls"]
        require(len(acls) == 1 and acls[0]["IsDefault"], "Baseline requires the default NACL")
        for egress in (False, True):
            entries = sorted((e for e in acls[0]["Entries"] if e["Egress"] == egress and "CidrBlock" in e),
                             key=lambda e: e["RuleNumber"])
            require(entries and entries[0]["Protocol"] == "-1" and entries[0]["CidrBlock"] == "0.0.0.0/0"
                    and entries[0]["RuleAction"] == "allow", "Default NACL is not allowing IPv4")
        records = self.aws("ec2", "describe-instances", "--instance-ids",
                           *self.inventory["instance_ids"].values())["Reservations"]
        instances = {i["InstanceId"]: i for r in records for i in r["Instances"]}
        groups = {}
        for role, identifier in self.inventory["instance_ids"].items():
            instance = instances[identifier]
            require(instance["VpcId"] == vpc and instance["State"]["Name"] == "running",
                    f"{role} is not running in the saved VPC")
            require(instance["PrivateIpAddress"] == ROLES[role], f"Wrong {role} address")
            require(dict((t["Key"], t["Value"]) for t in instance["Tags"]).get("Project")
                    == "vpc-networking-lab", f"Wrong {role} project tag")
            require(len(instance["SecurityGroups"]) == 1, f"Unexpected {role} groups")
            groups[role] = instance["SecurityGroups"][0]["GroupId"]
            require(instance.get("PublicIpAddress") == (self.inventory["proxy_ip"]
                    if role == "web" else None), f"Unexpected {role} public address")
        tables = self.aws("ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc}")
        for role, identifier in self.inventory["instance_ids"].items():
            subnet = instances[identifier]["SubnetId"]
            associated = [t for t in tables["RouteTables"]
                          if any(a.get("SubnetId") == subnet for a in t["Associations"])]
            require(len(associated) == 1, f"Missing explicit {role} route association")
            defaults = [r for r in associated[0]["Routes"] if r.get("DestinationCidrBlock") == "0.0.0.0/0"]
            target = {"web": ("GatewayId", self.inventory["internet_gateway_id"]),
                      "app": ("NatGatewayId", self.inventory["nat_gateway_id"])}
            if role == "db":
                require(not defaults, "Database bootstrap route is still present")
            else:
                field, value = target[role]
                require(len(defaults) == 1 and defaults[0].get(field) == value
                        and defaults[0]["State"] == "active", f"Wrong {role} default")
        security = self.aws("ec2", "describe-security-groups", "--group-ids", *groups.values())
        by_id = {g["GroupId"]: g for g in security["SecurityGroups"]}
        operator = permission_rules(by_id[groups["web"]]["IpPermissions"])
        operator_cidrs = {peer for protocol, port, peer in operator
                          if protocol == "tcp" and port in (22, 80)}
        require(len(operator_cidrs) == 1, "Unexpected operator rules")
        cidr = next(iter(operator_cidrs))
        require(ipaddress.IPv4Network(cidr).prefixlen == 32, "Operator ingress must be /32")
        expected_in = {"web": {("tcp", 22, cidr), ("tcp", 80, cidr)},
                       "app": {("tcp", 22, groups["web"]), ("tcp", 8080, groups["web"])},
                       "db": {("tcp", 22, groups["web"]), ("tcp", 5432, groups["app"])}}
        expected_out = {"web": {("tcp", 8080, groups["app"]), ("tcp", 22, groups["app"]),
                                ("tcp", 22, groups["db"]), ("tcp", 80, "0.0.0.0/0"),
                                ("tcp", 443, "0.0.0.0/0")},
                        "app": {("tcp", 5432, groups["db"]), ("tcp", 80, "0.0.0.0/0"),
                                ("tcp", 443, "0.0.0.0/0")}, "db": set()}
        for role, identifier in groups.items():
            require(permission_rules(by_id[identifier]["IpPermissions"]) == expected_in[role],
                    f"Unexpected {role} ingress")
            require(permission_rules(by_id[identifier]["IpPermissionsEgress"]) == expected_out[role],
                    f"Unexpected {role} egress")
        return {"routes": "explicit public/IGW, private/NAT, isolated/local",
                "security_rules": "6 ingress, 8 egress; operator /32; no DB egress",
                "network_acl": "default NACL allows IPv4 in both directions",
                "private_hosts_have_no_public_ip": True}

    def traffic(self):
        require(set(self.inventory["instance_ids"]) == set(ROLES), "Traffic checks require all three instances")
        result = {"topology": self.topology()}
        proxy = self.inventory["proxy_ip"]
        for endpoint in ("health", "message"):
            with urlopen(f"http://{proxy}/{endpoint}", timeout=8) as response:
                require(response.status == 200, f"Proxy {endpoint} did not return 200")
                body = json.load(response)
            expected = {"status": "ok"} if endpoint == "health" else {
                "message": "Hello from the isolated subnet"}
            require(body == {**expected, "peer": ROLES["web"]}, f"Unexpected proxy {endpoint}")
            result[f"proxy_{endpoint}"] = {"http_status": 200, "body": body}
        web = self.python("web", '''import json, socket
from urllib.request import urlopen
with urlopen('http://10.0.2.10:8080/health', timeout=5) as response:
    healthy = json.load(response)['status'] == 'ok'
try:
    connection = socket.create_connection(('10.0.3.10', 5432), timeout=2)
except (TimeoutError, OSError):
    blocked = True
else:
    connection.close()
    blocked = False
print(json.dumps({'app_healthy': healthy, 'database_connection_blocked': blocked}))
''')
        require(web == {"app_healthy": True, "database_connection_blocked": True}, "Web access matrix failed")
        result["web"] = web
        app = self.python("app", '''import json, socket
from urllib.request import urlopen
with urlopen('https://checkip.amazonaws.com/', timeout=8) as response:
    observed = response.read().decode().strip()
destination = socket.gethostbyname('checkip.amazonaws.com')
with socket.create_connection((destination, 443), timeout=5):
    pass
with urlopen('http://checkip.amazonaws.com/', timeout=8) as response:
    http_observed = response.read().decode().strip()
print(json.dumps({'observed': observed, 'http_observed': http_observed, 'destination': destination}))
''')
        require(app["observed"] == app["http_observed"] == self.inventory["nat_ip"], "Wrong app NAT source")
        result["app_external"] = {"http_and_https": True, "source_matches_nat_eip": True,
                                  "database_request_succeeded": True}
        destination = str(ipaddress.IPv4Address(app["destination"]))
        db = self.python("db", f'''import json, socket
results = {{}}
for name, address, port in [('external_by_ip', '{destination}', 443), ('new_app_connection', '10.0.2.10', 8080)]:
    try:
        connection = socket.create_connection((address, port), timeout=2)
    except (TimeoutError, OSError):
        results[name] = 'blocked'
    else:
        connection.close()
        results[name] = 'connected'
print(json.dumps(results))
''')
        require(db == {"external_by_ip": "blocked", "new_app_connection": "blocked"}, "Database isolation failed")
        result["database"] = db
        # A healthy listener distinguishes access denial from an unused closed port.
        self.ssh("web", "sudo systemd-run --quiet --collect --unit=vpc-lab-unused-port "
                 "--property=RuntimeMaxSec=30 /usr/bin/python3 -m http.server 18080 --bind 10.0.1.10")
        try:
            self.python("web", '''import json, time
from urllib.request import urlopen
for attempt in range(10):
    try:
        with urlopen('http://10.0.1.10:18080/', timeout=2) as response:
            assert response.status == 200
        break
    except OSError:
        if attempt == 9:
            raise
        time.sleep(0.2)
print(json.dumps({'listener_healthy': True}))
''')
            try:
                connection = socket.create_connection((proxy, 18080), timeout=2)
            except (TimeoutError, OSError):
                result["external_unused_port"] = {"listener_healthy_on_web": True, "connection": "blocked"}
            else:
                connection.close()
                raise RuntimeError("Unused proxy port is externally reachable")
        finally:
            self.ssh("web", "sudo systemctl stop vpc-lab-unused-port.service")
        return result

    def destroyed(self):
        checks = [
            ("instances", "describe-instances", "--instance-ids", list(self.inventory["instance_ids"].values()),
             "InvalidInstanceID.NotFound", lambda data: all(i["State"]["Name"] == "terminated"
              for r in data["Reservations"] for i in r["Instances"])),
            ("volumes", "describe-volumes", "--volume-ids", list(self.inventory["root_volume_ids"].values()),
             "InvalidVolume.NotFound", lambda data: not data["Volumes"]),
            ("elastic_ips", "describe-addresses", "--allocation-ids", list(self.inventory["elastic_ip_ids"].values()),
             "InvalidAllocationID.NotFound", lambda data: not data["Addresses"]),
            ("nat_gateway", "describe-nat-gateways", "--nat-gateway-ids", [self.inventory["nat_gateway_id"]],
             "NatGatewayNotFound", lambda data: all(n["State"] == "deleted" for n in data["NatGateways"])),
            ("internet_gateway", "describe-internet-gateways", "--internet-gateway-ids", [self.inventory["internet_gateway_id"]],
             "InvalidInternetGatewayID.NotFound", lambda data: not data["InternetGateways"]),
            ("vpc", "describe-vpcs", "--vpc-ids", [self.inventory["vpc_id"]],
             "InvalidVpcID.NotFound", lambda data: not data["Vpcs"]),
        ]
        result = {}
        # Query each ID individually: one missing ID must not hide another live one.
        for name, operation, flag, identifiers, missing, absent in checks:
            for identifier in identifiers:
                data = self.aws("ec2", operation, flag, identifier, missing=missing)
                require(data is None or absent(data), f"Saved {name} still exist")
            result[name] = "removed"
        return result


def permission_rules(permissions):
    rules = set()
    for permission in permissions:
        require(permission.get("IpProtocol") == "tcp" and permission["FromPort"] == permission["ToPort"],
                "Unexpected protocol or port range")
        require(not permission.get("Ipv6Ranges") and not permission.get("PrefixListIds"), "Unexpected IPv6 or prefix rule")
        peers = [r["CidrIp"] for r in permission["IpRanges"]] + [r["GroupId"] for r in permission["UserIdGroupPairs"]]
        for peer in peers:
            rules.add(("tcp", permission["FromPort"], peer))
    return rules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("traffic", "destroyed"))
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--ssh-config", type=Path)
    args = parser.parse_args()
    if args.mode == "traffic" and args.ssh_config is None:
        parser.error("traffic requires --ssh-config with pinned known host keys")
    started = time.monotonic()
    try:
        lab = Lab(json.loads(args.inventory.read_text(encoding="utf-8-sig")), args.profile, args.ssh_config)
        lab.identity()
        results = lab.traffic() if args.mode == "traffic" else lab.destroyed()
        print(json.dumps({"mode": args.mode, "status": "pass", "elapsed_seconds": round(time.monotonic() - started, 3),
                          "results": results}, indent=2))
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
