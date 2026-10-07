#!/usr/bin/env bash
set -Eeuo pipefail
scratch=$(mktemp -d)
configuration="$scratch/postgresql.conf"
trap 'rm -f -- "$configuration" "$scratch/postgresql.auto.conf" "$scratch/negative.log"; rmdir -- "$scratch"' EXIT
chmod 755 "$scratch"
touch "$configuration" "$scratch/postgresql.auto.conf"
chmod 644 "$configuration" "$scratch/postgresql.auto.conf"

parser=(/usr/lib/postgresql/16/bin/postgres -D "$scratch" -c "config_file=$configuration")
if (( EUID == 0 )); then
    parser=(runuser -u postgres -- "${parser[@]}")
fi
# Reproduce the old numeric-looking, unquoted IPv4 value with the real tools.
command pg_conftool 16 main "$configuration" set listen_addresses 10.0.3.10
if "${parser[@]}" -C listen_addresses > "$scratch/negative.log" 2>&1; then
    printf 'FAIL: PostgreSQL accepted the unquoted address\n' >&2
    exit 1
fi
[[ $(< "$scratch/negative.log") == *'syntax error'* ]]

# Parse the actual config block from the rendered startup script.
awk '
    /^cat > .*<<.PGCONFIG./ { copying = 1; next }
    copying && $0 == "PGCONFIG" { copying = 0; next }
    copying { print }
' "$1" > "$configuration"
[[ $("${parser[@]}" -C listen_addresses) == '10.0.3.10' ]]
[[ $("${parser[@]}" -C password_encryption) == 'scram-sha-256' ]]
printf 'PASS: PostgreSQL rejects the old address and parses rendered bootstrap settings\n'
