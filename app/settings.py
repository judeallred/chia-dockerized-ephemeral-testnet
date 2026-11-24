"""Paths and environment settings."""

from __future__ import annotations

import os
from pathlib import Path

DATA = Path(os.environ.get("BOX_DATA", "/data"))
CONFIG_DIR = Path(os.environ.get("BOX_CONFIG", "/config"))
DEFAULT_CONFIG = Path("/opt/box/default/initial-config.zip")  # used while /config is empty
PLOTS_DIR = Path(os.environ.get("BOX_PLOTS", "/plots"))
KEYS_ROOT = Path(os.environ.get("CHIA_KEYS_ROOT", "/data/keys"))
CHIA_SRC = Path("/chia-blockchain")
VENV_BIN = CHIA_SRC / ".venv/bin"
PYTHON = VENV_BIN / "python"
MONITOR_SRC = Path("/opt/monitor")
PLOTTER_DIST = Path("/opt/plotter")
STATIC = Path(__file__).resolve().parent / "static"
MNEMONIC_FILE = Path(os.environ.get("MNEMONIC_FILE", "/run/secrets/mnemonic"))

PORT = int(os.environ.get("BOX_PORT", "8900"))
MONITOR_PORT = 8899


def _csv(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Host headers the port answers to. Anything else is refused, which stops DNS rebinding from a web page.
ALLOWED_HOSTS = {h.lower() for h in _csv("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testnet,host.docker.internal")}
# Other web origins that may call the API from a browser, e.g. http://localhost:3000.
CORS_ORIGINS = set(_csv("CORS_ORIGINS"))
# When set, /rpc, uploads and controls need "Authorization: Bearer <token>".
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
UNSAFE_RPC = _flag("UNSAFE_RPC")

LOG_LEVEL = os.environ.get("CHIA_LOG_LEVEL", "INFO").upper()
AUTO_V1_PLOTS = int(os.environ.get("AUTO_V1_PLOTS", "2"))
V1_K = int(os.environ.get("V1_K", "25"))
AUTO_V2_PLOTS = int(os.environ.get("AUTO_V2_PLOTS", "2"))
V2_K = int(os.environ.get("V2_K", "28"))  # only k28 can win; smaller sizes are for testing this code
V2_STRENGTH = int(os.environ.get("V2_STRENGTH", "2"))
# Free memory a k28 plot needs. Each step down in k needs about half.
V2_MIN_FREE_GB = float(os.environ.get("V2_MIN_FREE_GB", "11")) / 2 ** (28 - V2_K)
# The testnet organisers ask for at most 30 GB of v2 plots per person.
V2_LIMIT_GB = float(os.environ.get("V2_LIMIT_GB", "30"))
