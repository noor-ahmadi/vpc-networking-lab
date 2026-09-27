# Local networking lab

The lab creates three separate Ethernet segments and routes IPv4 traffic
between them. `vpc-router` has an interface in each segment, with forwarding
enabled. Each workload uses its subnet's `.1` address as its default gateway.
The names describe their future roles: there is no NAT, application, database
service, or AWS deployment yet.

| Segment | Bridge in `vpc-switch` | Namespace interfaces |
| --- | --- | --- |
| Public | `br-public` | `vpc-router` 10.0.1.1, `vpc-web` 10.0.1.10, `vpc-nat` 10.0.1.20 |
| Private | `br-private` | `vpc-router` 10.0.2.1, `vpc-app` 10.0.2.10 |
| Isolated | `br-isolated` | `vpc-router` 10.0.3.1, `vpc-db` 10.0.3.10 |

Every address uses a `/24`. Seven veth pairs connect the namespace interfaces
to their bridges. The switch namespace has no IPv4 address, and nothing is
attached to a host interface. The lab uses the kernel's existing network stack.

On a disposable Ubuntu 24.04 Linux VM, install the tools and run:

```sh
sudo apt-get update
sudo apt-get install -y iproute2 iputils-ping iputils-arping tcpdump procps util-linux
sudo ./lab.sh up
sudo ./lab.sh status
sudo ./lab.sh check
sudo ./lab.sh arp
sudo ./lab.sh down
```

`status` includes the router's policy rules and tables. `check` verifies local
peers, round trips between subnets, policy selection, and external rejection.
It also checks that public ARP broadcasts cannot reach the private or isolated
peer. `arp` clears the public host's neighbor cache and captures the request
and reply for `10.0.1.20`.

The router selects a routing table by the packet's incoming interface:

| Router ingress | Rule priority / table | Routes in this iteration |
| --- | --- | --- |
| `public` | `101` | All three subnet CIDRs; `unreachable default` |
| `private` | `102` | All three subnet CIDRs; `unreachable default` |
| `isolated` | `103` | All three subnet CIDRs; `unreachable default` |

Each table has direct `/24` routes to the three segments. The terminal
`unreachable default` matters: leaving out a default route would let Linux
continue to the next policy rule and potentially use a route in `main`.
The rules run after the kernel's local-address lookup and before `main`.
Router-originated traffic still uses `main`; forwarded subnet traffic uses
its associated table. This models subnet route associations with Linux rules.

For an app-to-database ping, the app sends the packet to `10.0.2.1`. Table `102`
sends it out the router's isolated interface to `10.0.3.10`. The database returns
its reply through `10.0.3.1`, and table `103` sends it back to the app. The app
resolves its gateway's MAC address with ARP, not the remote database's. Inspect
the forward decision with:

```sh
sudo ip -n vpc-router route get 10.0.3.10 from 10.0.2.10 iif private
```

All three tables currently reject external traffic, so workloads cannot reach
the internet despite having host default routes. Public and private egress
routes will arrive with the external fixture and NAT. All three subnets can
communicate internally; access filtering remains a separate step.

Ownership records live in `/run/vpc-networking-lab`. Repeating `up` reports a
conflict instead of replacing anything. `down` checks namespace identities and
refuses cleanup if a name was replaced or a namespace still contains a process.
Stop any manually started namespace processes before retrying. Repeating
`down` is harmless. The empty lock file can remain in `/run`.

Network namespaces share the host kernel and filesystem. Use a disposable
Linux environment for experiments. The scripts modify only their own network
namespaces, with no host forwarding, route, or firewall changes.

Next: the external fixture and NAT.
The eventual AWS version will reproduce the selected traffic
behavior; it will not reproduce AWS's internal network implementation.

References: [network namespaces](https://man7.org/linux/man-pages/man7/network_namespaces.7.html),
[veth pairs](https://man7.org/linux/man-pages/man4/veth.4.html),
[Linux routes](https://man7.org/linux/man-pages/man8/ip-route.8.html),
[policy rules](https://man7.org/linux/man-pages/man8/ip-rule.8.html).

## Verification

`sudo bash tests/integration.sh` runs two create/check/capture/remove cycles,
checks conflicting and replaced namespace names, rolls back a failed setup,
refuses cleanup around a running process, and detects a deliberately miswired
port. Routing checks verify gateway ARP resolution and recover from disabled
forwarding, a missing database return route, and a missing policy rule.

The policy regression adds a test-only `203.0.113.10/32` address to `vpc-web`
and a route to it in the router's main table. The router can ping this healthy
destination, but all three subnet policies reject it. Adding a route only to
table `102` permits the app while the database stays blocked. Removing table
`103`'s terminal route reproduces the database leak through `main`; restoring
it blocks the leak again. The fixture is removed before the lifecycle ends.
No real internet connection is involved.

The suite also compares host
interfaces, addresses, routes, routing rules, forwarding settings, and
firewall rules before and after. Install `nftables` for this comparison and
`shellcheck` for shell linting.

For an isolated test on a Linux Docker engine, build the tool image, then run
with networking disabled. Namespace creation requires a privileged container;
use your local development machine or a disposable runner.

```sh
docker build -t vpc-lab-test -f tests/Dockerfile .
docker run --rm --privileged --network none \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" vpc-lab-test
```

The image supplies tools only. The network is created explicitly by `lab.sh`,
and all connectivity checks run without an external network connection.
