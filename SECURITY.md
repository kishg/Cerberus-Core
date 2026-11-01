# Security policy

Cerberus runs inside people's vans and RVs. It holds the Victron GX's Remote
Console password, the van's position and its daily patterns, so we take
reports seriously.

## Reporting a vulnerability

Please **don't open a public issue.** Report it privately through GitHub:
open the repository's **Security** tab and choose **Report a vulnerability**.
Include what you found, how to reproduce it, and what an attacker could do
with it.

You'll get an acknowledgement within a week. We'll agree a disclosure date
with you, and credit you unless you'd rather we didn't.

## In scope

- **The web UI and its API:** the settings PIN, sessions, the boundary
  between what is read-only and what needs the PIN, and the "with the van"
  checks.
- **Anything that would let Cerberus write to a Victron GX.** It must stay
  read-only.
- **Secret handling:** `.env`, the files beside `config.yaml`, logs, and the
  migration bundle.
- **The installers and services:** `bin/install-linux.sh`,
  `bin/migrate-to-box.sh`, the systemd units and the launchd plists.

## Supported versions

Only the latest `main` gets fixes for now.
