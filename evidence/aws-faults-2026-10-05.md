# AWS faults: October 5, 2026

Two complete live runs of [the fault runner](../aws/faults.py) passed on a
fresh isolated Ohio deployment. The session took place on October 5 in
America/New_York; UTC records below fall on October 6. Full inventories,
recovery targets, plans, and capture logs remain private.

The footprint matched the [first reproduction](aws-reproduction-2026-10-04.md):
three Ubuntu 24.04 `t3.micro` hosts, one zonal NAT, two EIPs, and three subnet
route tables in `us-east-2a`. All three cloud-init runs and console-verified
SSH host keys passed. Isolation removed exactly three bootstrap resources,
retaining every instance and root disk. The instances/disks also remained
unchanged through both fault runs.

## Observed failures and repairs

| Case | Run 1 / run 2 duration | Observed failure | Healthy control and recovery |
| --- | --- | --- | --- |
| App NAT default deleted | 14.750 / 14.843 s | Fresh app TCP 443 timed out in 3.003 s in both runs; AWS default was absent | `/health` and DB-backed `/message` stayed HTTP 200; external TCP connected after route repair |
| DB security-group port changed to 5433 | 19.406 / 18.672 s | App TCP 5432 timed out in 3.003 s; `/message` returned HTTP 503 | Local DB query and `/health` remained healthy; restoring 5432 on the same rule ID restored HTTP 200 |
| DB NACL app return rule removed | 50.703 / 50.453 s | App TCP 5432 timed out in 3.003 s; `/message` returned HTTP 503 | Custom NACL first passed HTTP 200; restoring its return rule restored HTTP 200 before returning to the default NACL |

Total run times were **176.844 s** and **176.985 s**, including complete live
baseline checks before and after the faults. The security-group case showed
the probed SYN leaving app and absent from DB's capture while DB's local
query succeeded. The NACL case showed that same connection's SYN reaching
DB and its SYN-ACK leaving DB, with no matching SYN-ACK received by app.
The NACL retained web SSH ingress and its separate reply rule throughout.

Run 2's return-port capture excerpt, with sequence/options omitted:

```text
app 02:41:30.928102 ens5 Out IP 10.0.2.10.32849 > 10.0.3.10.5432: Flags [S]
db  02:41:30.928161 ens5 In  IP 10.0.2.10.32849 > 10.0.3.10.5432: Flags [S]
db  02:41:30.928191 ens5 Out IP 10.0.3.10.5432 > 10.0.2.10.32849: Flags [S.]
app 02:41:31.986067 ens5 Out IP 10.0.2.10.32849 > 10.0.3.10.5432: Flags [S]
```

The checker matched all four IP/port fields and rejected evidence from a
different connection. This supports the controlled return-rule diagnosis;
the guest captures do not expose the NACL's internal processing. Capture
filters retained only SYN/RST headers between the private app and DB, bounded
to ten seconds/twelve packet lines per host. No authentication payloads or
operator/public address mappings were published. Managed NAT internals were
not captured; the route fault used a previously healthy external IP and AWS
route data.

## Recovery and cleanup

Both runs restored their original policies and removed their temporary NACL.
A separate live `--restore-only` invocation loaded the saved journal and
passed the complete baseline again in **70.796 s**. Offline regression cases
covered interrupted-run restoration, preserving an uncompleted journal,
attempting other repairs after one fails, rejecting another account's
journal, and rejecting misleading packet matches.

A refreshed Terraform plan reported **no changes** after all checks. Destroy
removed the 38 remaining managed resources at **02:48:45 UTC**, after a
session beginning at **02:27:05 UTC**, about 22 minutes. Independent per-ID
AWS checks passed in **29.125 s**: three instances terminated, three root
volumes deleted, NAT deleted, both EIPs released, and IGW/VPC removed.
At **02:50:48 UTC**, separate regional lab-tag queries found zero active
instances, volumes, NAT gateways, EIPs, VPCs, imported key pairs, or custom
NACLs. Local Terraform state listed no resources.

The refreshed regional API estimate was still **$0.08883/hour** for the
steady footprint, using 730 hours/month for gp3 storage. NAT processing,
transfer, taxes, and billing rounding are additional. Final billed cost was
not observed. No cloud deployment or paid fault exercise runs in CI.
