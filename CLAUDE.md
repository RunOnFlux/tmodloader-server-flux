# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Docker image repo**, not an application: the tModLoader dedicated server for Flux,
`runonflux/tmodloader-server-flux`. steamcmd/steamcmd:ubuntu-24 (for the Workshop), .NET 8,
tModLoader from GitHub, and a supervisor in bash. Same family as `~/work/corekeeper-server-flux`
and `~/work/unturned-server-flux`; read them for the patterns this one follows.

The rule that shapes everything: **the container supervises its own server.** PID 1 is
`flux-entrypoint.sh`, never the game.

## Commands

```bash
./tests/test-flux.sh
docker build -t tmodloader-server-flux:local .
./tests/test-image.sh                    # the scripts INSIDE the built image; CI gates the publish on it
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh
```

A change to what the image does bumps `VERSION` and gets a line under "Versions" in the README.

## The files

- `scripts/flux-entrypoint.sh`: PID 1. Per generation: `fluxtml.py install`, `sync-mods`, a
  backup, `apply`, the server on the console FIFO, then the watcher (ready, updates, daily
  restart), the autosaver and the backupper.
- `scripts/fluxtml.py`: every decision about the server's files and mods, and the hub's commands.
- `scripts/flux-console.sh`: `flux-console <command>`, one at a time (a lock), the answer from the
  log.
- `scripts/flux-lib.sh`: shared helpers. **Sourcing it must stay side-effect free.**

## Things that will bite you

- **Not from Steam.** An anonymous `app_update 1281930` reports success and installs nothing.
  tModLoader comes from its GitHub releases; only the Workshop goes through SteamCMD.
- **The server does not fetch dependencies.** A mod whose `modReferences` are missing is disabled
  at load ("Missing mod: X required by Y"). `sync-mods` reads "Required items" off each Workshop
  page (the keyless Web API does not give them) and cross-checks every `.tmod`'s references.
- **steamcommunity.com rate-limits** (429). Retried, and the answers cached a day.
- **Workshop items hold one folder per tModLoader release** (`2025.7/`, `2026.6/`): the newest not
  newer than the running tModLoader is the one that loads.
- **Find the server by `^dotnet tModLoader\.dll `.** A pattern without the anchor matches the
  shell that runs pgrep (it killed the test's own shell once).
- **Clients download the server's mods when they join**; the server's version wins.
- **The console's answers start with ": "** and arrive on the server's stdout, which is our log.
- **World evil cannot be chosen by autocreate** (random). Upload a world to choose it.
