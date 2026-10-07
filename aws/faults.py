#!/usr/bin/env python3
"""Break and repair three network paths in the deployed, isolated lab."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aws.check import ALIASES, Lab, ROLES, require


def handshakes(text):
    packets = {"S": set(), "S.": set()}
    pattern = r"IP (10\.0\.[123]\.10)\.(\d+) > (10\.0\.[123]\.10)\.(\d+): Flags \[(S\.?)\]"
    for source, sport, destination, dport, flags in re.findall(pattern, text):
        packets[flags].add((source, int(sport), destination, int(dport)))
    return packets


def reply_drop(app, db, source_port):
    request = (ROLES["app"], source_port, ROLES["db"], 5432)
    response = (ROLES["db"], 5432, ROLES["app"], source_port)
    require(request in app["S"] and request in db["S"] and response in db["S."]
            and response not in app["S."], "Capture does not establish the missing return packet")
    return {"same_tuple": True, "app_sent_syn": True, "db_received_syn": True,
            "db_sent_syn_ack": True, "app_received_syn_ack": False}


class Faults(Lab):
    def __init__(self, inventory, profile, ssh_config, journal):
        super().__init__(inventory, profile, ssh_config)
        require(journal.name.endswith(".private.json"), "Recovery journal must end in .private.json")
        self.journal = journal
        self.context = {}

    def save(self, state):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal.with_suffix(".tmp.private.json")
        temporary.write_text(json.dumps({"state": state, "account_id": self.inventory["account_id"],
            "region": self.inventory["region"], "vpc_id": self.inventory["vpc_id"],
            "context": self.context}, indent=2), encoding="utf-8")
        temporary.replace(self.journal)

    def discover(self):
        records = self.aws("ec2", "describe-instances", "--instance-ids",
                           *self.inventory["instance_ids"].values())["Reservations"]
        instances = {i["InstanceId"]: i for r in records for i in r["Instances"]}
        app = instances[self.inventory["instance_ids"]["app"]]
        db = instances[self.inventory["instance_ids"]["db"]]
        tables = self.aws("ec2", "describe-route-tables", "--filters",
                          f"Name=association.subnet-id,Values={app['SubnetId']}")["RouteTables"]
        require(len(tables) == 1, "App needs one explicit route table")
        groups = {"app": app["SecurityGroups"][0]["GroupId"], "db": db["SecurityGroups"][0]["GroupId"]}
        rules = self.aws("ec2", "describe-security-group-rules", "--filters",
                         f"Name=group-id,Values={groups['db']}")["SecurityGroupRules"]
        sql = [r for r in rules if not r["IsEgress"] and r.get("FromPort") == r.get("ToPort") == 5432
               and r.get("IpProtocol") == "tcp" and r.get("ReferencedGroupInfo", {}).get("GroupId") == groups["app"]]
        require(len(sql) == 1 and sql[0].get("Description") == "database_sql", "Unexpected database rule")
        acls = self.aws("ec2", "describe-network-acls", "--filters",
                        f"Name=vpc-id,Values={self.inventory['vpc_id']}")["NetworkAcls"]
        require(len(acls) == 1 and acls[0]["IsDefault"], "Start with only the default NACL")
        self.context = {"route_table": tables[0]["RouteTableId"], "app_subnet": app["SubnetId"],
                        "db_subnet": db["SubnetId"], "app_group": groups["app"], "db_group": groups["db"],
                        "sql_rule": sql[0]["SecurityGroupRuleId"], "default_acl": acls[0]["NetworkAclId"],
                        "run": secrets.token_hex(8)}
        # ponytail: one operator; add a journal lock before allowing concurrent runs.
        # Record recovery targets before the first write, including create timeouts.
        self.save("armed")

    def validate_recovery(self):
        records = self.aws("ec2", "describe-instances", "--instance-ids",
                           *self.inventory["instance_ids"].values())["Reservations"]
        instances = {i["InstanceId"]: i for r in records for i in r["Instances"]}
        for role in ("app", "db"):
            instance = instances[self.inventory["instance_ids"][role]]
            tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
            require(instance["VpcId"] == self.inventory["vpc_id"] and instance["State"]["Name"] == "running"
                    and instance["PrivateIpAddress"] == ROLES[role] and tags.get("Project") == "vpc-networking-lab"
                    and instance["SubnetId"] == self.context[f"{role}_subnet"]
                    and [g["GroupId"] for g in instance["SecurityGroups"]] == [self.context[f"{role}_group"]],
                    "Recovery targets differ from the deployed lab")

    def proxy(self, endpoint, expected=200):
        started = time.monotonic()
        try:
            response = urlopen(f"http://{self.inventory['proxy_ip']}/{endpoint}", timeout=8)
        except HTTPError as error:
            response = error
        with response:
            body = json.load(response)
            status = response.status
        expected_body = {"error": "database unavailable"} if expected == 503 else (
            {"status": "ok"} if endpoint == "health" else {"message": "Hello from the isolated subnet"})
        require(status == expected and body == {**expected_body, "peer": ROLES["web"]}, "Unexpected proxy response")
        return {"http_status": status, "elapsed_seconds": round(time.monotonic() - started, 3), "body": body}

    def tcp(self, destination, port):
        address = str(ipaddress.IPv4Address(destination))
        return self.python("app", f'''import json, socket, time
connection = socket.socket()
connection.settimeout(3)
connection.bind(('10.0.2.10', 0))
result = {{'source_port': connection.getsockname()[1]}}
started = time.monotonic()
try:
    connection.connect(('{address}', {port}))
    result['outcome'] = 'connected'
except OSError as error:
    result.update(outcome='timeout' if isinstance(error, TimeoutError) else type(error).__name__, errno=error.errno)
finally:
    connection.close()
result['elapsed_seconds'] = round(time.monotonic() - started, 3)
print(json.dumps(result))
''')

    def sql_port(self, port):
        rule = {"SecurityGroupRuleId": self.context["sql_rule"], "SecurityGroupRule": {
            "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
            "ReferencedGroupId": self.context["app_group"], "Description": "database_sql"}}
        self.aws("ec2", "modify-security-group-rules", "--group-id", self.context["db_group"],
                 "--security-group-rules", json.dumps([rule]))

    def restore_sql(self):
        rules = self.aws("ec2", "describe-security-group-rules", "--security-group-rule-ids",
                         self.context["sql_rule"])["SecurityGroupRules"]
        require(len(rules) == 1, "Recovery database rule is missing")
        rule = rules[0]
        require(rule["GroupId"] == self.context["db_group"] and not rule["IsEgress"]
                and rule["IpProtocol"] == "tcp" and rule["FromPort"] == rule["ToPort"]
                and rule["FromPort"] in (5432, 5433)
                and rule.get("ReferencedGroupInfo", {}).get("GroupId") == self.context["app_group"]
                and rule.get("Description") == "database_sql", "Recovery rule was changed outside this exercise")
        if rule["FromPort"] != 5432:
            self.sql_port(5432)

    def restore_route(self):
        tables = self.aws("ec2", "describe-route-tables", "--route-table-ids",
                          self.context["route_table"])["RouteTables"]
        require(len(tables) == 1 and tables[0]["VpcId"] == self.inventory["vpc_id"]
                and any(a.get("SubnetId") == self.context["app_subnet"] for a in tables[0]["Associations"]),
                "Recovery route table differs from the lab")
        defaults = [r for r in tables[0]["Routes"] if r.get("DestinationCidrBlock") == "0.0.0.0/0"]
        if defaults:
            require(len(defaults) == 1 and defaults[0].get("NatGatewayId") == self.inventory["nat_gateway_id"],
                    "Private default was changed outside this exercise")
        else:
            self.aws("ec2", "create-route", "--route-table-id", self.context["route_table"],
                     "--destination-cidr-block", "0.0.0.0/0", "--nat-gateway-id", self.inventory["nat_gateway_id"])

    def nacls(self):
        return self.aws("ec2", "describe-network-acls", "--filters",
                        f"Name=vpc-id,Values={self.inventory['vpc_id']}")["NetworkAcls"]

    def owned_acl(self, acl):
        tags = {t["Key"]: t["Value"] for t in acl.get("Tags", [])}
        return not acl["IsDefault"] and acl["VpcId"] == self.inventory["vpc_id"] and tags == {
            "Project": "vpc-networking-lab", "Purpose": "return-port-fault", "Run": self.context["run"]}

    def restore_acl(self):
        acls = self.nacls()
        defaults = [a for a in acls if a["IsDefault"] and a["NetworkAclId"] == self.context["default_acl"]]
        require(len(defaults) == 1, "Original default NACL is missing")
        owned = [a for a in acls if self.owned_acl(a)]
        for acl in owned:
            require(all(a["SubnetId"] == self.context["db_subnet"] for a in acl["Associations"]),
                    "Exercise NACL acquired an unrelated association")
            for association in acl["Associations"]:
                self.aws("ec2", "replace-network-acl-association", "--association-id",
                         association["NetworkAclAssociationId"], "--network-acl-id", self.context["default_acl"])
            self.aws("ec2", "delete-network-acl", "--network-acl-id", acl["NetworkAclId"])
        associated = [a for a in self.nacls() if any(s["SubnetId"] == self.context["db_subnet"]
                       for s in a["Associations"])]
        require(len(associated) == 1 and associated[0]["NetworkAclId"] == self.context["default_acl"],
                "Database did not return to its original NACL")

    def restore(self):
        self.validate_recovery()
        errors = []
        for repair in (self.restore_acl, self.restore_sql, self.restore_route):
            try:
                repair()
            except (RuntimeError, KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        require(not errors, "Recovery failed: " + "; ".join(errors))
        self.topology()
        self.save("restored")

    def capture_probe(self, name):
        directory = self.journal.parent / "captures"
        directory.mkdir(exist_ok=True)
        jobs = []
        handles = []
        try:
            for role in ("app", "db"):
                path = directory / f"{self.context['run']}-{name}-{role}.log"
                error_path = path.with_suffix(".stderr.log")
                output, errors = path.open("w", encoding="utf-8"), error_path.open("w", encoding="utf-8")
                handles.extend((output, errors))
                command = ("sudo timeout 10 tcpdump -nn -l -i any -c 12 "
                           "'tcp and host 10.0.2.10 and host 10.0.3.10 and port 5432 "
                           "and (tcp[tcpflags] & (tcp-syn|tcp-rst) != 0)'")
                process = subprocess.Popen(["ssh", "-F", Path(self.ssh_config).resolve().as_posix(),
                    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                    ALIASES[role], command], stdout=output, stderr=errors)
                jobs.append((role, path, error_path, process))
            deadline = time.monotonic() + 8
            while not all("listening on" in error.read_text(encoding="utf-8") for _, _, error, _ in jobs):
                require(time.monotonic() < deadline and all(p.poll() is None for _, _, _, p in jobs),
                        "Packet capture was not ready")
                time.sleep(0.1)
            probe = self.tcp(ROLES["db"], 5432)
            require(probe["outcome"] == "timeout", "Database connection did not time out")
            health, message = self.proxy("health"), self.proxy("message", 503)
        finally:
            failures = []
            for _, _, _, process in jobs:
                try:
                    code = process.wait(timeout=16)
                    if code not in (0, 124):
                        failures.append("Capture exited unsuccessfully")
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    failures.append("Capture exceeded its deadline")
            for handle in handles:
                handle.close()
            require(not failures, "; ".join(failures))
        packets = {role: handshakes(path.read_text(encoding="utf-8")) for role, path, _, _ in jobs}
        return probe, health, message, packets

    def private_default(self):
        address = self.python("app", "import json, socket; print(json.dumps(socket.gethostbyname('checkip.amazonaws.com')))")
        require(self.tcp(address, 443)["outcome"] == "connected", "External TCP control is unhealthy")
        started = time.monotonic()
        try:
            self.aws("ec2", "delete-route", "--route-table-id", self.context["route_table"],
                     "--destination-cidr-block", "0.0.0.0/0")
            probe = self.tcp(address, 443)
            require(probe["outcome"] == "timeout" or probe.get("errno") in (101, 113),
                    "Missing-route probe did not fail as expected")
            health, message = self.proxy("health"), self.proxy("message")
            tables = self.aws("ec2", "describe-route-tables", "--route-table-ids", self.context["route_table"])
            require(not any(r.get("DestinationCidrBlock") == "0.0.0.0/0"
                            for r in tables["RouteTables"][0]["Routes"]), "Private default is still present")
        finally:
            self.restore_route()
        require(self.tcp(address, 443)["outcome"] == "connected", "Private egress did not recover")
        return {"source": "app", "destination": "known healthy external IPv4:443", "expected": "new TCP fails",
                "observed": probe, "internal_health": health, "internal_message": message,
                "evidence": "AWS private default absent; local service path remained healthy",
                "recovered": True, "elapsed_seconds": round(time.monotonic() - started, 3)}

    def database_group(self):
        require(self.tcp(ROLES["db"], 5432)["outcome"] == "connected", "Database TCP control is unhealthy")
        require(self.ssh("db", "sudo -u postgres psql -v ON_ERROR_STOP=1 -At -d vpc_lab "
                         "-c 'SELECT message FROM demo_message WHERE id = 1'").strip()
                == "Hello from the isolated subnet", "Database local query is unhealthy")
        started = time.monotonic()
        try:
            self.sql_port(5433)
            probe, health, message, packets = self.capture_probe("security-group")
            request = (ROLES["app"], probe["source_port"], ROLES["db"], 5432)
            require(request in packets["app"]["S"] and request not in packets["db"]["S"],
                    "Security-group capture did not show the missing ingress packet")
            evidence = {"same_tuple": True, "app_sent_syn": True, "db_received_syn": False,
                        "database_local_query_healthy": True, "allowed_port_changed_to": 5433}
        finally:
            self.restore_sql()
        recovery = self.proxy("message")
        return {"source": "app", "destination": "10.0.3.10:5432", "expected": "TCP timeout; HTTP 503",
                "observed": probe, "internal_health": health, "proxy_message": message,
                "evidence": evidence, "recovered": recovery,
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    def acl_rule(self, acl, egress, number, address, start, end):
        self.aws("ec2", "create-network-acl-entry", "--network-acl-id", acl, "--rule-number", str(number),
                 "--protocol", "6", "--rule-action", "allow", "--egress" if egress else "--ingress",
                 "--cidr-block", address + "/32", "--port-range", f"From={start},To={end}")

    def check_acl(self, identifier, return_allowed):
        acls = self.aws("ec2", "describe-network-acls", "--network-acl-ids", identifier)["NetworkAcls"]
        require(len(acls) == 1 and self.owned_acl(acls[0])
                and [a["SubnetId"] for a in acls[0]["Associations"]] == [self.context["db_subnet"]],
                "Unexpected exercise NACL ownership or association")
        expected = {(False, 100, ROLES["web"] + "/32", 22, 22),
                    (False, 110, ROLES["app"] + "/32", 5432, 5432),
                    (True, 100, ROLES["web"] + "/32", 1024, 65535)}
        if return_allowed:
            expected.add((True, 110, ROLES["app"] + "/32", 1024, 65535))
        actual = set()
        for rule in acls[0]["Entries"]:
            if rule["RuleNumber"] == 32767:
                require(rule["RuleAction"] == "deny", "Unexpected implicit NACL rule")
                continue
            require(rule["Protocol"] == "6" and rule["RuleAction"] == "allow" and "CidrBlock" in rule,
                    "Unexpected exercise NACL rule")
            actual.add((rule["Egress"], rule["RuleNumber"], rule["CidrBlock"],
                        rule["PortRange"]["From"], rule["PortRange"]["To"]))
        require(actual == expected, "Exercise NACL does not match the stage")

    def return_ports(self):
        started = time.monotonic()
        try:
            tags = [{"Key": "Project", "Value": "vpc-networking-lab"},
                    {"Key": "Purpose", "Value": "return-port-fault"}, {"Key": "Run", "Value": self.context["run"]}]
            acl = self.aws("ec2", "create-network-acl", "--vpc-id", self.inventory["vpc_id"],
                "--tag-specifications", json.dumps([{"ResourceType": "network-acl", "Tags": tags}]))["NetworkAcl"]["NetworkAclId"]
            self.acl_rule(acl, False, 100, ROLES["web"], 22, 22)
            self.acl_rule(acl, False, 110, ROLES["app"], 5432, 5432)
            self.acl_rule(acl, True, 100, ROLES["web"], 1024, 65535)
            self.acl_rule(acl, True, 110, ROLES["app"], 1024, 65535)
            default = [a for a in self.nacls() if a["NetworkAclId"] == self.context["default_acl"]][0]
            associations = [a for a in default["Associations"] if a["SubnetId"] == self.context["db_subnet"]]
            require(len(associations) == 1, "Database default NACL association is missing")
            self.aws("ec2", "replace-network-acl-association", "--association-id",
                     associations[0]["NetworkAclAssociationId"], "--network-acl-id", acl)
            self.check_acl(acl, True)
            baseline = self.proxy("message")
            self.aws("ec2", "delete-network-acl-entry", "--network-acl-id", acl,
                     "--egress", "--rule-number", "110")
            self.check_acl(acl, False)
            probe, health, message, packets = self.capture_probe("return-ports")
            evidence = reply_drop(packets["app"], packets["db"], probe["source_port"])
            self.acl_rule(acl, True, 110, ROLES["app"], 1024, 65535)
            self.check_acl(acl, True)
            recovery = self.proxy("message")
        finally:
            self.restore_acl()
        return {"source": "app", "destination": "10.0.3.10:5432", "expected": "return SYN-ACK lost; HTTP 503",
                "custom_acl_baseline": baseline, "observed": probe, "internal_health": health,
                "proxy_message": message, "evidence": evidence, "recovered": recovery,
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    def run(self):
        self.identity()
        if self.journal.exists():
            require(json.loads(self.journal.read_text(encoding="utf-8"))["state"] == "restored",
                    "An interrupted run needs --restore-only before another exercise")
        self.traffic()
        self.discover()
        results = {}
        try:
            for name, exercise in (("private_default", self.private_default),
                                   ("database_group", self.database_group), ("return_ports", self.return_ports)):
                results[name] = exercise()
                print(f"PASS: {name}; original setting restored", file=sys.stderr, flush=True)
        finally:
            self.restore()
        self.traffic()
        return results

    def recover(self):
        self.identity()
        saved = json.loads(self.journal.read_text(encoding="utf-8"))
        require(all(saved[key] == self.inventory[key] for key in ("account_id", "region", "vpc_id")),
                "Recovery journal differs from the inventory")
        self.context = saved["context"]
        require(re.fullmatch(r"[0-9a-f]{16}", self.context["run"]), "Invalid recovery run tag")
        self.restore()
        self.traffic()
        return {"baseline_restored": True, "exercise_acl_removed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--ssh-config", type=Path, required=True)
    parser.add_argument("--recovery-file", type=Path, required=True)
    parser.add_argument("--restore-only", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        lab = Faults(json.loads(args.inventory.read_text(encoding="utf-8-sig")), args.profile,
                     args.ssh_config, args.recovery_file)
        result = lab.recover() if args.restore_only else lab.run()
        print(json.dumps({"status": "pass", "mode": "restore" if args.restore_only else "faults",
                          "elapsed_seconds": round(time.monotonic() - started, 3), "results": result}, indent=2))
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}. Keep the inventory and recovery file; use --restore-only before teardown.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
