# AWS reproduction

The flat configuration in [aws/](../aws) defines the cloud version of the
local lab. Terraform 1.16.5 and AWS provider 6.67.0 are pinned. Configuration
tests and rendered startup scripts are checked locally and in CI. A live
deployment in Ohio passed startup, service, NAT, database isolation, and
inventory-based teardown checks on October 4, 2026. See the
[recorded reproduction](../evidence/aws-reproduction-2026-10-04.md).

## Topology and access

One VPC (`10.0.0.0/16`) contains three explicitly associated subnet route
tables in `us-east-2a`. The implicit main table stays local-only. Automatic
public addressing is disabled on every subnet and explicitly disabled on the
private instances. Web inherits its subnet's setting; its provider-computed
address flag becomes true when the separately managed EIP attaches. Leaving
that web flag unset avoids a replacement on the next refresh. Exactly two
Elastic IPs provide the web and NAT public mappings.

| Workload | Private address | External route | New service ingress |
| --- | --- | --- | --- |
| Web / Nginx | `10.0.1.10` | Public table to IGW | TCP 80 from the operator's IPv4 `/32` |
| App / Python | `10.0.2.10` | Private table to NAT | TCP 8080 from the web security group |
| Database / PostgreSQL 16 | `10.0.3.10` | None after bootstrap | TCP 5432 from the app security group |
| Public zonal NAT Gateway | `10.0.1.20` | Public table to IGW | Managed gateway; no instance security group |

The service ports and addresses match Linux. The app and seed SQL are embedded
from the existing project files, and the proxy opens a separate app connection.
Security group references identify permitted workload peers. Separate rules
allow operator SSH to web, then web SSH to app/database for ProxyJump. Web and
app can initiate HTTP/HTTPS externally; the final database has no new TCP
egress rule. Established replies remain allowed by the stateful groups.
AWS-provided DNS and other platform traffic have their own semantics; this is
not an air gap. See [AWS security groups](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-security-groups.html).

The three `t3.micro` instances use a pinned Ubuntu 24.04 amd64 server AMI,
8 GiB encrypted gp3 root disks deleted on termination, IMDSv2, and standard
CPU credits. Standard mode can throttle a busy instance after its credits are
used; it avoids unlimited-mode surplus credit charges. This is a disposable,
single-AZ demonstration, with a private HTTP app and TCP/SCRAM database path.

## Verify without an AWS account

With the pinned Terraform version, Python 3, Bash, ShellCheck, and Ubuntu's
PostgreSQL 16 / `postgresql-common` tools installed:

```sh
bash tests/aws.sh
```

`init` downloads the locked provider; subsequent tests use a mock AWS provider.
The mock `apply` operations create in-memory test state, with no cloud API
changes. Six runs check final topology/access, temporary bootstrap access,
its removal while retaining the DB instance, and rejected client/password/AZ
inputs. Startup scripts for all three roles are rendered with dummy inputs and
checked with Bash and ShellCheck. The native PostgreSQL parser rejects the
old unquoted address and reads the rendered drop-in configuration. Offline
cleanup cases reject live resources, wrong accounts, and AWS read errors.
These checks establish configuration intent; they do not establish that an
EC2 machine booted or that AWS traffic succeeded.

## Prepare a real plan

Authenticate separately using a supported local AWS credential provider. A
browser login can be exposed to Terraform through a separate
[`credential_process` profile](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html).
Keep credentials in the normal AWS configuration outside the checkout. Select
that profile with `AWS_PROFILE`; the Terraform region defaults to `us-east-2`
regardless of the profile's default region.

Before the first apply, select a current
[Canonical Ubuntu 24.04 amd64 server AMI](https://ubuntu.com/aws/docs/aws-how-to/instances/find-ubuntu-images/)
in that region and supply its ID. Confirm its publisher, architecture, and
root-disk requirements. The AMI ID is an explicit input rather than an
automatic latest-image lookup, so a later plan does not silently replace the
database after an image release.

Create `aws/local.tfvars`, which is ignored by Git:

```hcl
ami_id             = "<regional Ubuntu 24.04 amd64 AMI ID>"
operator_cidr      = "<your current public IPv4>/32"
ssh_public_key     = "<your existing RSA or Ed25519 public key>"
bootstrap_database = true
```

Supply `TF_VAR_db_password` privately using `secrets.token_hex(24)` and retain
the same value for the entire demo. The app uses that generated password for
TCP queries and has SELECT access to the seeded table. Terraform marks the
input sensitive, but local state, saved plans, and EC2 app/database user data
contain it. Keep those artifacts private. The web startup script contains no
database password; each private SSH key stays on the operator's machine.

```sh
terraform -chdir=aws init
terraform -chdir=aws plan -var-file=local.tfvars -out=bootstrap.tfplan
terraform -chdir=aws show bootstrap.tfplan
```

Review that concrete plan, current regional prices, intended runtime, and
cleanup inventory before applying it. The configuration uses billable EC2,
disks, NAT, and public IPv4 resources; CI performs no deployment.

## Bootstrap, then isolate

The database has no package-download path in its final network state. The
required `bootstrap_database` input makes the temporary stage explicit:

1. Apply the reviewed bootstrap plan with `bootstrap_database = true`. The
   isolated table temporarily gets a NAT default and the DB group gets TCP
   80/443 egress. Instances wait for route and security-rule dependencies.
   Package operations retry while the public web EIP association finishes.
2. Use SSH ProxyJump to wait for `sudo cloud-init status --wait` on every host.
   Inspect `/var/log/cloud-init-output.log` privately if startup fails. The
   marker `/var/lib/vpc-lab/bootstrap-complete` records script completion, not
   end-to-end readiness. Confirm `/health` and a real `/message` query before
   removing bootstrap access.
3. Set `bootstrap_database = false` in the same private input file, retain
   the same AMI/key/password, and review another saved plan. It should remove
   the temporary default and two egress rules while retaining the instances
   and database. Changing startup content, AMI, or password can replace an
   instance; inspect that before proceeding.
4. Apply that isolation plan and verify the actual routes/rules, successful
   database query, and denied database external connection by IP. Never use
   `false` for the first creation of an unprepared Ubuntu image.

Use local SSH aliases with `User ubuntu` and an `IdentityFile` for both the
proxy and private hosts. Set the proxy's `HostName` from `proxy_ip`, and use
`ProxyJump vpc-lab-proxy` for app/database. For example:

```sshconfig
Host vpc-lab-proxy
    HostName <proxy_ip output>
    User ubuntu
    IdentityFile ~/.ssh/vpc-lab
    UserKnownHostsFile ~/.ssh/vpc-lab-known-hosts
    StrictHostKeyChecking yes
    IdentitiesOnly yes
    ForwardAgent no

Host vpc-lab-app
    HostName 10.0.2.10
    User ubuntu
    IdentityFile ~/.ssh/vpc-lab
    ProxyJump vpc-lab-proxy
    UserKnownHostsFile ~/.ssh/vpc-lab-known-hosts
    StrictHostKeyChecking yes
    IdentitiesOnly yes
    ForwardAgent no

Host vpc-lab-db
    HostName 10.0.3.10
    User ubuntu
    IdentityFile ~/.ssh/vpc-lab
    ProxyJump vpc-lab-proxy
    UserKnownHostsFile ~/.ssh/vpc-lab-known-hosts
    StrictHostKeyChecking yes
    IdentitiesOnly yes
    ForwardAgent no
```

Save these aliases in a local SSH config. Before connecting, compare each
scanned Ed25519 host key's SHA256 fingerprint with its authenticated EC2
console output (`aws ec2 get-console-output --instance-id ... --latest`).
The AWS CLI decodes the console text. Private host keys can be scanned from
the already verified proxy. Add only matching keys to the separate known
hosts file; a replacement requires a new comparison. No agent forwarding or
private-key upload is required. ProxyJump permits the SSH control channel
only; it does not grant the web service TCP 5432 access.

On Windows, Git's OpenSSH worked with the Ubuntu image used here. Its
ProxyJump child requires a POSIX shell; set `SHELL=/usr/bin/sh` and put Git's
`usr/bin` ahead of Windows OpenSSH on `PATH` for the verification process.
Use forward slashes in absolute Windows paths in SSH config and in an AWS
`credential_process` executable path.

## Check the live deployment

Save the inventory before isolation or any replacement, and retain older
inventories until teardown. After applying the isolation plan:

```sh
terraform -chdir=aws output -json resource_inventory > aws/inventory.private.json
python3 aws/check.py traffic --inventory aws/inventory.private.json \
  --profile <your AWS profile> --ssh-config <your local SSH config>
```

This verifies the inventory's account, routes, NAT mapping, private addressing,
exact security rules, and permissive default NACL before exercising traffic.
It checks both proxy endpoints, web/app reachability, app HTTP/HTTPS source
addresses matching the NAT EIP, and blocked new web/database, database/app,
and database/external connections. The external check uses an IP just reached
from app. An unused public port is checked against a temporary healthy web
listener, stopped in `finally` with a 30-second service deadline as a fallback.
SSH keys must already be pinned; the helper never accepts unknown keys.

Blocked checks use two-second connection timeouts and independently healthy
targets. They establish the observed access matrix alongside AWS policy;
they do not identify the exact packet-drop location. The reproduction also
changed the seed row, observed the new value through the proxy, restored it,
and captured web/app/database TCP headers on app. Managed NAT internals were
not captured. Custom NACLs and repeatable cloud faults remain next.

## Cost and destruction

The [Ohio NAT example and public IPv4 pricing](https://aws.amazon.com/vpc/pricing/)
give a network floor of `$0.045 + 2 × $0.005 = $0.055/hour`, plus NAT data
processing and transfer. The full estimate also needs three regional Linux
`t3.micro` prices and 24 GiB of gp3 storage. Recheck those with the
[AWS calculator](https://calculator.aws/) for the planned runtime; the network
floor is not a complete estimate or a spending cap. NAT partial hours round up.
The October 4 regional API rates gave about **$0.089/hour** for the fixed
footprint, using 730 hours/month to normalize disk storage. Data processing,
transfer, taxes, and billing rounding are additional; the recorded run did
not inspect the final bill.

Before destroy, save the `resource_inventory` output privately. It includes
instance and root-volume IDs, both EIP allocation IDs, the NAT, IGW, and VPC:

```sh
terraform -chdir=aws output -json resource_inventory > aws/inventory.private.json
terraform -chdir=aws plan -destroy -var-file=local.tfvars -out=destroy.tfplan
terraform -chdir=aws apply destroy.tfplan
```

The ignored inventory includes the account, region, public mappings, instance
and root-volume IDs, both EIP allocation IDs, the NAT, IGW, and VPC. Verify
each saved inventory, including those from before host replacements:

```sh
python3 aws/check.py destroyed --inventory aws/inventory.private.json \
  --profile <your AWS profile>
```

The helper checks every resource ID separately, accepts only the corresponding
not-found error or terminal state, and fails on wrong-account, authentication,
permission, or malformed-ID errors. NAT deletion can take several minutes.
A successful Terraform command or an empty local state alone is not a cloud
cleanup check. Preserve the state and inventories if creation or destruction
partially fails; stopping instances leaves other billable resources in place.
