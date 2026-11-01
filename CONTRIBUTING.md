# Contributing to Cerberus Core

Thanks for your interest. Cerberus is young, and reports from real vans help
most.

## Issues: welcome now

Open an issue for a bug, a question, or support for hardware Cerberus doesn't
read yet. Useful details:

- **Your system:** the GX model and firmware, batteries, solar controller,
  inverter/charger.
- **What happened** and what you expected.
- **Logs** from `journalctl -u cerberus` (Linux) or `soak/cerberus.log`
  (macOS), and the Health screen.

Remove anything you don't want public first: your GX's portal ID, addresses,
location, passwords.

## Pull requests: after the contributor agreement

Cerberus Core is AGPL-3.0-or-later, and KNM Technology also offers it under
other terms (the hardware bundle and hosted services). That needs every
contributor to sign a **Contributor License Agreement (CLA)**. You keep your
copyright; the CLA gives KNM Technology the right to distribute your
contribution under those terms too.

The CLA is being finalised. **Until it's published here, we can't merge
outside pull requests.** Open an issue to discuss a change in the meantime.

## How the code is held together

- **Tests:** every change comes with tests, and `python -m pytest -q` passes.
  CI runs them on Ubuntu and macOS.
- **Read-only to Victron:** Cerberus never publishes `W/` topics to a GX.
- **Findings are deterministic rules** with an explanation, advice and
  evidence.
- **Secrets stay in `.env`:** never in config, code, logs or tests. Fixtures
  from real systems have their identifiers replaced.
