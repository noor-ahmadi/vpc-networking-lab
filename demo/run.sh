#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
cd "$(dirname "$0")/.."

(( EUID == 0 )) || { printf 'Run in a disposable Linux environment as root.\n' >&2; exit 1; }
[[ ! -e /run/vpc-networking-lab ]] || { printf 'A lab already exists.\n' >&2; exit 1; }
[[ -z $(ip netns list) ]] || { printf 'Use a disposable environment without existing namespaces.\n' >&2; exit 1; }

services=''
fixture=''
created=false
scratch=$(mktemp -d)
cleanup() {
    local result=$?
    trap - EXIT
    for pid in "$fixture" "$services"; do
        if [[ -n "$pid" ]]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    if $created; then bash lab.sh down || result=1; fi
    rm -f -- "$scratch/services" "$scratch/fixture"
    rmdir -- "$scratch"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

show() {
    printf '\n$ '
    printf '%q ' "$@"
    printf '\n'
    sleep 1
    "$@"
    sleep 3
}
ready() {
    local pid=$1 log=$2
    for _ in {1..100}; do
        if grep -qx 'ready' "$log"; then return; fi
        kill -0 "$pid" || { cat "$log"; return 1; }
        sleep 0.1
    done
    cat "$log"
    printf 'Service readiness timed out.\n' >&2
    return 1
}

cat <<'TOPOLOGY'
VPC Networking Lab | recorded local IPv4 demonstration

outside 203.0.113.10 -> edge web mapping 203.0.113.20
  -> public Nginx 10.0.1.10:80 -> private app 10.0.2.10:8080
  -> isolated PostgreSQL 10.0.3.10:5432

private egress: app -> router table 102 -> NAT 10.0.1.20
  -> router table 101 -> edge mapping 203.0.113.30 -> outside
The outside fixture is sealed; these are documentation addresses.
TOPOLOGY
sleep 5
printf '\n$ bash lab.sh up\n'
bash lab.sh up
created=true
sleep 3
show ip -n vpc-router rule show
for table in 101 102 103; do show ip -n vpc-router route show table "$table"; done

printf '\nStarting the real Nginx, Python, and PostgreSQL services...\n'
python3 -u app/services.py > "$scratch/services" 2>&1 &
services=$!
ready "$services" "$scratch/services"
ip netns exec vpc-internet python3 -u tests/traffic.py serve 203.0.113.10 8080 > "$scratch/fixture" 2>&1 &
fixture=$!
ready "$fixture" "$scratch/fixture"

show ip netns exec vpc-internet python3 tests/services.py get 203.0.113.20 80 /message
show env PYTHONPATH=tests python3 -c \
    'from services import http; assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "Hello from the isolated subnet"; print("PASS: proxy returns a real PostgreSQL row")'
show env PYTHONPATH=tests python3 -c 'from traffic import capture_nat; capture_nat()'

printf '\nBreak only private external access; keep the internal DB path healthy.\n'
show bash lab.sh fault private-route
show ip -n vpc-router route show table 102
show env PYTHONPATH=tests python3 -c \
    'from traffic import request; request("vpc-internet", "203.0.113.10", peer="203.0.113.10"); request("vpc-app", "203.0.113.10", denied=True); print("PASS: healthy outside fixture; new app HTTP connection denied")'
show ip netns exec vpc-internet python3 tests/services.py get 203.0.113.20 80 /message
show env PYTHONPATH=tests python3 -c \
    'from services import http; assert http("vpc-internet", "203.0.113.20", 80, "/message")["message"] == "Hello from the isolated subnet"; print("PASS: internal PostgreSQL request survives the missing external route")'
show bash lab.sh repair private-route
show ip netns exec vpc-app python3 tests/traffic.py request 203.0.113.10 8080
show env PYTHONPATH=tests python3 -c \
    'from traffic import request; request("vpc-app", "203.0.113.10", peer="203.0.113.30"); print("PASS: repaired route restores fresh HTTP through both NAT translations")'

printf '\nEarlier live AWS evidence, recorded October 5 (this is a file, not a live deployment):\n'
show sed -n '17,30p' evidence/aws-faults-2026-10-05.md
printf '\nStopping this demo and removing its owned namespaces...\n'
kill "$fixture" "$services"
wait "$fixture" || true
wait "$services"
fixture=''
services=''
show bash lab.sh down
created=false
[[ -z $(ip netns list) ]] || { printf 'Namespaces remain.\n' >&2; exit 1; }
printf '\nPASS: demo completed; services stopped; no namespaces remain.\n'
sleep 3
