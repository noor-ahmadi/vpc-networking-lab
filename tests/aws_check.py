#!/usr/bin/env python3
"""Offline regression: cleanup must fail for live resources or unreadable AWS APIs."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aws.check import Lab, permission_rules  # noqa: E402


inventory = {
    "account_id": "123456789012", "region": "us-east-2",
    "vpc_id": "vpc-00000000000000001", "internet_gateway_id": "igw-00000000000000002",
    "nat_gateway_id": "nat-00000000000000003", "proxy_ip": "203.0.113.10", "nat_ip": "203.0.113.20",
    "instance_ids": {role: f"i-{i:017x}" for i, role in enumerate(("web", "app", "db"), 1)},
    "root_volume_ids": {role: f"vol-{i:017x}" for i, role in enumerate(("web", "app", "db"), 11)},
    "elastic_ip_ids": {role: f"eipalloc-{i:017x}" for i, role in enumerate(("web", "nat"), 21)},
}


def run_case(live=None, error=None, account="123456789012", saved=inventory):
    calls = []

    def command(arguments, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        operation = arguments[2]
        calls.append(arguments)
        if operation == "get-caller-identity":
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"Account": account}), "")
        if error:
            return subprocess.CompletedProcess(arguments, 1, "", f"An error occurred ({error}) when calling AWS")
        identifier = arguments[4]
        is_live = identifier == live
        if operation == "describe-instances":
            body = {"Reservations": [{"Instances": [{"State": {"Name": "running" if is_live else "terminated"}}]}]}
        elif operation == "describe-nat-gateways":
            body = {"NatGateways": [{"State": "available" if is_live else "deleted"}]}
        else:
            field, missing = {
                "describe-volumes": ("Volumes", "InvalidVolume.NotFound"),
                "describe-addresses": ("Addresses", "InvalidAllocationID.NotFound"),
                "describe-internet-gateways": ("InternetGateways", "InvalidInternetGatewayID.NotFound"),
                "describe-vpcs": ("Vpcs", "InvalidVpcID.NotFound"),
            }[operation]
            if not is_live:
                return subprocess.CompletedProcess(arguments, 1, "", f"An error occurred ({missing}) when calling AWS")
            body = {field: [{"Id": identifier}]}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(body), "")

    with patch("aws.check.subprocess.run", side_effect=command):
        lab = Lab(saved, "offline")
        lab.identity()
        result = lab.destroyed()
    return result, calls


result, calls = run_case()
assert len(result) == 6 and set(result.values()) == {"removed"}
assert len(calls) == 12, "Every saved resource must be checked individually, plus identity"
partial = {**inventory,
           "instance_ids": {role: inventory["instance_ids"][role] for role in ("app", "db")},
           "root_volume_ids": {role: inventory["root_volume_ids"][role] for role in ("app", "db")}}
assert len(run_case(saved=partial)[1]) == 10, "A partial deployment still needs cleanup checks"
try:
    Lab(partial, "offline").traffic()
except RuntimeError as error:
    assert "all three instances" in str(error)
else:
    raise AssertionError("Traffic checks accepted an incomplete deployment")
# The first volume is missing while the second is live. A batch NotFound must
# never conceal that second volume. Exercise every resource category too.
for identifier in (inventory["instance_ids"]["app"], inventory["root_volume_ids"]["app"],
                   inventory["elastic_ip_ids"]["nat"], inventory["nat_gateway_id"],
                   inventory["internet_gateway_id"], inventory["vpc_id"]):
    try:
        run_case(live=identifier)
    except RuntimeError as error:
        assert "still exist" in str(error)
    else:
        raise AssertionError("Live saved resource was incorrectly reported removed")
for code in ("UnauthorizedOperation", "ExpiredToken", "InvalidInstanceID.Malformed"):
    try:
        run_case(error=code)
    except RuntimeError as error:
        assert code in str(error)
    else:
        raise AssertionError("AWS read error was incorrectly treated as teardown")
try:
    run_case(account="987654321098")
except RuntimeError as error:
    assert "account differs" in str(error)
else:
    raise AssertionError("Wrong AWS account was accepted")
rule = {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
        "IpRanges": [], "UserIdGroupPairs": [{"GroupId": "sg-app"}], "Ipv6Ranges": [], "PrefixListIds": []}
assert permission_rules([rule]) == {("tcp", 5432, "sg-app")}
for change in ({"IpProtocol": "-1"}, {"ToPort": 65535}, {"Ipv6Ranges": [{"CidrIpv6": "::/0"}]}):
    try:
        permission_rules([{**rule, **change}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("Broad security rule was accepted")
print("PASS: per-ID teardown, wrong-account/read-error rejection, and narrow security rules")
