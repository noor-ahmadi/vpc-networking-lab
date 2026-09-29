#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
umask 077

readonly STATE=/run/vpc-networking-lab
readonly NETNS=/run/netns
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly ROOT
readonly -a NAMESPACES=(vpc-switch vpc-router vpc-web vpc-nat vpc-app vpc-db vpc-edge vpc-internet)

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "Missing command: $1"; }
exists() { [[ -e "$NETNS/$1" ]]; }
identity() { stat -Lc '%d:%i' "$NETNS/$1"; }

usage() {
    cat <<'USAGE'
Usage: sudo ./lab.sh {up|down|status|check|arp}

  up      Create three subnet segments with independent routing policies.
  down    Remove this lab's namespaces; refuse busy or replaced ones.
  status  Show addresses, routes, policy rules, and NAT/filter counters.
  check   Verify internal routing, public access, NAT, and isolated egress.
  arp     Capture an ARP request and reply on the public segment.

Requires Linux and root. See docs/local-lab.md for dependencies and topology.
USAGE
}

# Record namespace identities, so a reused name never grants permission to delete it.
verify_owned() {
    local name recorded
    [[ -f "$STATE/namespaces" ]] || die "Ownership record is missing: $STATE/namespaces"
    while read -r name recorded; do
        [[ " ${NAMESPACES[*]} " == *" $name "* ]] || die "Unknown namespace in ownership record"
        if exists "$name"; then
            [[ $(identity "$name") == "$recorded" ]] || die "$name has been replaced; leaving it untouched"
        fi
    done < "$STATE/namespaces"
}

require_lab() {
    [[ -d "$STATE" ]] || die "Lab is down; run ./lab.sh up first"
    verify_owned
    local name
    for name in "${NAMESPACES[@]}"; do
        exists "$name" || die "$name is missing; run down before rebuilding"
        grep -qxF "$name $(identity "$name")" "$STATE/namespaces" || die "$name is not owned by this lab"
    done
}

down() {
    if [[ ! -d "$STATE" ]]; then
        printf 'Lab is down.\n'
        return
    fi
    verify_owned
    local name recorded pids
    # Check every namespace before deleting any of them.
    while read -r name recorded; do
        if exists "$name"; then
            pids=$(ip netns pids "$name")
            [[ -z "$pids" ]] || die "$name still has processes ($pids); stop them before running down"
        fi
    done < "$STATE/namespaces"
    while read -r name recorded; do
        if exists "$name"; then
            ip netns delete "$name"
        fi
    done < "$STATE/namespaces"
    rm -- "$STATE/namespaces"
    rmdir -- "$STATE"
    printf 'Lab removed.\n'
}

rollback() {
    local result=$?
    trap - EXIT
    if (( result != 0 )); then
        printf 'Setup failed; removing namespaces created by this attempt.\n' >&2
        down
    fi
    exit "$result"
}

connect() {
    local namespace=$1 interface=$2 port=$3 bridge=$4 address=$5
    # Create both ends inside lab namespaces, never in the host network.
    ip -n vpc-switch link add "$port" type veth peer name "$interface" netns "$namespace"
    ip -n vpc-switch link set "$port" master "$bridge" up
    ip -n "$namespace" address add "$address" dev "$interface"
    ip -n "$namespace" link set "$interface" up
}

up() {
    need nft
    [[ ! -e "$STATE" ]] || die "Lab state already exists; inspect status or run down first"
    local name bridge table
    for name in "${NAMESPACES[@]}"; do
        [[ ! -e "$NETNS/$name" ]] || die "$name already exists; leaving it untouched"
    done
    mkdir -- "$STATE"
    : > "$STATE/namespaces"
    trap rollback EXIT
    for name in "${NAMESPACES[@]}"; do
        ip netns add "$name"
        printf '%s %s\n' "$name" "$(identity "$name")" >> "$STATE/namespaces"
        ip -n "$name" link set lo up
        ip netns exec "$name" sysctl -q -w net.ipv4.ip_forward=0
    done
    for bridge in br-public br-private br-isolated; do
        ip -n vpc-switch link add "$bridge" type bridge
        ip -n vpc-switch link set "$bridge" up
    done
    connect vpc-router public r-public br-public 10.0.1.1/24
    connect vpc-router private r-private br-private 10.0.2.1/24
    connect vpc-router isolated r-isolated br-isolated 10.0.3.1/24
    connect vpc-web eth0 web br-public 10.0.1.10/24
    connect vpc-nat eth0 nat br-public 10.0.1.20/24
    connect vpc-app eth0 app br-private 10.0.2.10/24
    connect vpc-db eth0 db br-isolated 10.0.3.10/24

    ip -n vpc-router link add edge type veth peer name vpc netns vpc-edge
    ip -n vpc-router address add 198.51.100.2/30 dev edge
    ip -n vpc-edge address add 198.51.100.1/30 dev vpc
    ip -n vpc-router link set edge up
    ip -n vpc-edge link set vpc up
    ip -n vpc-edge link add internet type veth peer name eth0 netns vpc-internet
    ip -n vpc-edge address add 203.0.113.1/24 dev internet
    ip -n vpc-edge address add 203.0.113.20/32 dev internet
    ip -n vpc-edge address add 203.0.113.30/32 dev internet
    ip -n vpc-internet address add 203.0.113.10/24 dev eth0
    ip -n vpc-edge link set internet up
    ip -n vpc-internet link set eth0 up

    # Hosts send off-subnet packets to the router; connected routes handle replies.
    ip -n vpc-web route add default via 10.0.1.1 dev eth0
    ip -n vpc-nat route add default via 10.0.1.1 dev eth0
    ip -n vpc-app route add default via 10.0.2.1 dev eth0
    ip -n vpc-db route add default via 10.0.3.1 dev eth0

    # Each ingress subnet selects its own table, like a subnet route association.
    for table in 101 102 103; do
        ip -n vpc-router route add table "$table" 10.0.1.0/24 dev public scope link
        ip -n vpc-router route add table "$table" 10.0.2.0/24 dev private scope link
        ip -n vpc-router route add table "$table" 10.0.3.0/24 dev isolated scope link
    done
    ip -n vpc-router route add table 101 198.51.100.0/30 dev edge scope link
    ip -n vpc-router route add table 101 default via 198.51.100.1 dev edge
    ip -n vpc-router route add table 102 default via 10.0.1.20 dev public
    # A missing route would fall through to main; unreachable stops lookup.
    ip -n vpc-router route add table 103 unreachable default
    ip -n vpc-router rule add priority 101 iif public lookup 101
    ip -n vpc-router rule add priority 102 iif private lookup 102
    ip -n vpc-router rule add priority 103 iif isolated lookup 103
    ip -n vpc-router route add 203.0.113.0/24 via 198.51.100.1 dev edge
    ip -n vpc-edge route add 10.0.0.0/16 via 198.51.100.2 dev vpc
    # No external-host route into the VPC: replies must use the public mappings.
    for name in vpc-router vpc-edge vpc-nat; do
        ip netns exec "$name" sysctl -q -w net.ipv4.ip_forward=1
    done
    # Configure these after forwarding, which resets some IPv4 defaults.
    for name in "${NAMESPACES[@]}"; do
        ip netns exec "$name" bash -e <<'SYSCTLS'
            for interface in /proc/sys/net/ipv4/conf/*; do
                printf "0\n" > "$interface/send_redirects"
                printf "0\n" > "$interface/accept_redirects"
                printf "2\n" > "$interface/rp_filter"
            done
SYSCTLS
    done
    ip netns exec vpc-nat nft -f "$ROOT/network/nat.nft"
    ip netns exec vpc-edge nft -f "$ROOT/network/edge.nft"
    trap - EXIT
    printf 'Lab created: eight namespaces, subnet routing, public mappings, and private NAT.\n'
}

status() {
    if [[ ! -d "$STATE" ]]; then
        printf 'Lab is down.\n'
        return
    fi
    require_lab
    local name table
    for name in "${NAMESPACES[@]}"; do
        printf '\n%s\n' "$name"
        ip -n "$name" -brief address show
        ip -n "$name" route show
    done
    printf '\nvpc-router policy rules\n'
    ip -n vpc-router rule show
    for table in 101 102 103; do
        printf '\nvpc-router table %s\n' "$table"
        ip -n vpc-router route show table "$table"
    done
    for name in vpc-nat vpc-edge; do
        printf '\n%s NAT and filter rules\n' "$name"
        ip netns exec "$name" nft list ruleset
    done
}

check() {
    require_lab
    need ping
    need arping
    local namespace target interface source egress table route
    while read -r namespace target; do
        ip netns exec "$namespace" ping -n -c 1 -W 2 "$target" >/dev/null || die "$namespace cannot reach $target"
        printf 'PASS: %s reaches %s on its own segment\n' "$namespace" "$target"
    done <<'PEERS'
vpc-web 10.0.1.20
vpc-nat 10.0.1.10
vpc-web 10.0.1.1
vpc-app 10.0.2.1
vpc-db 10.0.3.1
vpc-router 10.0.2.10
vpc-router 10.0.3.10
PEERS
    while read -r namespace target; do
        ip netns exec "$namespace" ping -n -c 1 -W 2 "$target" >/dev/null || die "$namespace cannot reach $target through the router"
        printf 'PASS: %s reaches %s through the router\n' "$namespace" "$target"
    done <<'ROUTED_PEERS'
vpc-web 10.0.2.10
vpc-app 10.0.1.10
vpc-app 10.0.3.10
vpc-db 10.0.2.10
vpc-db 10.0.1.20
vpc-nat 10.0.3.10
ROUTED_PEERS
    ip netns exec vpc-web arping -I eth0 -c 1 -w 2 10.0.1.20 >/dev/null || die 'Public peer did not answer ARP'
    printf 'PASS: public peers exchange ARP\n'
    # These destinations were just reached from their own segment above.
    for target in 10.0.2.10 10.0.3.10; do
        if ip netns exec vpc-web arping -I eth0 -c 1 -w 1 "$target" >/dev/null; then
            die "ARP crossed a subnet bridge to $target"
        fi
        printf 'PASS: public ARP does not reach %s\n' "$target"
    done
    while read -r interface source target egress table; do
        route=$(ip -n vpc-router route get "$target" from "$source" iif "$interface") || die "$interface has no internal route"
        [[ $route == *"dev $egress table $table"* && $route != *" via "* ]] || die "$interface is not using its direct subnet route in table $table"
        printf 'PASS: %s uses table %s for direct internal traffic\n' "$interface" "$table"
    done <<'POLICIES'
public 10.0.1.10 10.0.2.10 private 101
private 10.0.2.10 10.0.3.10 isolated 102
isolated 10.0.3.10 10.0.2.10 private 103
POLICIES
    route=$(ip -n vpc-router route get 203.0.113.10 from 10.0.1.10 iif public)
    [[ $route == *'via 198.51.100.1 dev edge table 101'* ]] || die 'Public traffic does not use the edge'
    route=$(ip -n vpc-router route get 203.0.113.10 from 10.0.2.10 iif private)
    [[ $route == *'via 10.0.1.20 dev public table 102'* ]] || die 'Private traffic does not use NAT'
    [[ $(ip -n vpc-router route show table 103 type unreachable exact default) == 'unreachable default'* ]] || die 'Isolated subnet is missing its terminal unreachable route'
    if ip -n vpc-router route get 203.0.113.10 from 10.0.3.10 iif isolated >/dev/null 2>&1; then
        die 'Isolated traffic escaped its routing policy'
    fi
    for namespace in vpc-web vpc-app; do
        ip netns exec "$namespace" ping -n -c 1 -W 2 203.0.113.10 >/dev/null || die "$namespace cannot reach the external fixture"
    done
    ip netns exec vpc-internet ping -n -c 1 -W 2 203.0.113.20 >/dev/null || die 'Public web mapping is unreachable'
    if ip netns exec vpc-db ping -n -c 1 -W 2 203.0.113.10 >/dev/null; then
        die 'Database reached the healthy external fixture'
    fi
    for namespace in vpc-router vpc-edge vpc-nat; do
        [[ $(ip netns exec "$namespace" sysctl -n net.ipv4.ip_forward) == 1 ]] || die "$namespace forwarding is disabled"
    done
    printf 'PASS: public access and private NAT work; isolated external traffic is blocked\n'
}

capture_arp() {
    require_lab
    need tcpdump
    need timeout
    need ping
    local capture_dir capture_pid attempt ready=0
    capture_dir=$(mktemp -d)
    # The capture is the only background process this script starts.
    trap 'if [[ -n ${capture_pid:-} ]]; then kill "$capture_pid" 2>/dev/null || true; wait "$capture_pid" 2>/dev/null || true; fi; rm -f -- "$capture_dir/packets" "$capture_dir/stderr"; rmdir -- "$capture_dir"' EXIT
    ip -n vpc-web neigh flush dev eth0
    ip netns exec vpc-web timeout 8 tcpdump -n -l -i eth0 -c 2 'arp and host 10.0.1.20' > "$capture_dir/packets" 2> "$capture_dir/stderr" &
    capture_pid=$!
    for (( attempt=0; attempt<50; attempt++ )); do
        if grep -q 'listening on' "$capture_dir/stderr"; then ready=1; break; fi
        kill -0 "$capture_pid" 2>/dev/null || break
        sleep 0.1
    done
    if (( ! ready )); then
        cat "$capture_dir/stderr" >&2
        die 'Packet capture did not become ready'
    fi
    ip netns exec vpc-web ping -n -c 1 -W 2 10.0.1.20 >/dev/null
    if ! wait "$capture_pid"; then
        capture_pid=''
        cat "$capture_dir/stderr" >&2
        die 'ARP capture did not complete'
    fi
    capture_pid=''
    cat "$capture_dir/packets"
    grep -q 'Request who-has 10.0.1.20 tell 10.0.1.10' "$capture_dir/packets" || die 'ARP request missing'
    grep -q 'Reply 10.0.1.20 is-at' "$capture_dir/packets" || die 'ARP reply missing'
    rm -- "$capture_dir/packets" "$capture_dir/stderr"
    rmdir -- "$capture_dir"
    trap - EXIT
}

case "${1:-help}" in
    help|-h|--help) usage; exit 0 ;;
    up|down|status|check|arp) action=$1 ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# == 1 ]] || die 'Expected exactly one command'
[[ $(uname -s) == Linux ]] || die 'Run this inside Linux'
(( EUID == 0 )) || die 'Root is required; use sudo'
for command in ip sysctl stat flock grep; do need "$command"; done
exec {lock_fd}> /run/vpc-networking-lab.lock
flock -n "$lock_fd" || die 'Another lab command is running'
trap 'exit 130' INT
trap 'exit 143' TERM

case "$action" in
    arp) capture_arp ;;
    *) "$action" ;;
esac
