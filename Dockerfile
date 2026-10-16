# tModLoader (Terraria with mods) dedicated server image for Flux.
#
# Written from scratch. tModLoader from its GitHub releases (no Steam account or Terraria licence
# needed for the server), mods from the Steam Workshop with SteamCMD (anonymous), the .NET 8
# runtime it needs installed here instead of downloaded on every first start, and a supervisor
# that resolves the mods' dependencies, writes serverconfig.txt from the environment and the hub's
# panel, saves on stop and on a timer, follows tModLoader and mod updates with warnings in chat,
# keeps backups, and gives the hub a console and JSON status. See README.md.
# The base. CI builds it pinned to the digest it resolved and records that digest in the
# org.opencontainers.image.base.digest label, which the scheduled check compares against
# (.github/ci/needs-rebuild.sh).
ARG BASE_IMAGE=steamcmd/steamcmd:ubuntu-24
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG FLUX_IMAGE_VERSION=dev

# dotnet-runtime-8.0: what tModLoader.runtimeconfig.json asks for (8.0, no roll-forward); Ubuntu's
# package pulls libicu in (.NET fails without ICU). python3, NOT python3-minimal: fluxtml.py
# needs json, zipfile and urllib.
RUN apt-get update -y && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl procps tzdata python3 dotnet-runtime-8.0 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY scripts/flux-lib.sh scripts/flux-entrypoint.sh scripts/flux-console.sh scripts/fluxtml.py /opt/flux/
RUN chmod 0755 /opt/flux/*.sh /opt/flux/fluxtml.py && \
    ln -s /opt/flux/flux-console.sh /usr/local/bin/flux-console && \
    ln -s /opt/flux/fluxtml.py /usr/local/bin/flux-tml

# SteamCMD's self-update, done once here; the weekly rebuild keeps it current.
RUN steamcmd +quit >/dev/null 2>&1; test -d /root/.local/share/Steam

# .NET's first-run telemetry and banner: off.
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 FLUX_IMAGE_VERSION=${FLUX_IMAGE_VERSION}

VOLUME ["/mnt/tml/server", "/mnt/tml/data"]

# Terraria's protocol is TCP only.
EXPOSE 7777/tcp

HEALTHCHECK --interval=60s --timeout=10s --start-period=20m --retries=3 \
    CMD pgrep -f "^dotnet tModLoader\.dll " >/dev/null || exit 1

ENTRYPOINT []
CMD ["/opt/flux/flux-entrypoint.sh"]
