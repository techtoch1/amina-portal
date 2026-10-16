#!/usr/bin/env bash
# Pull the latest code and reinstall: bash ~/amina-portal/deploy/update.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." && git pull --ff-only
sudo tar --exclude=.git --exclude=data --exclude=.venv --exclude=__pycache__ --exclude=.env -C . -cf - . | sudo tar -C /opt/amina -xf -
sudo /opt/amina/.venv/bin/pip install -q -r /opt/amina/requirements.txt || { echo "pip install failed"; exit 1; }
sudo systemctl restart amina && echo "Updated."
