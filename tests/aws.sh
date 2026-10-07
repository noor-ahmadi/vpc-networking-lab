#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."

python3 tests/aws_check.py

terraform -chdir=aws fmt -check -recursive
terraform -chdir=aws init -backend=false -input=false -lockfile=readonly
terraform -chdir=aws validate
terraform -chdir=aws test

# Offline fixtures only. No credentials or real deployment inputs are needed.
export TF_VAR_ami_id=ami-00000000000000001
export TF_VAR_operator_cidr=198.51.100.10/32
export TF_VAR_ssh_public_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA offline-test'
export TF_VAR_db_password=000000000000000000000000000000000000000000000000
export TF_VAR_bootstrap_database=false
export AWS_EC2_METADATA_DISABLED=true

scratch=$(mktemp -d)
trap 'rm -f -- "$scratch/rendered.json" "$scratch/web.sh" "$scratch/app.sh" "$scratch/db.sh"; rmdir -- "$scratch"' EXIT
printf 'nonsensitive(jsonencode(local.user_data))\n' | terraform -chdir=aws console > "$scratch/rendered.json"
python3 - "$scratch" <<'PY'
import json
from pathlib import Path
import sys
directory = Path(sys.argv[1])
scripts = json.loads(json.loads((directory / 'rendered.json').read_text()))
for role in ('web', 'app', 'db'):
    (directory / f'{role}.sh').write_text(scripts[role])
PY
for role in web app db; do
    bash -n "$scratch/$role.sh"
    shellcheck "$scratch/$role.sh"
done
bash tests/aws_postgres.sh "$scratch/db.sh"
printf 'PASS: Terraform contracts and three rendered startup scripts\n'
