#!/bin/bash
# The status table (tools/status_report.py) for an installed box, from a laptop:
# runs the copy bin/install-linux.sh put in /opt/cerberus/tools, on the box, as
# the cerberus user, which alone can read the database and the owner's notes.
# Needs the sudoers rule in docs/install.md ("Status table").
#
#   tools/status-on-box.sh [host]      # default host: cerberus
set -euo pipefail

exec ssh -o BatchMode=yes "${1:-cerberus}" \
  sudo -n -u cerberus /opt/cerberus/.venv/bin/python /opt/cerberus/tools/status_report.py \
  --db /var/lib/cerberus/cerberus.db --config /etc/cerberus/config.yaml
