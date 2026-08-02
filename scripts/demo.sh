#!/bin/sh
set -eu

repository="$(
  CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd
)"

python3 "$repository/scripts/generate_demo.py" --check
cat "$repository/evidence/demo-transcript.txt"
