"""Runs the chia services, restarts them when they die, and samples their resource use."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from app import network
from app import settings as S
from app.network import ConfigError, NetworkConfig


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} box: {msg}", flush=True)


@dataclass
class Service:
    name: str
    argv: list[str]
    grace: float = 20
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    started: float | None = None
    restarts: int = 0
    last_exit: int | None = None
    out: deque[str] = field(default_factory=lambda: deque(maxlen=60))
    cpu: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    rss: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    ps: dict[int, psutil.Process] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def sample(self) -> None:
        """CPU and memory for the process and its children (chia starts worker processes)."""
        cpu = rss = 0.0
        if self.running and self.proc is not None:
            try:
                parent = self.ps.get(self.proc.pid) or psutil.Process(self.proc.pid)
                live = {}
                for p in [parent, *parent.children(recursive=True)]:
                    q = self.ps.get(p.pid, p)
                    live[p.pid] = q
                    with contextlib.suppress(psutil.Error):
                        cpu += q.cpu_percent(None)
                        rss += q.memory_info().rss
                self.ps = live
            except psutil.Error:
                self.ps = {}
        self.cpu.append(round(cpu, 1))
        self.rss.append(rss)

    def status(self) -> dict[str, Any]:
        state = "down"
        if self.running:
            state = "starting" if time.time() - (self.started or 0) < 8 else "up"
        return {
            "name": self.name,
            "state": state,
            "pid": self.proc.pid if self.running and self.proc else None,
            "uptime": time.time() - self.started if self.running and self.started else 0,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "cpu": list(self.cpu),
            "rss": list(self.rss),
            "out": list(self.out)[-8:],
        }


class Supervisor:
    def __init__(self) -> None:
        self.net: NetworkConfig | None = None
        self.key: dict[str, Any] | None = None
        self.phase = "waiting for a config"
        self.error: str | None = None
        self.bad_sha: str | None = None
        self.services: dict[str, Service] = {}
        self.stopping = False
        self.started = time.time()
        self.plot_job: dict[str, Any] | None = None
        self.synced = False  # kept up to date by the gateway's collector
        self.height: int | None = None
        self.v1_cutoff: int | None = None
        self._switch = asyncio.Lock()

    @property
    def root(self) -> Path | None:
        return self.net.root if self.net else None

    # ---- services -----------------------------------------------------
    def _build_services(self, root: Path) -> None:
        b = S.VENV_BIN
        mon = root / "monitor"
        mon.mkdir(exist_ok=True)
        # The monitor keeps its saved stats beside server.py, so run a copy from the data volume.
        for f in ("server.py", "index.html"):
            shutil.copyfile(S.MONITOR_SRC / f, mon / f)
        self.services = {
            s.name: s
            for s in [
                Service("daemon", [str(b / "chia_daemon")], 15),
                Service("full_node", [str(b / "chia_full_node")], 40),
                Service("solver", [str(b / "chia_solver")], 15),
                Service("farmer", [str(b / "chia_farmer")]),
                Service("harvester", [str(b / "chia_harvester")]),
                Service("wallet", [str(b / "chia_wallet")], 25),
                Service("monitor", [str(S.PYTHON), str(mon / "server.py"), "--root", str(root), "--port", str(S.MONITOR_PORT)], 5),
            ]
        }

    async def _run_service(self, svc: Service, root: Path, delay: float) -> None:
        await asyncio.sleep(delay)
        backoff = 2.0
        while True:
            svc.started = time.time()
            svc.proc = await asyncio.create_subprocess_exec(
                *svc.argv,
                env=network.chia_env(root),
                cwd=str(root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            assert svc.proc.stdout is not None
            reader = asyncio.create_task(self._read_output(svc, svc.proc.stdout))
            try:
                code = await svc.proc.wait()
            except asyncio.CancelledError:
                reader.cancel()
                raise
            # Children may still hold the pipe open; don't wait for them.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(reader, 1)
            svc.last_exit = code
            svc.restarts += 1
            backoff = 2.0 if time.time() - svc.started > 60 else min(backoff * 2, 30)
            log(f"{svc.name} exited with {code}, restarting in {backoff:.0f}s" + (f": {svc.out[-1]}" if svc.out else ""))
            await asyncio.sleep(backoff)

    @staticmethod
    async def _read_output(svc: Service, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stream.readline()
            except ValueError:  # a line longer than the buffer
                continue
            if not line:
                return
            svc.out.append(line.decode("utf-8", "replace").rstrip()[:400])

    async def _start_all(self, root: Path) -> None:
        self._build_services(root)
        # The daemon hosts the keychain for the others, so it gets a head start.
        delays = {"daemon": 0, "full_node": 3, "solver": 3, "farmer": 5, "harvester": 6, "wallet": 5, "monitor": 8}
        for svc in self.services.values():
            svc.task = asyncio.create_task(self._run_service(svc, root, delays[svc.name]))

    async def _stop_service(self, svc: Service) -> None:
        if svc.task:
            svc.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await svc.task
        if svc.running and svc.proc:
            with contextlib.suppress(ProcessLookupError):
                svc.proc.terminate()
            try:
                await asyncio.wait_for(svc.proc.wait(), svc.grace)
            except asyncio.TimeoutError:
                log(f"{svc.name} did not stop in {svc.grace:.0f}s, killing it")
            # Clean up worker processes either way.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(svc.proc.pid, signal.SIGKILL)
            await svc.proc.wait()

    async def stop_all(self) -> None:
        daemon = self.services.get("daemon")
        others = [s for s in self.services.values() if s is not daemon]
        await asyncio.gather(*(self._stop_service(s) for s in others))
        if daemon:
            await self._stop_service(daemon)

    async def restart(self, name: str) -> None:
        async with self._switch:
            svc = self.services[name]
            root = self.root
            assert root is not None
            await self._stop_service(svc)
            svc.task = asyncio.create_task(self._run_service(svc, root, 0))

    # ---- network ------------------------------------------------------
    async def switch(self, net: NetworkConfig) -> None:
        async with self._switch:
            log(f"switching to {net.name} (from {net.source})")
            self.phase = f"setting up {net.name}"
            await self._cancel_plot_job()
            await self.stop_all()
            self.services = {}
            self.synced, self.height, self.v1_cutoff = False, None, None
            try:
                self.key = await asyncio.to_thread(network.prepare, net)
            except Exception as e:
                self.phase, self.error, self.net = "setup failed", f"{net.name}: {e}", None
                self.bad_sha = net.sha  # don't retry until the file changes
                log(f"setup failed: {e}")
                return
            self.net, self.error, self.bad_sha = net, None, None
            await self._start_all(net.root)
            self.phase = "running"
            log(f"running {net.name}, key fingerprint {self.key['fingerprint']}")

    async def watch_config(self) -> None:
        while not self.stopping:
            try:
                net = await asyncio.to_thread(network.newest)
                if net and net.sha != self.bad_sha and (self.net is None or net.sha != self.net.sha):
                    await self.switch(net)
                elif net and self.net and net.sha == self.net.sha:
                    self.error = None
            except ConfigError as e:
                # A bad file never takes down a running network.
                if self.error != str(e):
                    log(f"ignoring config: {e}")
                self.error = str(e)
            except Exception as e:
                self.error = f"config watcher: {e!r}"
                log(self.error)
            await asyncio.sleep(3)

    async def solver_loop(self, rpc: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any] | None]]) -> None:
        """Keeps the farmer connected to the solver. The farmer doesn't connect by itself on this branch."""
        while not self.stopping:
            await asyncio.sleep(5)
            farmer, solver = self.services.get("farmer"), self.services.get("solver")
            if not (farmer and solver and farmer.running and solver.running):
                continue
            conns = await rpc("farmer", "get_connections", {})
            if conns is None or any(c["type"] == 8 for c in conns.get("connections", [])):
                continue
            r = await rpc("farmer", "connect_to_solver", {"host": "localhost", "port": 8666})
            log("farmer connected to the solver" if r else "farmer could not connect to the solver, retrying")

    async def sample_loop(self) -> None:
        while not self.stopping:
            for svc in self.services.values():
                svc.sample()
            await asyncio.sleep(5)

    # ---- starter plots ------------------------------------------------
    def _v1_count(self) -> int:
        return sum(1 for _ in S.PLOTS_DIR.rglob("*.plot"))

    async def _cancel_plot_job(self) -> None:
        job = self.plot_job
        if job and job.get("proc") and job["proc"].returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job["proc"].pid, signal.SIGKILL)
            await job["proc"].wait()
        self.plot_job = None

    async def plot_loop(self) -> None:
        """Makes a few small v1 plots, one at a time, so a fresh box has something to farm."""
        while not self.stopping:
            await asyncio.sleep(10)
            retired = self.height is not None and self.v1_cutoff is not None and self.height >= self.v1_cutoff
            if not (self.net and self.key and self.synced) or retired or self._v1_count() >= S.AUTO_V1_PLOTS:
                continue
            root, tmp, dest = self.net.root, S.DATA / "tmp", S.PLOTS_DIR
            if shutil.disk_usage(S.DATA).free < 10e9:
                continue
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            log(f"making a starter v1 k{S.V1_K} plot ({self._v1_count() + 1} of {S.AUTO_V1_PLOTS})")
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "15", str(S.VENV_BIN / "chia"), "plots", "create",
                "-k", str(S.V1_K), "--override-k", "-n", "1", "-b", "1024", "-r", "2",
                "-f", self.key["farmer_pk"], "-p", self.key["pool_pk"], "-t", str(tmp), "-d", str(dest), "-x",
                env=network.chia_env(root), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )  # fmt: skip
            self.plot_job = {"kind": "v1", "k": S.V1_K, "started": time.time(), "line": "", "proc": proc}
            assert proc.stdout is not None
            async for raw in proc.stdout:
                if self.plot_job:
                    self.plot_job["line"] = raw.decode("utf-8", "replace").strip()[:160] or self.plot_job["line"]
            code = await proc.wait()
            if code != 0 and self.plot_job:
                log(f"plotting exited with {code}: {self.plot_job['line']}")
                await asyncio.sleep(60)
            self.plot_job = None

    async def shutdown(self) -> None:
        self.stopping = True
        async with self._switch:
            await self._cancel_plot_job()
            await self.stop_all()
