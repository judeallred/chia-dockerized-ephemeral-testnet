# syntax=docker/dockerfile:1

# The browser plotter. Its WASM is committed, so this only builds the page.
FROM --platform=$BUILDPLATFORM node:24-bookworm-slim AS plotter
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && npm install -g pnpm@12
ARG PLOTTER_REPO=https://github.com/judeallred/chia-plotter-wasm.git
# Set to a commit hash to pin it; ./testnet update passes the newest.
ARG PLOTTER_COMMIT=
RUN git clone -q "$PLOTTER_REPO" /src && cd /src \
 && if [ -n "$PLOTTER_COMMIT" ]; then git checkout -q "$PLOTTER_COMMIT"; fi
WORKDIR /src/web
RUN pnpm install --frozen-lockfile && pnpm exec astro build \
 && rm -f dist/_headers && test -f dist/wasm/pos2w64.wasm && git rev-parse HEAD > dist/COMMIT

FROM python:3.12-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential && rm -rf /var/lib/apt/lists/*

ARG CHIA_REPO=https://github.com/Chia-Network/chia-blockchain.git
ARG CHIA_REF=hardfork-testnet
# Set to a commit hash to pin the build (and to bust the cache when the branch moves; ./testnet update does this).
ARG CHIA_COMMIT=
WORKDIR /
RUN git clone --filter=blob:none -b "$CHIA_REF" "$CHIA_REPO" /chia-blockchain \
 && cd /chia-blockchain \
 && if [ -n "$CHIA_COMMIT" ]; then git checkout -q "$CHIA_COMMIT"; fi \
 && git rev-parse HEAD > /chia-blockchain/COMMIT
# What install.sh does, minus the GUI, UPnP and dev extras.
WORKDIR /chia-blockchain
RUN ./setup-poetry.sh -c python3 \
 && .penv/bin/poetry env use python3 \
 && .penv/bin/poetry sync --only main \
 && rm -rf .penv .git chia-blockchain-gui chia/_tests benchmarks \
 && .venv/bin/python -c "import chia.cmds.chia, chia.solver.start_solver, chia.full_node.start_full_node, chia.wallet.start_wallet"

# The dashboard: Acevail's testnet monitor, cloned and used as is.
ARG MONITOR_REPO=https://github.com/greimela/chia-testnet-monitor.git
ARG MONITOR_COMMIT=
RUN git clone -q "$MONITOR_REPO" /opt/monitor && cd /opt/monitor \
 && if [ -n "$MONITOR_COMMIT" ]; then git checkout -q "$MONITOR_COMMIT"; fi && git rev-parse HEAD > COMMIT

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /chia-blockchain /chia-blockchain
COPY --from=build /opt/monitor /opt/monitor
COPY --from=plotter /src/web/dist /opt/plotter
COPY bin/ /usr/local/bin/
COPY app /opt/box/app

# /data/current points at the active network, so `docker compose exec testnet chia ...` just works.
ENV PATH=/usr/local/bin:/chia-blockchain/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    PYTHONPATH=/opt/box \
    PYTHONUNBUFFERED=1 \
    CHIA_ROOT=/data/current \
    CHIA_KEYS_ROOT=/data/keys
VOLUME ["/data"]
EXPOSE 8900 58445
WORKDIR /opt/box
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD ["/chia-blockchain/.venv/bin/python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8900/healthz', timeout=4)"]
ENTRYPOINT ["tini", "--", "/chia-blockchain/.venv/bin/python", "-m", "app"]
