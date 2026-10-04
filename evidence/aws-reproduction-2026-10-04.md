# AWS reproduction: October 4, 2026

Observed with AWS APIs, SSH, HTTP requests, and an app-side packet capture.
Full inventories, account/address mappings, state, plans, and startup logs
remain private. These results apply to this disposable deployment.

## Footprint and startup

- Region/AZ: `us-east-2` / `us-east-2a`; Canonical Ubuntu 24.04 amd64 server
  image `ami-0fa99aa8f97f9e30b` (20260923).
- Terraform 1.16.5, AWS provider 6.67.0, AWS CLI 2.37.5.
- Three `t3.micro` hosts, 24 GiB total encrypted gp3 root storage, one zonal
  NAT gateway, two Elastic IPs, and three explicit subnet route tables.
- All three cloud-init runs and completion markers passed. SSH host keys
  matched authenticated EC2 console fingerprints; private keys stayed local.
- Live bootstrap exposed a Nginx wildcard-listener reload failure and an
  unquoted PostgreSQL address. Fresh hosts passed after a Nginx restart and
  a correctly quoted PostgreSQL drop-in. Terraform's EIP-computed web address
  flag also needed to inherit the subnet setting to avoid refresh replacement.
- Isolation removed exactly the DB NAT default and its two HTTP/HTTPS
  egress rules. All three instance IDs and root-volume IDs were retained.
  A subsequent refreshed Terraform plan reported **no changes**.

## Final access matrix

`aws/check.py traffic` passed in **29.296 seconds** after isolation. AWS APIs
confirmed the expected route targets, exactly six ingress/eight egress rules,
operator `/32`, no private-host public addresses, and permissive default NACL.

| Source → destination | Observed result |
| --- | --- |
| Operator → web TCP 80 `/health` | HTTP 200, `status=ok`, app saw peer `10.0.1.10` |
| Operator → web `/message` → app → DB | HTTP 200, `Hello from the isolated subnet` |
| Web → app TCP 8080 | Healthy HTTP service |
| Web → DB TCP 5432 | New connection blocked; DB query from app had succeeded |
| App → external HTTP and HTTPS | Both observed source addresses matched the NAT EIP |
| DB → external TCP 443 by IP | New connection blocked; app reached that IP/port |
| DB → app TCP 8080 | New connection blocked; app service was healthy |
| Operator → web TCP 18080 | Blocked while a temporary local listener returned HTTP 200 |

Negative probes used a two-second socket timeout. The observed denials and
AWS configuration establish the access matrix; no packet capture identified
their exact drop locations. AWS platform DNS was not tested as blocked.
The temporary listener was stopped after the check and also had a 30-second
systemd lifetime limit.

Updating the seed row locally on DB produced
`Fresh AWS database query verified on 2026-10-04` through the proxy with HTTP
200. Restoring the seed produced the original message, also with HTTP 200.
The app capture collected 30 packet-summary lines during those requests. A
bounded excerpt shows the separate service connections:

```text
21:48:01.017977 ens5  In  IP 10.0.1.10.52562 > 10.0.2.10.8080: Flags [S]
21:48:01.018006 ens5  Out IP 10.0.2.10.8080 > 10.0.1.10.52562: Flags [S.]
21:48:01.018637 ens5  Out IP 10.0.2.10.50284 > 10.0.3.10.5432: Flags [S]
21:48:01.018910 ens5  In  IP 10.0.3.10.5432 > 10.0.2.10.50284: Flags [S.]
```

This excerpt omits sequence numbers/options; no authentication payload was
dumped. The managed NAT's internal translations were not captured. Its source
mapping was checked using the external echo endpoint and AWS address data.

## Teardown and cost

Terraform deleted all 38 remaining managed resources at **21:50:44 UTC**.
`aws/check.py destroyed` then passed against both the initial and final
inventories, checking each ID separately in the recorded account and region:
**five unique instances terminated, five root volumes deleted, NAT deleted,
two EIPs released, IGW and VPC removed**. The inventories include the web/DB
hosts replaced during startup fixes. Regional project-tag queries at
**21:52:44 UTC** also found zero active instances, volumes, NAT gateways, EIPs,
VPCs, or imported key pairs. Local Terraform state listed no resources.

The session ran from 20:50:54 UTC to completed destroy, about one hour. Ohio
API rates checked before apply were `$0.0104/hour` per Linux `t3.micro`,
`$0.045/hour` plus `$0.045/GB` for zonal NAT, `$0.005/hour` per public IPv4,
and `$0.08/GB-month` for gp3. The steady three-host footprint is about
`$0.08883/hour` using 730 hours/month for storage. This is an estimate,
excluding transfer, taxes, and billing rounding; the final bill was not
observed. See [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/) and the
[regional Price List API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-price-list-query-api.html).

Custom NACL return-port failures and repeatable cloud fault/repair exercises
remain future work. The existing Linux fault results establish that fixture's
behavior separately.
