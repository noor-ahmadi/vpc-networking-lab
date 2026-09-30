# Local networking lab

The lab creates three separate Ethernet segments and routes IPv4 traffic
between them. `vpc-router` has an interface in each segment, with forwarding
enabled. Each workload uses its subnet's `.1` address as its default gateway.
An edge namespace connects the public subnet to a sealed internet fixture,
and a separate NAT namespace provides private outbound access. Nginx calls
a private Python app, which queries PostgreSQL in the isolated subnet.
The AWS deployment is still to come.

| Segment | Bridge in `vpc-switch` | Namespace interfaces |
| --- | --- | --- |
| Public | `br-public` | `vpc-router` 10.0.1.1, `vpc-web` 10.0.1.10, `vpc-nat` 10.0.1.20 |
| Private | `br-private` | `vpc-router` 10.0.2.1, `vpc-app` 10.0.2.10 |
| Isolated | `br-isolated` | `vpc-router` 10.0.3.1, `vpc-db` 10.0.3.10 |

Subnet addresses use `/24`. Seven veth pairs connect them to their bridges.
Two more pairs connect router to edge and edge to the outside fixture:

| Link | Addresses |
| --- | --- |
| Router `edge` <-> edge `vpc` | `198.51.100.2/30` <-> `198.51.100.1/30` |
| Edge `internet` <-> outside `eth0` | `203.0.113.1/24` <-> `203.0.113.10/24` |
| Public web mapping, owned by edge | `203.0.113.20` <-> `10.0.1.10` |
| Public NAT mapping, owned by edge | `203.0.113.30` <-> `10.0.1.20` |

These are documentation addresses inside the lab, not real public addresses.
The switch namespace has no IPv4 address, and nothing is attached to a host
interface. The lab uses the kernel's existing network stack.

On a disposable Ubuntu 24.04 Linux VM, install the tools and run:

```sh
sudo apt-get update
sudo apt-get install -y iproute2 iputils-ping iputils-arping tcpdump procps util-linux nftables
sudo ./lab.sh up
sudo ./lab.sh status
sudo ./lab.sh check
sudo ./lab.sh arp
sudo ./lab.sh down
```

`status` includes policy tables and nftables rules with packet counters. `check`
verifies local peers, inter-subnet routing, public ingress/egress, private NAT,
and rejection of isolated egress, using ICMP and route lookups.
It also checks that public ARP broadcasts cannot reach the private or isolated
peer. `arp` clears the public host's neighbor cache and captures the request
and reply for `10.0.1.20`.

The router selects a routing table by the packet's incoming interface:

| Router ingress | Rule priority / table | Default route |
| --- | --- | --- |
| `public` | `101` | Edge at `198.51.100.1` |
| `private` | `102` | NAT at `10.0.1.20` |
| `isolated` | `103` | `unreachable default` |

Each table also has direct `/24` routes to the three segments. The isolated
`unreachable default` matters: leaving out a default route would let Linux
continue to the next policy rule and potentially use a route in `main`.
The rules run after the kernel's local-address lookup and before `main`.
Router-originated traffic and return traffic from the edge use `main`, which
has routes to the subnets and the outside fixture. Forwarded subnet traffic
uses its associated table. This models subnet route associations with Linux rules.

For an app-to-database ping, the app sends the packet to `10.0.2.1`. Table `102`
sends it out the router's isolated interface to `10.0.3.10`. The database returns
its reply through `10.0.3.1`, and table `103` sends it back to the app. The app
resolves its gateway's MAC address with ARP, not the remote database's. Inspect
the forward decision with:

```sh
sudo ip -n vpc-router route get 10.0.3.10 from 10.0.2.10 iif private
```

All three subnets have internal routes. Workload firewall rules separately
control TCP service access; the isolated routing policy blocks external traffic.

## NAT and public access

For private egress, table `102` sends a packet to the NAT namespace on the
public segment. NAT forwards it back out the same interface and changes its
source to `10.0.1.20`. It reaches the router on `public`, so table `101` now
sends it to the edge. The edge maps this fixed private address to `203.0.113.30`.

| Observation point | Source | Destination |
| --- | --- | --- |
| App `eth0` outgoing | `10.0.2.10` | `203.0.113.10` |
| NAT `eth0` outgoing | `10.0.1.20` | `203.0.113.10` |
| Edge `internet` outgoing | `203.0.113.30` | `203.0.113.10` |
| Reply delivered to app | `203.0.113.10` | `10.0.2.10` |

Connection tracking reverses both translations for replies. Tests print the
original and reply TCP tuples, including ports; NAT need not preserve a source
port. Fixed addresses use explicit SNAT in [nat.nft](../network/nat.nft) and
[edge.nft](../network/edge.nft). Masquerade would select an interface address
dynamically; it is unnecessary for these fixed addresses.

The edge also maps incoming traffic for `203.0.113.20` to the public web
host, permitting TCP 80 and diagnostic ICMP. Its forward filter drops other
new incoming traffic, direct traffic to VPC addresses, and new connections
to the NAT public mapping. Outbound traffic must already
have one of the two mapped sources; the edge does not rescue a broken NAT
by translating arbitrary private addresses. The outside fixture has no VPC
return route, so a packet that escapes without translation cannot get a reply.

Forwarding is enabled only on router, edge, and NAT. ICMP redirects are disabled
inside all lab namespaces to keep hosts using the configured gateways. Loose
reverse-path validation (`rp_filter=2`) permits the asymmetric NAT return path.
These sysctls are set after forwarding and do not change host configuration.
Native nftables supplies NAT and stateful filtering; no parallel iptables
ruleset is maintained. In iptables terms, these are SNAT/DNAT in the NAT hooks
and connection-state rules in the forwarding path.

## Lifecycle

Ownership records live in `/run/vpc-networking-lab`. Repeating `up` reports a
conflict instead of replacing anything. `down` checks namespace identities and
refuses cleanup if a name was replaced or a namespace still contains a process.
Stop any manually started namespace processes before retrying. Repeating
`down` is harmless. The empty lock file can remain in `/run`.

Network namespaces share the host kernel and filesystem. Use a disposable
Linux environment for experiments. The scripts modify only their own network
namespaces, with no host forwarding, route, or firewall changes.

The eventual AWS version will reproduce the selected traffic
behavior; it will not reproduce AWS's internal network implementation.

## Application and service access

Install `nginx`, `postgresql-16`, `python3`, and `python3-psycopg2` alongside
the networking tools. After `sudo ./lab.sh up`, start the demo in a terminal:

```sh
sudo python3 app/services.py
```

Wait for `ready`, then use a second terminal to query it from the external fixture:

```sh
sudo ip netns exec vpc-internet python3 -c \
  "from urllib.request import urlopen; print(urlopen('http://203.0.113.20/message', timeout=6).read().decode())"
```

`/health` returns app health without querying the database. `/message` reads
the seeded value, `Hello from the isolated subnet`, over TCP at `10.0.3.10:5432`.
Nginx accepts the public request on `10.0.1.10:80` and opens a separate TCP
connection to `10.0.2.10:8080`; the response's `peer` is the proxy's private IP.
The app uses a read-only database role with a generated SCRAM password.
No shared PostgreSQL Unix socket is enabled. A database connection failure
returns HTTP 503 while `/health` remains available; unknown paths return 404.

| Workload | New TCP ingress | New TCP egress |
| --- | --- | --- |
| Web | TCP 80 from external fixture `203.0.113.10` | App TCP 8080; external segment TCP 80, 443, 8080 |
| App | TCP 8080 from web `10.0.1.10` | Database TCP 5432; external segment TCP 80, 443, 8080 |
| Database | TCP 5432 from app `10.0.2.10` | None |

The input/output rules live in `network/{web,app,db}.nft`. Established and
related traffic is allowed in both directions, so the database can answer an
app query without permission to open new connections. Named `denied_input`
and `denied_output` counters identify rejected traffic. Loopback and ICMP are
permitted for local readiness and routing diagnostics; this TCP service matrix
does not imply ICMP isolation. These host rules model access intent with fixed
addresses, not AWS security group identities or AWS's implementation.

The launcher creates its own temporary PostgreSQL cluster and Nginx files.
Ctrl+C stops its processes and removes those files before `sudo ./lab.sh down`.
It does not start or stop system services. Each launch resets the demo data.
The database administration trust entry applies only to its own `10.0.3.10`
source; the app connects from `10.0.2.10` using SCRAM. This is a sealed,
disposable demo, not a production application or durable database.

References: [network namespaces](https://man7.org/linux/man-pages/man7/network_namespaces.7.html),
[veth pairs](https://man7.org/linux/man-pages/man4/veth.4.html),
[Linux routes](https://man7.org/linux/man-pages/man8/ip-route.8.html),
[policy rules](https://man7.org/linux/man-pages/man8/ip-rule.8.html),
[IPv4 sysctls](https://docs.kernel.org/networking/ip-sysctl.html),
[nftables](https://netfilter.org/projects/nftables/manpage.html),
[Nginx proxying](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[PostgreSQL authentication](https://www.postgresql.org/docs/16/auth-pg-hba-conf.html).

## Verification

`sudo bash tests/integration.sh` runs two create/check/capture/remove cycles,
checks conflicting and replaced namespace names, rolls back a failed setup,
refuses cleanup around a running process, and detects a deliberately miswired
port. Routing checks verify gateway ARP resolution and recover from disabled
forwarding, a missing database return route, and a missing policy rule.

The policy regression adds a test-only `192.0.2.10/32` address to `vpc-web`
and a route to it in the router's main table. The router can ping this healthy
destination, but all three subnet policies reject it. Adding a route only to
table `102` permits the app while the database stays blocked. Removing table
`103`'s terminal route reproduces the database leak through `main`; restoring
it blocks the leak again. The fixture is removed before the lifecycle ends.
No real internet connection is involved.

`tests/traffic.py` starts temporary HTTP servers on the lab addresses and
verifies their health before testing rejection. It proves the two SNAT steps
with four simultaneous captures and conntrack tuples, checks the server's
observed client address, and verifies public HTTP in both directions. It also:

- Adds a test-only outside route into the VPC and proves that the edge's drop
  counter increases for denied requests to healthy internal services.
- Takes NAT's interface down and verifies that internal HTTP and public egress
  survive. Recovery restores the interface and its lost static default route.
- Removes SNAT at each translation point, verifies that new outbound HTTP
  fails while the destination remains healthy, then restores the rules.

Every test server and capture is stopped before teardown. These echo services
are temporary network fixtures on the allowed ports; they stop before the
real application and database checks start. Install `python3` and `conntrack`
to run them as part of the integration suite.

`tests/services.py` starts the actual services in each lifecycle cycle. It
queries the seeded row through the public proxy, updates that row inside the
database namespace, and confirms the next response reads the new value. It
checks proxy-to-database and new database-to-app denial at both the source
output and destination input filters, with healthy destinations and increasing
deny counters. A live listener on an unused web port proves external rejection
at the edge rather than failure from a closed port. It also verifies that the
full database path survives a lost NAT interface, and that a deliberate
app-to-database drop returns 503 while health stays up, then recovers after
the rule is restored. All demo processes exit before namespace teardown.
An injected Nginx startup failure also checks that the launcher stops the app
and database and removes its temporary files before exiting.

The suite also compares host interfaces, addresses, routes, routing rules, forwarding settings, and
firewall rules before and after. Install `nftables` for this comparison and
`shellcheck` for shell linting.

CI uses `sudo unshare --net bash tests/integration.sh` to give this comparison
its own network stack. Runner interfaces can appear during a job; they remain
outside that stack. The full snapshot comparison still detects changes to the
test's starting network, and the namespace ownership checks still run.

For an isolated test on a Linux Docker engine, build the tool image, then run
with networking disabled. Namespace creation requires a privileged container;
use your local development machine or a disposable runner.

```sh
docker build -t vpc-lab-test -f tests/Dockerfile .
docker run --rm --privileged --network none \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" vpc-lab-test
```

The image supplies the tools and service binaries. `lab.sh` creates the network,
and the service launcher starts its own demo processes. All connectivity checks
run without an external network connection.
