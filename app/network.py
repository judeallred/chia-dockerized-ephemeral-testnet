"""Finds the testnet's initial-config, checks it, and prepares a CHIA_ROOT for it."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app import settings as S

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_CONFIG_BYTES = 2_000_000
FORBIDDEN = {"mainnet"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class NetworkConfig:
    name: str
    genesis: str
    text: str
    sha: str
    source: str

    @property
    def root(self) -> Path:
        return S.DATA / "networks" / self.name


def parse(raw: bytes, source: str) -> NetworkConfig:
    """Reads a config from initial-config.zip or .yaml bytes. Nothing is extracted to disk."""
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("file is too large to be a chia config")
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos = [
                    i for i in z.infolist()
                    if i.filename.lower().endswith((".yaml", ".yml")) and "__MACOSX" not in i.filename
                ]
                if len(infos) != 1:
                    raise ConfigError(f"expected one .yaml in the zip, found {len(infos)}")
                if infos[0].file_size > MAX_CONFIG_BYTES:
                    raise ConfigError("the config in the zip is too large")
                raw = z.read(infos[0])
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:
            raise ConfigError(f"not a readable zip: {e}") from None
    try:
        text = raw.decode("utf-8")
        cfg: Any = yaml.safe_load(text)
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigError(f"not valid YAML: {str(e)[:200]}") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("selected_network"), str):
        raise ConfigError("no selected_network in this file; is it an initial-config.yaml?")
    name = cfg["selected_network"]
    if not NAME_RE.match(name):
        raise ConfigError(f"odd network name {name!r}")
    if name in FORBIDDEN:
        raise ConfigError("this config selects mainnet; this box only runs testnets")
    for section in ("full_node", "farmer", "harvester", "wallet", "solver"):
        if not isinstance(cfg.get(section), dict):
            raise ConfigError(f"config has no {section} section")
    try:
        genesis = str(cfg["network_overrides"]["constants"][name]["GENESIS_CHALLENGE"])
    except (KeyError, TypeError):
        raise ConfigError(f"config has no GENESIS_CHALLENGE for {name}") from None
    return NetworkConfig(name, genesis, text, hashlib.sha256(text.encode()).hexdigest(), source)


def candidates() -> list[Path]:
    """Config files in /config, newest first."""
    try:
        files = [p for p in S.CONFIG_DIR.iterdir() if p.is_file() and p.suffix.lower() in (".zip", ".yaml", ".yml")]
        return sorted((p for p in files if not p.name.startswith(".")), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def newest(use_default: bool) -> NetworkConfig | None:
    """The newest config in /config, or the bundled one if there is none and use_default is set."""
    found = candidates()[:1]
    if not found and use_default and S.DEFAULT_CONFIG.exists():
        found = [S.DEFAULT_CONFIG]
    for p in found:
        try:
            return parse(p.read_bytes(), "bundled default" if p == S.DEFAULT_CONFIG else p.name)
        except OSError as e:
            raise ConfigError(str(e)) from None
        except ConfigError as e:
            raise ConfigError(f"{p.name}: {e}") from None
    return None


def store_upload(raw: bytes, filename: str) -> NetworkConfig:
    """Saves an uploaded config as the active one; earlier files move to /config/old."""
    net = parse(raw, filename)
    old = S.CONFIG_DIR / "old"
    old.mkdir(parents=True, exist_ok=True)
    for p in candidates():
        target = old / f"{time.strftime('%Y%m%d-%H%M%S')}-{p.name}"
        shutil.move(str(p), target)
    tmp = S.CONFIG_DIR / ".initial-config.yaml.tmp"
    tmp.write_text(net.text)
    tmp.replace(S.CONFIG_DIR / "initial-config.yaml")
    return net


def chia_env(root: Path) -> dict[str, str]:
    return {**os.environ, "CHIA_ROOT": str(root), "CHIA_KEYS_ROOT": str(S.KEYS_ROOT)}


def chia(root: Path, *args: str, input_text: str = "", timeout: float = 180) -> str:
    """Runs a chia CLI command. Its output may hold secrets, so callers decide what to log."""
    p = subprocess.run(
        [str(S.VENV_BIN / "chia"), *args], env=chia_env(root), input=input_text, text=True, capture_output=True, timeout=timeout
    )
    if p.returncode != 0:
        raise RuntimeError(f"chia {' '.join(args[:2])} failed: {(p.stderr or p.stdout)[-400:].strip()}")
    return p.stdout


def key_info(root: Path) -> dict[str, Any] | None:
    """Public key material for the first key. Never includes the mnemonic."""
    out = chia(root, "keys", "show", "--json")
    keys = json.loads(out[out.index("{") :])["keys"]
    if not keys:
        return None
    k = keys[0]
    return {n: k.get(n) for n in ("fingerprint", "farmer_pk", "pool_pk", "wallet_address")}


def _ensure_key(root: Path) -> dict[str, Any]:
    info = key_info(root)
    if info is None:
        if S.MNEMONIC_FILE.exists():
            chia(root, "keys", "add", "-f", str(S.MNEMONIC_FILE), "-l", "testnet")
        else:
            # This prints the new mnemonic, so the output is dropped.
            chia(root, "keys", "generate", "-l", "testnet")
        info = key_info(root)
    if info is None:
        raise RuntimeError("no key in the keychain; if you mounted a mnemonic file, check its contents")
    return info


def _patch(root: Path, key: dict[str, Any]) -> None:
    from chia.util.config import lock_and_load_config, save_config

    with lock_and_load_config(root, "config.yaml") as cfg:
        # logging appears in every section (a YAML anchor in the original), so set it everywhere.
        for section in [cfg, *(v for v in cfg.values() if isinstance(v, dict))]:
            if isinstance(section.get("logging"), dict):
                section["logging"].update(log_stdout=False, log_level=S.LOG_LEVEL, log_maxfilesrotation=3)
            if "enable_upnp" in section:
                section["enable_upnp"] = False
        h = cfg["harvester"]
        # Recursive, because plotters make one folder per plot group.
        h["recursive_plot_scan"] = True
        dirs = list(h.get("plot_directories") or [])
        if str(S.PLOTS_DIR) not in dirs:
            dirs.append(str(S.PLOTS_DIR))
        h["plot_directories"] = dirs
        h["plots_refresh_parameter"]["interval_seconds"] = 30
        # The wallet only uses the node in this container. Otherwise, when that node is synced first, the wallet
        # drops every other peer on connect and asks the introducer for more every few seconds, forever.
        cfg["wallet"]["connect_to_unknown_peers"] = False
        for section in ("farmer", "pool"):
            if isinstance(cfg.get(section), dict) and not cfg[section].get("xch_target_address"):
                cfg[section]["xch_target_address"] = key["wallet_address"]
        save_config(root, "config.yaml", cfg)


def prepare(net: NetworkConfig) -> dict[str, Any]:
    """Creates or updates the CHIA_ROOT for a network and returns its public key info."""
    root = net.root
    marker = root / "box.json"
    state: dict[str, Any] = {}
    if marker.exists():
        state = json.loads(marker.read_text())
        if state.get("genesis") != net.genesis:
            # Same name, different chain: the old database is useless but kept aside.
            root.rename(root.with_name(f"{root.name}.old-{int(time.time())}"))
            state = {}
    root.mkdir(parents=True, exist_ok=True)
    S.KEYS_ROOT.mkdir(parents=True, exist_ok=True)
    S.PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    # What the testnet guide does: replace the packaged initial config, then chia init.
    (S.CHIA_SRC / "chia/util/initial-config.yaml").write_text(net.text)
    if state.get("sha") != net.sha:
        (root / "config/config.yaml").unlink(missing_ok=True)
    chia(root, "init")
    key = _ensure_key(root)
    _patch(root, key)
    marker.write_text(json.dumps({"sha": net.sha, "genesis": net.genesis, "source": net.source}))

    current = S.DATA / "current"
    tmp = S.DATA / ".current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(root.relative_to(S.DATA))
    tmp.replace(current)
    return key
