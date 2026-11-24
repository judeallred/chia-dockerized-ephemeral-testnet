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
        self.v2_waiting: str | None = None  # why starter v2 plots aren't being made
        self.v2_failed: str | None = None
        self._v2_logged: str | None = None
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
                Service("monitor", [str(S.PYTHON), str(Path(__file__).with_name("run_monitor.py")), str(mon / "server.py"), "--root", str(root), "--port", str(S.MONITOR_PORT)], 5),
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
                # Emptying /config doesn't pull a running box back to the bundled network.
                net = await asyncio.to_thread(network.newest, self.net is None)
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
    @staticmethod
    def _plots(suffix: str) -> list[Path]:
        return [p for p in S.PLOTS_DIR.rglob(f"*{suffix}") if not p.name.startswith(".")]

    @staticmethod
    def _free_memory_gb() -> float:
        """What a new process here can use: the host's free memory, or what is left of the container's limit."""
        free = psutil.virtual_memory().available
        with contextlib.suppress(OSError, ValueError):
            limit = int(Path("/sys/fs/cgroup/memory.max").read_text())  # "max" when there is no limit
            used = int(Path("/sys/fs/cgroup/memory.current").read_text())
            free = min(free, limit - used)
        return free / 2**30

    async def _cancel_plot_job(self) -> None:
        # Cleared first, which is how _plot tells a cancelled run from a failed one.
        job, self.plot_job = self.plot_job, None
        if job and job["proc"].returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job["proc"].pid, signal.SIGKILL)
            await job["proc"].wait()

    async def _plot(self, kind: str, k: int, root: Path, work: Path, args: list[str]) -> int | None:
        """Runs one plotting command into an empty work folder. None if it was cancelled."""
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        # At low priority, and first to go if memory runs out, so the node and farmer are not what gets killed.
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "15", "sh", "-c", 'echo 1000 > /proc/self/oom_score_adj; exec "$@"', "sh",
            str(S.VENV_BIN / "chia"), "plots", *args,
            env=network.chia_env(root), cwd=str(work), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip
        job = {"kind": kind, "k": k, "started": time.time(), "line": "", "proc": proc}
        self.plot_job = job
        assert proc.stdout is not None
        async for raw in proc.stdout:
            job["line"] = raw.decode("utf-8", "replace").strip()[:160] or job["line"]
        code = await proc.wait()
        if self.plot_job is not job:
            return None
        self.plot_job = None
        if code != 0:
            log(f"{kind} plotting exited with {code}: {job['line']}")
        return code

    @staticmethod
    def _deliver(work: Path, suffix: str) -> list[str]:
        """Moves finished plots to the plots folder under a hidden name first, so the harvester never sees half a file."""
        names = []
        for src in work.glob(f"*{suffix}"):
            partial = S.PLOTS_DIR / f".{src.name}.partial"
            try:
                shutil.copyfile(src, partial)
                partial.replace(S.PLOTS_DIR / src.name)
            finally:
                partial.unlink(missing_ok=True)
            names.append(src.name)
        return names

    def _v2_blocker(self) -> str | None:
        """Why a v2 plot can't be made now, or None."""
        if self.v2_failed:
            return self.v2_failed
        used = sum(p.stat().st_size for p in self._plots(".gplot"))
        if used + 1.1e9 > S.V2_LIMIT_GB * 1e9:
            return f"the {S.V2_LIMIT_GB:g} GB v2 limit is reached"
        if min(shutil.disk_usage(S.DATA).free, shutil.disk_usage(S.PLOTS_DIR).free) < 10e9:
            return "less than 10 GB of disk is free"
        free = self._free_memory_gb()
        if free < S.V2_MIN_FREE_GB:
            return f"a k{S.V2_K} plot needs {S.V2_MIN_FREE_GB:.2g} GB of free memory and the container has {free:.1f} GB"
        return None

    async def _plot_once(self) -> None:
        assert self.net is not None and self.key is not None
        root, work, keys = self.net.root, S.DATA / "tmp", ["-f", self.key["farmer_pk"], "-p", self.key["pool_pk"]]
        retired = self.height is not None and self.v1_cutoff is not None and self.height >= self.v1_cutoff
        have_v1, have_v2 = len(self._plots(".plot")), len(self._plots(".gplot"))
        if have_v1 < S.AUTO_V1_PLOTS and not retired and shutil.disk_usage(S.DATA).free > 10e9:
            log(f"making starter v1 k{S.V1_K} plot {have_v1 + 1} of {S.AUTO_V1_PLOTS}")
            code = await self._plot("v1", S.V1_K, root, work, [
                "create", "-k", str(S.V1_K), "--override-k", "-n", "1", "-b", "1024", "-r", "2",
                *keys, "-t", str(work), "-d", str(S.PLOTS_DIR), "-x",
            ])  # fmt: skip
            if code:
                await asyncio.sleep(60)
            return
        self.v2_waiting = None
        if have_v2 >= S.AUTO_V2_PLOTS:
            return
        blocker = self._v2_blocker()
        if blocker:
            if blocker.split(" and ")[0] != self._v2_logged:
                self._v2_logged = blocker.split(" and ")[0]
                log(f"not making v2 plots: {blocker}")
            self.v2_waiting = blocker
            return
        log(f"making starter v2 k{S.V2_K} plot {have_v2 + 1} of {S.AUTO_V2_PLOTS}")
        code = await self._plot("v2", S.V2_K, root, work, [
            "create2", "-k", str(S.V2_K), "-s", str(S.V2_STRENGTH), "-n", "1", *keys, "-d", str(work), "-x",
        ])  # fmt: skip
        if code == 0:
            names = await asyncio.to_thread(self._deliver, work, ".gplot")
            log(f"finished {', '.join(names) or 'no plot'}")
            if not names:
                code = -1
        if code:
            # Most likely out of memory. Trying again on a loop would only make things worse.
            killed = " (killed, probably out of memory)" if code in (-9, 137) else ""
            self.v2_failed = f"the last try failed{killed}; restart the box to try again"
        shutil.rmtree(work, ignore_errors=True)

    async def plot_loop(self) -> None:
        """Makes a few plots, one at a time, so a fresh box has something to farm."""
        while not self.stopping:
            await asyncio.sleep(10)
            if self.net and self.key and self.synced:
                try:
                    await self._plot_once()
                except Exception as e:
                    self.plot_job = None
                    log(f"plotting: {e!r}")
                    await asyncio.sleep(60)

    async def shutdown(self) -> None:
        self.stopping = True
        async with self._switch:
            await self._cancel_plot_job()
            await self.stop_all()
