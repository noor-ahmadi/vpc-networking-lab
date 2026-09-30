#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
cd "$(dirname "$0")/.."

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
expect_failure() {
    if "$@"; then fail "Unexpected success: $*"; fi
}

(( EUID == 0 )) || fail 'Run in a disposable Linux environment as root'
[[ ! -e /run/vpc-networking-lab ]] || fail 'A lab already exists'
[[ ! -e /run/netns/vpc-sentinel ]] || fail 'Test namespace already exists'
for name in vpc-switch vpc-router vpc-web vpc-nat vpc-app vpc-db vpc-edge vpc-internet; do
    [[ ! -e /run/netns/$name ]] || fail "$name already exists"
done

scratch=$(mktemp -d)
busy_pid=''
cleanup() {
    local result=$?
    trap - EXIT
    if [[ -n "$busy_pid" ]]; then kill "$busy_pid" 2>/dev/null || true; wait "$busy_pid" 2>/dev/null || true; fi
    bash ./lab.sh down || true
    # These two names are created only by this test, after the preflight above.
    if [[ -e /run/netns/vpc-web ]]; then ip netns delete vpc-web; fi
    if [[ -e /run/netns/vpc-sentinel ]]; then ip netns delete vpc-sentinel; fi
    rm -f -- "$scratch/before" "$scratch/after" "$scratch/arp" "$scratch/manifest"
    rmdir -- "$scratch"
    exit "$result"
}
trap cleanup EXIT

snapshot_host() {
    ip -j link show
    ip -j address show | sed -E 's/"(valid_life_time|preferred_life_time)":[0-9]+/"\1":0/g'
    ip -j route show table all
    ip -j rule show
    ip netns list | sort
    sysctl -n net.ipv4.ip_forward net.ipv4.conf.all.rp_filter
    nft --stateless list ruleset
}

snapshot_host > "$scratch/before"
ip netns add vpc-sentinel
sentinel_identity=$(stat -Lc '%d:%i' /run/netns/vpc-sentinel)

# Names alone must never grant permission to overwrite or delete a namespace.
ip netns add vpc-web
collision_identity=$(stat -Lc '%d:%i' /run/netns/vpc-web)
expect_failure bash ./lab.sh up
bash ./lab.sh down
[[ $(stat -Lc '%d:%i' /run/netns/vpc-web) == "$collision_identity" ]] || fail 'Foreign namespace was changed'
ip netns delete vpc-web
printf 'PASS: conflicting namespace is preserved\n'

# Simulate a kernel/tool failure after some real namespaces have been created.
expect_failure bash -c '
    ip() {
        if [[ $* == "netns add vpc-app" ]]; then return 23; fi
        command ip "$@"
    }
    export -f ip
    exec bash ./lab.sh up
'
[[ ! -e /run/vpc-networking-lab ]] || fail 'Failed setup left ownership state behind'
for name in vpc-switch vpc-router vpc-web vpc-nat vpc-app vpc-db vpc-edge vpc-internet; do
    [[ ! -e /run/netns/$name ]] || fail 'Failed setup left a namespace behind'
done
printf 'PASS: partial setup failure rolls back created namespaces\n'

for cycle in 1 2; do
    printf '\nLifecycle cycle %s\n' "$cycle"
    bash ./lab.sh up
    bash ./lab.sh status
    bash ./lab.sh check
    python3 -u tests/traffic.py
    python3 -u tests/services.py
    # A healthy off-VPC destination exists only through the router's main table.
    ip -n vpc-web address add 192.0.2.10/32 dev lo
    ip -n vpc-router route add 192.0.2.10/32 via 10.0.1.10 dev public
    ip netns exec vpc-router ping -n -c 1 -W 2 192.0.2.10 >/dev/null
    for namespace in vpc-db vpc-app vpc-nat; do
        if ip netns exec "$namespace" ping -n -c 1 -W 2 192.0.2.10 >/dev/null; then
            fail "$namespace escaped its subnet policy through the main table"
        fi
    done
    printf 'PASS: subnet policies block a reachable destination in the main table\n'

    if (( cycle == 1 )); then
        # An explicit private-table route must not grant the database access.
        ip -n vpc-router route add table 102 192.0.2.10/32 via 10.0.1.10 dev public
        ip netns exec vpc-app ping -n -c 1 -W 2 192.0.2.10 >/dev/null
        expect_failure ip netns exec vpc-db ping -n -c 1 -W 2 192.0.2.10
        ip -n vpc-router route delete table 102 192.0.2.10/32
        printf 'PASS: a route added to one subnet table stays scoped to that subnet\n'

        # Without the terminal route, a failed lookup falls through to main.
        ip -n vpc-router route delete table 103 unreachable default
        ip netns exec vpc-db ping -n -c 1 -W 2 192.0.2.10 >/dev/null
        expect_failure bash ./lab.sh check
        ip -n vpc-router route add table 103 unreachable default
        expect_failure ip netns exec vpc-db ping -n -c 1 -W 2 192.0.2.10
        printf 'PASS: removing the isolated guard reproduces a leak; restoring it blocks the leak\n'
    fi
    bash ./lab.sh check
    ip -n vpc-router route delete 192.0.2.10/32
    ip -n vpc-web address delete 192.0.2.10/32 dev lo

    # A reply from a different subnet requires forwarding and both host routes.
    ip -n vpc-app neigh flush dev eth0
    ip netns exec vpc-app ping -n -c 1 -W 2 10.0.3.10 >/dev/null
    ip -n vpc-app neigh show 10.0.2.1 dev eth0 | grep -q 'lladdr'
    [[ -z $(ip -n vpc-app neigh show 10.0.3.10 dev eth0) ]] || fail 'App resolved the remote host instead of its gateway'
    printf 'PASS: routed traffic resolves the gateway MAC, not the remote host\n'
    bash ./lab.sh arp | tee "$scratch/arp"
    grep -q 'Request who-has 10.0.1.20 tell 10.0.1.10' "$scratch/arp"
    grep -q 'Reply 10.0.1.20 is-at' "$scratch/arp"
    cp /run/vpc-networking-lab/namespaces "$scratch/manifest"
    expect_failure bash ./lab.sh up
    cmp /run/vpc-networking-lab/namespaces "$scratch/manifest"

    if (( cycle == 1 )); then
        # A live namespace must not silently survive behind a deleted name.
        ip netns exec vpc-app sleep 30 &
        busy_pid=$!
        for (( attempt=0; attempt<50; attempt++ )); do
            if ip netns pids vpc-app | grep -qx "$busy_pid"; then break; fi
            sleep 0.02
        done
        ip netns pids vpc-app | grep -qx "$busy_pid" || fail 'Busy namespace fixture did not start'
        expect_failure bash ./lab.sh down
        [[ -e /run/netns/vpc-switch ]] || fail 'Cleanup partially removed the busy lab'
        kill "$busy_pid"
        wait "$busy_pid" 2>/dev/null || true
        busy_pid=''
        printf 'PASS: cleanup refuses active namespace processes\n'

        # Moving a live port to the wrong bridge must make check fail.
        ip -n vpc-switch link set app master br-public
        expect_failure bash ./lab.sh check
        ip -n vpc-switch link set app master br-private
        bash ./lab.sh check
        printf 'PASS: connectivity checks detect and recover from a miswired port\n'

        ip netns exec vpc-router sysctl -q -w net.ipv4.ip_forward=0
        ip netns exec vpc-web ping -n -c 1 -W 2 10.0.1.20 >/dev/null
        expect_failure bash ./lab.sh check
        ip netns exec vpc-router sysctl -q -w net.ipv4.ip_forward=1
        bash ./lab.sh check
        printf 'PASS: forwarding failure breaks routed traffic while local peers still work\n'

        ip -n vpc-db route delete default
        ip netns exec vpc-db ping -n -c 1 -W 2 10.0.3.1 >/dev/null
        expect_failure bash ./lab.sh check
        ip -n vpc-db route add default via 10.0.3.1 dev eth0
        bash ./lab.sh check
        printf 'PASS: checks detect and recover from a missing database return route\n'

        # Main-table routes still work, but check must notice a missing policy.
        ip -n vpc-router rule delete priority 102
        ip netns exec vpc-app ping -n -c 1 -W 2 10.0.3.10 >/dev/null
        expect_failure bash ./lab.sh check
        ip -n vpc-router rule add priority 102 iif private lookup 102
        bash ./lab.sh check
        printf 'PASS: checks detect and recover from a missing subnet policy rule\n'
    fi

    bash ./lab.sh down
    bash ./lab.sh down
    for name in vpc-switch vpc-router vpc-web vpc-nat vpc-app vpc-db vpc-edge vpc-internet; do
        [[ ! -e /run/netns/$name ]] || fail "$name survived teardown"
    done
    [[ ! -e /run/vpc-networking-lab ]] || fail 'Ownership state survived teardown'
    [[ $(stat -Lc '%d:%i' /run/netns/vpc-sentinel) == "$sentinel_identity" ]] || fail 'Unrelated namespace changed'
done

# Replace an owned namespace with an unrelated namespace of the same name.
bash ./lab.sh up
ip netns delete vpc-web
ip netns add vpc-web
replacement_identity=$(stat -Lc '%d:%i' /run/netns/vpc-web)
expect_failure bash ./lab.sh down
[[ $(stat -Lc '%d:%i' /run/netns/vpc-web) == "$replacement_identity" ]] || fail 'Replaced namespace was deleted'
[[ -e /run/netns/vpc-switch ]] || fail 'Ownership failure partially removed the lab'
ip netns delete vpc-web
bash ./lab.sh down
printf 'PASS: replaced namespace is preserved\n'

ip netns delete vpc-sentinel
snapshot_host > "$scratch/after"
diff -u "$scratch/before" "$scratch/after" || fail 'Host network configuration changed'
printf '\nPASS: two full cycles, ownership guards, ARP, subnet policies, services and access rules, NAT captures and faults, and unchanged host network\n'
