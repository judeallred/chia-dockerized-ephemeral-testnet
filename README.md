# Chia ephemeral testnet in Docker

A Chia ephemeral PoS2 testnet node, farmer, harvester, wallet and solver in one
container, with a dashboard and an RPC gateway on one port.

```sh
./testnet up
open http://localhost:8900
```

That joins the testnet in [`default/initial-config.zip`](default/initial-config.zip), currently
`test-2026-11-19-1-chia`. Testnets are replaced often, so if the node finds no peers or stops getting blocks,
get the latest zip from the Discord channel and run `./testnet up <zip>`. A config in `./config` always wins over the
bundled one. The first build takes a few minutes. The node
then finds its peers and syncs; small v1 starter plots are made once it is synced.

**New testnet?** Drop the new `initial-config.zip` anywhere on the dashboard, or run `./testnet up` with it.
The box switches over. Your key and plots carry over, and chain data is kept per network.
If the branch moved too, run `./testnet update`.

## What is on the port

| Path | What |
| --- | --- |
| `/` | Dashboard: [chia-testnet-monitor](https://github.com/greimela/chia-testnet-monitor), cloned at build time and unmodified, plus this box's services, plots, RPC console and log |
| `/plotter/` | Browser plotter for v2 plots: [chia-plotter-wasm](https://github.com/judeallred/chia-plotter-wasm), cloned and built into the image |
| `/rpc/<service>/<endpoint>` | Chia RPC without certificates: `full_node`, `wallet`, `farmer`, `harvester`, `solver` |
| `/api/state`, `/api/box` | The dashboard's data as JSON |
| `/api/plots/<name>` | `PUT` a finished plot file |
| `/api/config` | `PUT` a new `initial-config.zip` or `.yaml` |
| `/metrics`, `/healthz` | Prometheus metrics, health check |

```sh
curl localhost:8900/rpc/full_node/get_blockchain_state
curl -X POST localhost:8900/rpc/wallet/get_wallet_balance -d '{"wallet_id": 1}'
```

`GET` works for `get_*` endpoints; everything else is `POST`. Port 58445 is the Chia peer protocol, so a
light wallet on your machine can use the node.

## Plots

Plots go in `./plots`, which the harvester scans every 30 seconds.

- **v1**: two k25 plots (about 0.7 GB each) are made automatically. `AUTO_V1_PLOTS=0` turns that off,
  `./testnet box plot-v1 5` makes more. v1 plots stop farming at the v1 cut-off shown on the dashboard.
- **v2 in the browser**: open `/plotter/` in Chrome or Edge. Your keys are filled in. Choose the `plots`
  folder as the output. It uses WebGPU, which a Linux container on a Mac cannot. A k28 plot takes about
  11 GB of memory while it is made, so close other things first.
- **v2 with cpos or xchplot2**: use the farmer and pool public keys from the dashboard and write to `./plots`.
- **v2 in the container**: `./testnet box plot-v2 2`. Needs 16 GB of RAM for Docker.

The testnet asks for at most 30 GB of v2 plots per person. The box enforces that for uploads and
`plot-v2`; it can't for other plotters, so the dashboard warns instead.

## Keys

A new key is made on first start and kept in the `data` volume. To use your own **testnet** key, mount a
file with its mnemonic at `/run/secrets/mnemonic` before the first start. Never use a mainnet key.
`./testnet box mnemonic` shows the words.

## Safety

The gateway can spend from the testnet wallet, so compose publishes it on 127.0.0.1 only. It refuses
unknown `Host` headers and cross-origin browser requests, and blocks endpoints that reveal or delete keys
or delete plots. Mainnet configs are refused. To reach it from elsewhere, set `API_TOKEN` and `ALLOWED_HOSTS`, and
put TLS in front.

| Variable | Default | |
| --- | --- | --- |
| `BOX_PORT` / `P2P_PORT` | 8900 / 58445 | Host ports |
| `AUTO_V1_PLOTS` | 2 | Starter v1 plots |
| `CHIA_REF` | hardfork-testnet | chia-blockchain branch |
| `API_TOKEN` | | Bearer token for RPC, uploads and restarts |
| `ALLOWED_HOSTS` | localhost and friends | Accepted `Host` names |
| `CORS_ORIGINS` | | Other web origins allowed to call the API, e.g. `http://localhost:3000` |
| `UNSAFE_RPC` | | `1` allows the blocked endpoints |

Put them in `.env`.

## Good to know

- The node may log `failed fetching 0 to 31 from peers` and restart its sync a few times at first. That
  happens when a block arrives while the weight proof is checked. It retries until it gets through.
- On this branch the farmer doesn't connect to the solver by itself; the box does it, and again after restarts.
- Docker networks usually have no IPv6, so connections to IPv6 peers and `IN AAAA` lookups fail in the log. That is
  harmless.
- The wallet only talks to the node in the container, so it syncs after the node does.
- Old networks' data: `./testnet box prune`.

## License

Apache 2.0, see [LICENSE](LICENSE). The image also contains chia-blockchain, chia-testnet-monitor and
chia-plotter-wasm, each under its own Apache 2.0 license.
