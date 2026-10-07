#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."

bash demo/run.sh

# Terminate during setup/presentation and require the ownership state to vanish.
set +e
timeout --signal=TERM --kill-after=15 8 bash demo/run.sh
result=$?
set -e
[[ $result == 124 ]] || { printf 'Expected timeout, got %s\n' "$result"; exit 1; }
[[ ! -e /run/vpc-networking-lab ]]
[[ -z $(ip netns list) ]]
printf 'PASS: interrupted demonstration removes its owned lab\n'
