# tmodloader-server-flux

The tModLoader (Terraria with mods) dedicated server for [Flux](https://runonflux.io), as
`runonflux/tmodloader-server-flux`. tModLoader from its GitHub releases (the server needs no Steam
account and no Terraria licence), Workshop mods with SteamCMD (anonymous), the .NET 8 runtime in
the image, and a supervisor that:

- installs the tModLoader build `TML_VERSION` names (`stable`, `preview` or an exact version)
- downloads the Workshop mods asked for **and every mod they require** (read off each item's
  "Required items"), keeps them updated, and writes `Mods/enabled.json`; uploaded `.tmod` files
  are mods too. Players get the mods from the server when they join (tModLoader syncs them)
- writes `serverconfig.txt` from the environment and the hub's panel (world, size, difficulty,
  seed, players, password, MOTD, Journey permissions...)
- saves on stop (`exit` on the console: 2.4 s measured with Calamity; tModLoader also saves on
  SIGTERM) and every `TML_AUTOSAVE_MINUTES`
- follows a newer tModLoader and mod updates on the Workshop: warns the players in chat for 5
  minutes (skipped on an empty server), then restarts onto them; `TML_RESTART_TIME` = a daily one
- makes a backup of the worlds, mod configs and settings on every start and every 6 hours
- gives the hub a console (`flux-console`) and JSON commands (`flux-tml`)

## Volumes

| Path | Flux mount | What |
|---|---|---|
| `/mnt/tml/data` | `g:` (synced) | the tModLoader save directory: `Worlds/` (`.wld` + `.twld`, and their `.bak`), `Mods/` (`enabled.json`, uploaded `.tmod`), `ModConfigs/`, `banlist.txt`, and `flux/` (log, state, settings, mods list, backups, the game's logs) |
| `/mnt/tml/server` | `ml:server` (local) | the tModLoader build (~170 MB) and the Workshop downloads (Calamity and its music: ~1.1 GB). Downloaded again on a new node |

Spec: `g:/mnt/tml/data|ml:server:/mnt/tml/server`.

## Ports

`TML_PORT` (7777), **TCP**: Terraria's protocol. No query protocol exists; the hub reads the
server through its console.

## Environment

A variable that is **set** is applied on every start; one that is not set leaves the panel's
value (`flux/settings.json`), and then the default. Two exceptions: the size, difficulty and seed of
a **new** world, where the panel's New World choice wins (the variables are the default); and an
**empty** `TML_MOTD`, `TML_PASSWORD` or `TML_SEED`, which counts as unset.

| Variable | | |
|---|---|---|
| `TML_VERSION` | | `stable` (default), `preview`, or a version (`v2026.08.3.0`) |
| `TML_WORLD_NAME` | `worldname` | the world's name; also its file (`Worlds/<name>.wld`) unless the panel picked another world |
| `TML_WORLD` | `world` | the world file to load (a name in `Worlds/`) |
| `TML_WORLD_SIZE` | `autocreate` | `small`, `medium` (default), `large`. A NEW world only |
| `TML_DIFFICULTY` | `difficulty` | `classic`, `expert`, `master`, `journey`. A NEW world only |
| `TML_SEED` | `seed` | text, secret seeds included (`not the bees`). A NEW world only |
| `TML_MAX_PLAYERS` | `maxplayers` | 1-255 (default 8) |
| `TML_PASSWORD` | `password` | empty = anyone with the address can join |
| `TML_MOTD` | `motd` | |
| `TML_MODS` | | Workshop IDs or links, comma separated. Their dependencies are added |
| `TML_SECURE`, `TML_LANGUAGE`, `TML_NPCSTREAM` | `secure`, `language`, `npcstream` | |
| `TML_AUTOSAVE_MINUTES` | | 10 (0 = off) |
| `TML_RESTART_TIME` | | `HH:MM` (container time zone, `TZ`), a daily restart. Empty = none |

The world's evil (Corruption/Crimson) cannot be chosen by a server that creates the world: it is
random. A world made in the game and uploaded keeps its own.

Supervisor knobs: `FLUX_BACKUP_HOURS` (6), `FLUX_BACKUP_KEEP` (5), `FLUX_UPDATE_CHECK_MINUTES` (60,
0 = off), `FLUX_WARN_MINUTES` (5), `FLUX_RESTART_MAX_ATTEMPTS` (5 in `FLUX_RESTART_WINDOW` 3600 s,
then exit 42).

## For the hub

| Command | |
|---|---|
| `flux-console <command>` | one console command, its answer printed: `playing`, `kick <name>`, `ban <name>`, `say <text>`, `save`, `dawn`/`noon`/`dusk`/`midnight`, `settle`, `time`, `seed`, `modlist`, `motd <text>`, `password <pw>`... |
| `flux-tml status` | `state`, `players` (`name`, `ip`), `max_players`, `world`, `mods` loaded, `version`, `pending` |
| `flux-tml settings` / `set-settings '<json>'` | the panel's settings, the env that overrides them, the choices |
| `flux-tml worlds` / `delete-world <name>` | the worlds in `Worlds/`, the active one |
| `flux-tml mods` / `set-mods '{"workshop":[...],"disabled":[...]}'` | every mod (Workshop or uploaded): name, version, side, dependencies, who requires it, on or off |
| `flux-tml mod-search-deps <id>` | what a Workshop item requires, recursively |
| `flux-tml modconfigs` / `set-modconfig <file> '<json>'` | the mods' config files |
| `flux-tml bans` / `unban <name or IP>` | `banlist.txt` |
| `flux-tml restart` | save and start again in place (applies the above) |

## Measured (tModLoader v2026.08.3.0, Terraria 1.4.4.9)

- No mods: a small world made in ~20 s, ~0.75-1 GB of RAM.
- Calamity (+ its music) and Magic Storage (+ SerousCommonLib): 1.1 GB downloaded, loaded in ~20 s
  once downloaded; a new large Master world generated in ~90 s; ~1.6 GB of RAM idle. Hosts quote
  4 GB for Calamity in play and 6-8 GB for big modpacks.
- `exit` and SIGTERM both save (`Saving before exit...`); `docker stop`: 2.4 s.
- An anonymous `app_update 1281930` "succeeds" with nothing installed: the server comes from
  GitHub. Workshop items download anonymously.
- steamcommunity.com answers 429 to bursts for a while: the dependency lookups are retried and
  cached for a day.

## Versions

- **1.0.1** (2026-10-11): a new world made from the panel keeps its own size, difficulty and seed
  (the spec's variables are the default); an empty MOTD, password or seed variable no longer
  erases the panel's.
- **1.0.0** (2026-10-11): first release.
