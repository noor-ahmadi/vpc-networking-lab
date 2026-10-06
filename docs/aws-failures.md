# AWS faults and repairs

Run three controlled faults on the isolated deployment described in
[the AWS guide](aws.md). These checks make real AWS changes and stay outside
CI. Use one operator session at a time, retain the private inventory, and
destroy the demo after checking recovery.

```sh
python3 aws/faults.py --inventory aws/inventory.private.json \
  --profile <your AWS profile> --ssh-config <your pinned SSH config> \
  --recovery-file aws/recovery.private.json
```

The runner first verifies the account, exact baseline routes/security rules,
default NACL, and live traffic. It records recovery targets atomically before
changing AWS settings. All connections are fresh. Each result includes source,
destination, expected/observed behavior, timing, controls, evidence, and repair.

| Fault | Expected failure | Control and repair |
| --- | --- | --- |
| Delete app's NAT default route | App cannot open TCP 443 to an external IP it just reached | Proxy health and DB query stay healthy; recreate the same NAT route and reconnect |
| Change DB group's permitted port from 5432 to 5433 | App's TCP 5432 times out; proxy `/message` returns 503 | Local DB query and proxy health stay healthy; restore 5432 on the same rule ID and query again |
| Remove DB NACL's app return-port rule | DB receives SYN and sends SYN-ACK, which app does not receive; `/message` returns 503 | Verify the custom NACL works first, restore the return rule, query again, then restore the default NACL |

The database-port case changes a rule in place, preserving Terraform's rule
ID. Captures on app and DB show the same app SYN leaving app and missing from
DB. The observed change, healthy database, and successful repair support the
security-group diagnosis; there is no capture inside AWS's filtering layer.

The custom NACL applies only to the DB subnet. Its ingress permits TCP 22
from web and TCP 5432 from app. Separate egress rules permit TCP 1024–65535
to web for SSH replies and to app for database replies. Removing only the app
egress rule keeps the SSH recovery path available. The capture check requires
the same IP/port tuple in app's SYN, DB's received SYN and sent SYN-ACK, and
requires that SYN-ACK to be absent at app. A different connection's packets
cannot satisfy the check.

NACLs require explicit response rules, whereas security groups track
connections. The ephemeral range here covers the Linux client ports used by
this exercise; it does not permit new traffic through a security group.
[AWS custom NACL rules and ephemeral ports](https://docs.aws.amazon.com/vpc/latest/userguide/custom-network-acl.html).

Each capture filters only TCP SYN/RST headers, runs at most ten seconds,
and records at most twelve packet lines per host. Captures are saved beside
the ignored recovery journal. Authentication payloads and operator/public
addresses are excluded by the filter. The managed NAT has no guest capture
point, so its missing-route case uses AWS route data and bounded app probes.

## Interrupted runs

Normal exceptions restore the original settings in `finally`. The original
database rule and private default are checked before repair; unrelated
changes are rejected. Every repair is attempted even if another repair fails.
An uncompleted journal prevents another fault run from overwriting it.

After a killed process or interrupted connection, keep the same inventory,
SSH config, profile, and recovery file, then run:

```sh
python3 aws/faults.py --inventory aws/inventory.private.json \
  --profile <your AWS profile> --ssh-config <your pinned SSH config> \
  --recovery-file aws/recovery.private.json --restore-only
```

Recovery checks its account/region/VPC against the inventory and verifies the
live instance, subnet, and group targets. Only the temporary NACL with this
run's project/purpose/run tags is detached and removed. Its tags are recorded
before creation, so an API timeout during creation is recoverable too.
The complete baseline traffic checks run after restoration. Verify a no-change
Terraform plan, then follow the AWS guide's teardown and independent cleanup
checks. A recovery failure requires inspection of the preserved artifacts;
it is never reported as successful cleanup.
