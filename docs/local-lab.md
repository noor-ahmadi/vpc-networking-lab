# Local Ethernet lab

The first slice creates three separate Ethernet segments. `vpc-router` has an
interface in each segment, but forwarding is disabled and workloads have no
default route. The names describe their future roles: there is no NAT,
application, database service, or AWS deployment yet.

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

`check` verifies reachable peers on every segment before checking that public
ARP broadcasts cannot reach the private or isolated peer. `arp` clears the
public host's neighbor cache and captures the request and reply for
`10.0.1.20`. It demonstrates discovery of a neighbor's MAC address; it does not
demonstrate internet access.

Ownership records live in `/run/vpc-networking-lab`. Repeating `up` reports a
conflict instead of replacing anything. `down` checks namespace identities and
refuses cleanup if a name was replaced or a namespace still contains a process.
Stop any manually started namespace processes before retrying. Repeating
`down` is harmless. The empty lock file can remain in `/run`.

Network namespaces share the host kernel and filesystem. Use a disposable
Linux environment for experiments. The scripts modify only their own network
namespaces, with no host forwarding, route, or firewall changes.

Next: subnet routing policies and return paths, followed by the external
fixture and NAT. The eventual AWS version will reproduce the selected traffic
behavior; it will not reproduce AWS's internal network implementation.

References: [network namespaces](https://man7.org/linux/man-pages/man7/network_namespaces.7.html),
[veth pairs](https://man7.org/linux/man-pages/man4/veth.4.html).

## Verification

`sudo bash tests/integration.sh` runs two create/check/capture/remove cycles,
checks conflicting and replaced namespace names, rolls back a failed setup,
refuses cleanup around a running process, and detects a deliberately miswired port. It also compares
host interfaces, addresses, routes, routing rules, forwarding settings, and
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
