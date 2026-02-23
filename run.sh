#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
source env/bin/activate
python run_webhook.py "$@"
