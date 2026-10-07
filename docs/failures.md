# Failure exercises

Run these in the disposable [local lab](local-lab.md). Each fault changes one
part of the fixed topology. `repair` restores that part's baseline; it does
not restore arbitrary manual changes. Commands verify ownership of every lab
namespace before changing anything. Repeating a fault or its repair is safe.

## Start and observe

Create the network, check it, then leave the services running in one terminal:

```sh
sudo ./lab.sh up
sudo ./lab.sh check
sudo python3 app/services.py
```

Wait for `ready`. For the external HTTP tests, run this sealed fixture in a
second terminal and wait for its `ready`:

```sh
sudo ip netns exec vpc-internet python3 tests/traffic.py serve 203.0.113.10 8080
```

In a third terminal, these commands check private egress and the database path:

```sh
sudo ip netns exec vpc-app python3 tests/traffic.py request 203.0.113.10 8080
sudo ip netns exec vpc-internet python3 -c \
  "from http.client import HTTPConnection; c = HTTPConnection('203.0.113.20', 80, timeout=6); c.request('GET', '/message'); r = c.getresponse(); print(r.status, r.read().decode()); c.close()"
```

The first reports the app's client address and the translated peer
`203.0.113.30`. The second returns HTTP 200 and the seeded database message.
Use `/health` in place of `/message` to check the app without a database query.
Keep those same checks for diagnosis and repeat them after repair.

## Five faults

Apply one fault at a time, using a name from this table:

```sh
sudo ./lab.sh fault private-route
sudo ./lab.sh status
# Run the relevant checks and inspect the evidence below.
sudo ./lab.sh repair private-route
sudo ./lab.sh check
```

| Name | Change | Expected symptom | Evidence |
| --- | --- | --- | --- |
| `forwarding` | Disable IPv4 forwarding in `vpc-router` | Same-subnet peers work; inter-subnet traffic fails | Router `ip_forward=0`; public peer ping succeeds; routed ping fails |
| `private-route` | Replace table 102's NAT default with `unreachable default` | New private egress fails; public HTTP and app/database access work | Table 102 and a failed lookup for the outside destination |
| `nat-snat` | Empty NAT's postrouting chain | New private egress fails; public egress and internal access work | Missing SNAT rule; edge `denied_forward` increases for untranslated private traffic |
| `database` | Drop new app-to-database TCP 5432 connections | `/health` returns 200; `/message` returns 503 | Database `vpc_fault` counter `denied_database` increases |
| `return-ports` | Drop database replies to app TCP ports 1024–65535 at the router | `/health` returns 200; `/message` returns 503 | Outgoing SYN and database SYN-ACK; absent SYN-ACK at app; router `denied_return` increases |

`check` uses ICMP and route lookups. The two TCP filter faults can leave it
passing; use the real `/message` request to observe those failures and repairs.
All HTTP and database attempts use fresh connections with bounded timeouts.
Existing NAT bindings can survive a rule edit, so an already-open connection
does not test removal of SNAT. The automated tests leave connection tracking
intact and open new connections.

The private-route fault uses a terminal unreachable route because simply
deleting Linux's default would allow policy lookup to continue into `main`.
Direct internal subnet routes remain in table 102. Inspect the actual lookup:

```sh
sudo ip -n vpc-router route show table 102
sudo ip -n vpc-router route get 203.0.113.10 from 10.0.2.10 iif private
sudo ip -n vpc-router route get 10.0.3.10 from 10.0.2.10 iif private
```

Inspect the forwarding setting, translation chain, and named drop counters:

```sh
sudo ip netns exec vpc-router sysctl net.ipv4.ip_forward
sudo ip netns exec vpc-nat nft list chain ip vpc_nat postrouting
sudo ip netns exec vpc-edge nft list counter ip vpc_edge denied_forward
sudo ip netns exec vpc-db nft list counter ip vpc_fault denied_database
sudo ip netns exec vpc-router nft list counter ip vpc_fault denied_return
```

The last two counters exist only while their respective faults are applied.
Repair removes the extra fault table and preserves the workload's stateful
access rules. Forwarding repair also restores the router's redirect and
reverse-path settings after enabling forwarding.

## Why the return port matters

An app request travels from `10.0.2.10:<ephemeral port>` to
`10.0.3.10:5432`. The database's reply reverses that pair: its source port is
5432 and its destination is the app's ephemeral port. Permission for requests
to destination port 5432 alone does not grant permission for that reply.

The normal workload rules allow established replies using connection state.
The return-ports exercise adds a separate router forward rule with no
connection-state exception, on traffic from the isolated to the private
interface. It models the reply-port failure of a stateless subnet filter.
AWS [network ACLs](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-network-acls.html)
require explicit reply permissions, while security groups track connections.
The local rule demonstrates that traffic behavior using Linux. The
[AWS exercises](aws-failures.md) verify the corresponding custom-NACL reply
failure on a real deployment, with separate guest captures and recovery.

Capture the two sides in separate terminals before requesting `/message`:

```sh
sudo ip netns exec vpc-db tcpdump -nn -i eth0 -Q out \
  'tcp src port 5432 and dst host 10.0.2.10 and tcp[13] & 18 == 18'
sudo ip netns exec vpc-app tcpdump -nn -i eth0 \
  'host 10.0.3.10 and tcp port 5432'
```

While faulted, the app sends a SYN and PostgreSQL sends a SYN-ACK toward that
same client port. The router drops the reply, so it never reaches the app.
The integration test captures all three observation points simultaneously,
matches the client port, and checks the router counter. The app-side reply
capture is bounded at five seconds. After repair, a new query must return the
freshly stored database value; a timeout alone would not establish the cause.

## Repeat and clean up

`sudo bash tests/integration.sh` runs the fault and repair checks alongside
two full lifecycle cycles, service tests, and host network snapshots. It
repeats each named action to check idempotence and rejects commands against a
missing or replaced lab. `RESULT:` JSON lines record the case, source,
destination, expected and observed outcomes, elapsed seconds, and evidence.
Packet, route, and counter observations appear alongside those records.

For a manual session, repair the active fault and repeat both network and
service checks. Stop both foreground processes with Ctrl+C before running:

```sh
sudo ./lab.sh down
```
