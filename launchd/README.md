# launchd supervision (macOS)

macOS counterpart to `systemd/cerberus.service`. Keeps the collector running
and restarts it if it dies.

## Install

```sh
cp launchd/com.cerberus.collector.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cerberus.collector.plist
```

## Operate

```sh
launchctl print gui/$(id -u)/com.cerberus.collector       # state, pid, run count, last exit
launchctl kickstart -k gui/$(id -u)/com.cerberus.collector # restart now
launchctl bootout gui/$(id -u)/com.cerberus.collector      # stop and unload
tail -f soak/cerberus.log                                  # logs (stdout+stderr land here)
```

After editing the plist, `bootout` then `bootstrap` again — launchd caches the
loaded copy, so editing the file alone changes nothing.

Credentials come from `.env` via `bin/run-collector.sh`, never from the plist —
a plist in the home directory is far more readable than a 0600 `.env`.

## What this does and does not buy you

It survives **crashes**: `KeepAlive` respawns the process whenever it exits,
throttled to one start per 10s. Verified by `kill -9` — launchd brought it back
with a new pid.

It does **not**, on this machine, survive a **reboot**. Two reasons:

1. A LaunchAgent starts at *login*, not at boot.
2. FileVault is enabled, so a rebooted Mac sits at the unlock screen until
   someone types the password. macOS also disables auto-login while FileVault
   is on, so the usual workaround is unavailable.

For unattended reboot recovery you need a **LaunchDaemon** in
`/Library/LaunchDaemons` (runs at boot, before login, as root — add a
`UserName` key to drop privileges). Even then, FileVault holds the boot at the
unlock prompt: the disk cannot be read until it is unlocked. Genuinely
unattended operation means either disabling FileVault or moving off this host.

## Sleep is the bigger problem

A supervised process still collects nothing while the machine is asleep.
Measured on 2026-08-08: the Mac was on battery with `sleep 1` (one minute
idle), DarkWaking roughly every 15 minutes for ~20 seconds. The collector was
blind for 7.7 of 8 hours — **96% data loss** — and every gap lined up exactly
with a sleep/wake pair. launchd does not help here; the process is alive and
suspended, not dead.

Options, in rough order of preference:

- Run on the 12V mini PC the platform spec targets. It does not sleep, has no
  FileVault prompt, and uses the systemd unit already in this repo. This is the
  real fix; everything else is a workaround for dev hardware.
- Hold a power assertion for the life of the collector, by changing the last
  line of `bin/run-collector.sh` to `exec caffeinate -i -s "$HERE/.venv/bin/python" ...`.
  Costs van battery, and lid-close still sleeps regardless.
- `sudo pmset -a sleep 0` — machine-wide, affects everything, same power cost.

Check current behaviour with `pmset -g custom` and `pmset -g log | grep -E 'Sleep|Wake'`.
