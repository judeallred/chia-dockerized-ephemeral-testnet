"""The one port: dashboard, plotter, monitor, JSON stats, Prometheus metrics, and the RPC gateway."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import shutil
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from aiohttp import ClientSession, ClientTimeout, web

from app import network
from app import settings as S
from app.network import ConfigError, NetworkConfig
from app.supervisor import Supervisor, log

RPC_SERVICES = ("full_node", "farmer", "harvester", "wallet", "solver")
ENDPOINT_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PLOT_NAME_RE = re.compile(r"^plot-[A-Za-z0-9._-]{1,180}\.(plot|gplot)$")
PLOT_SUFFIXES = (".plot", ".gplot")
# Reveals or destroys keys and plots, or stops a service. UNSAFE_RPC=1 allows them.
BLOCKED = {
    "get_private_key", "delete_key", "delete_all_keys", "generate_mnemonic", "add_key",
    "stop_node", "delete_plot", "stop_service",
}  # fmt: skip
ISOLATION = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# The monitor page loads its font and confetti from CDNs; only those, and only that confetti version.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'"
)
LEVEL_RE = re.compile(r"^\S+ \S+ (\S+) .*?: (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")


def _hostname(host: str) -> str:
    return (urlsplit(f"//{host}").hostname or "").lower()


def _allowed_host(host: str) -> bool:
    name = _hostname(host)
    return name in {h.strip("[]") for h in S.ALLOWED_HOSTS} or "*" in S.ALLOWED_HOSTS


def _origin_ok(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        # Browsers without an Origin header still label cross-site requests.
        return request.headers.get("Sec-Fetch-Site", "none") in ("none", "same-origin")
    return origin in S.CORS_ORIGINS or urlsplit(origin).netloc.lower() == request.host.lower()


@web.middleware
async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
    if not _allowed_host(request.host):
        return web.json_response({"success": False, "error": f"host {request.host!r} is not in ALLOWED_HOSTS"}, status=403)
    origin = request.headers.get("Origin")
    cors = {}
    if origin in S.CORS_ORIGINS:
        cors = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, PUT",
            "Access-Control-Allow-Headers": "content-type, authorization",
            "Vary": "Origin",
        }
    if request.method == "OPTIONS" and cors:
        return web.Response(status=204, headers=cors)
    privileged = request.method not in ("GET", "HEAD") or request.path.startswith("/rpc/")
    if privileged:
        if not _origin_ok(request):
            return web.json_response({"success": False, "error": "cross-origin request refused; see CORS_ORIGINS"}, status=403)
        if S.API_TOKEN and not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(), f"Bearer {S.API_TOKEN}".encode()
        ):
            return web.json_response({"success": False, "error": "missing or wrong API token"}, status=401, headers=cors)
    resp: web.StreamResponse = await handler(request)
    if not resp.prepared:
        resp.headers.update(cors)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


class Gateway:
    def __init__(self, sup: Supervisor) -> None:
        self.sup = sup
        self.http: ClientSession | None = None
        self.state: dict[str, Any] = {"ready": False}
        self.state_raw = b'{"ready": false}'
        self.state_at = 0.0
        self.plots: list[dict[str, Any]] = []
        self.disk: dict[str, Any] = {}
        self._rpc_cache: dict[str, tuple[int, ssl.SSLContext]] = {}
        self._rpc_net: NetworkConfig | None = None
        self.commit, self.monitor_commit, self.plotter_commit = (
            (p / "COMMIT").read_text().strip() if (p / "COMMIT").exists() else "unknown"
            for p in (S.CHIA_SRC, S.MONITOR_SRC, S.PLOTTER_DIST)
        )

    # ---- background collection ---------------------------------------
    async def run(self) -> None:
        self.http = ClientSession(timeout=ClientTimeout(total=10))
        try:
            await asyncio.gather(self._poll_monitor(), self._poll_disk())
        finally:
            await self.http.close()

    async def _poll_monitor(self) -> None:
        assert self.http is not None
        while True:
            net = self.sup.net
            try:
                async with self.http.get(f"http://127.0.0.1:{S.MONITOR_PORT}/api/state") as r:
                    raw = await r.read()
                state = json.loads(raw)
                # Ignore an answer from the previous network's monitor.
                if net and state.get("ready") and state.get("network") == net.name:
                    self.state, self.state_raw, self.state_at = state, raw, time.time()
                    node = state.get("node") or {}
                    self.sup.synced = bool(node.get("synced"))
                    self.sup.height = node.get("height")
                    self.sup.v1_cutoff = (state.get("fork") or {}).get("v1_cutoff")
            except Exception:
                pass  # the monitor starts after the services and may be restarting
            if time.time() - self.state_at > 30 and self.state.get("ready"):
                self.state, self.state_raw = {"ready": False}, b'{"ready": false}'
                self.sup.synced = False
            await asyncio.sleep(2)

    async def _poll_disk(self) -> None:
        while True:
            try:
                self.plots, self.disk = await asyncio.to_thread(self._scan_disk)
            except Exception as e:
                log(f"disk scan: {e!r}")
            await asyncio.sleep(8)

    def _scan_disk(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        plots = []
        for base, _, files in os.walk(S.PLOTS_DIR):
            for f in files:
                if f.endswith(PLOT_SUFFIXES):
                    p = Path(base) / f
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    plots.append({
                        "name": f, "path": str(p.relative_to(S.PLOTS_DIR)), "size": st.st_size,
                        "mtime": st.st_mtime, "kind": "v2" if f.endswith(".gplot") else "v1",
                    })  # fmt: skip
        disk: dict[str, Any] = {"db": {}, "networks": []}
        for label, path in (("data", S.DATA), ("plots", S.PLOTS_DIR)):
            try:
                u = shutil.disk_usage(path)
                disk[label] = {"total": u.total, "free": u.free}
            except OSError:
                pass
        if root := self.sup.root:
            for label, sub in (("node", "db"), ("wallet", "wallet/db"), ("logs", "log")):
                disk["db"][label] = sum(x.stat().st_size for x in (root / sub).rglob("*") if x.is_file())
        nets = S.DATA / "networks"
        if nets.is_dir():
            disk["networks"] = sorted(p.name for p in nets.iterdir() if p.is_dir())
        return sorted(plots, key=lambda p: -p["mtime"]), disk

    # ---- pages --------------------------------------------------------
    async def index(self, _: web.Request) -> web.StreamResponse:
        """chia-testnet-monitor's own page, unmodified, plus the box's stylesheet and script."""
        html = (S.MONITOR_SRC / "index.html").read_text()
        tags = '<link rel="stylesheet" href="/static/box.css"><script src="/static/box.js"></script>'
        html = html.replace("</body>", f"{tags}</body>") if "</body>" in html else html + tags
        return web.Response(text=html, content_type="text/html", headers={"Content-Security-Policy": CSP})

    async def plotter(self, _: web.Request) -> web.StreamResponse:
        """The WASM plotter's page, plus a script that fills in this box's keys."""
        html = (S.PLOTTER_DIST / "index.html").read_text()
        tag = '<script src="/static/plotter-bridge.js" defer></script>'
        html = html.replace("</body>", f"{tag}</body>") if "</body>" in html else html + tag
        return web.Response(text=html, content_type="text/html", headers=ISOLATION)

    async def plotter_asset(self, request: web.Request) -> web.StreamResponse:
        top = request.path.split("/")[1]  # "_astro" or "wasm"
        base = (S.PLOTTER_DIST / top).resolve()
        path = (base / request.match_info["path"]).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={**ISOLATION, "Cache-Control": "no-cache"})

    async def static(self, request: web.Request) -> web.StreamResponse:
        base = S.STATIC.resolve()
        path = (base / request.match_info["path"]).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise web.HTTPNotFound
        return web.FileResponse(path, headers=ISOLATION)

    # ---- JSON ---------------------------------------------------------
    async def api_state(self, _: web.Request) -> web.Response:
        return web.Response(body=self.state_raw, content_type="application/json")

    def box(self) -> dict[str, Any]:
        sup = self.sup
        job = {k: v for k, v in sup.plot_job.items() if k != "proc"} if sup.plot_job else None
        return {
            "now": time.time(),
            "started": sup.started,
            "phase": sup.phase,
            "error": sup.error,
            "network": sup.net.name if sup.net else None,
            "genesis": sup.net.genesis if sup.net else None,
            "config_source": sup.net.source if sup.net else None,
            "commit": self.commit,
            "monitor_commit": self.monitor_commit,
            "key": sup.key,
            "services": [s.status() for s in sup.services.values()],
            "plots": self.plots,
            "plot_job": job,
            "auto_v1_plots": S.AUTO_V1_PLOTS,
            "v2_limit_gb": S.V2_LIMIT_GB,
            "disk": self.disk,
            "token_required": bool(S.API_TOKEN),
            "plotter_commit": self.plotter_commit,
        }

    async def api_box(self, _: web.Request) -> web.Response:
        return web.json_response(self.box())

    async def api_logs(self, request: web.Request) -> web.Response:
        root = self.sup.root
        lines: list[dict[str, str]] = []
        if root:
            level = request.query.get("level", "INFO").upper()
            service = request.query.get("service", "")
            lines = await asyncio.to_thread(self._tail, root / "log/debug.log", level, service)
        return web.json_response({"lines": lines})

    @staticmethod
    def _tail(path: Path, level: str, service: str) -> list[dict[str, str]]:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        floor = order.index(level) if level in order else 1
        try:
            with path.open("rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - 400_000))
                raw = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        out = []
        for line in raw:
            m = LEVEL_RE.match(line)
            if m and order.index(m[2]) >= floor and (not service or m[1] == service):
                out.append({"service": m[1], "level": m[2], "text": line[:600]})
        return out[-150:]

    async def healthz(self, _: web.Request) -> web.Response:
        svcs = {s.name: s.running for s in self.sup.services.values()}
        ok = bool(svcs) and all(svcs.values())
        return web.json_response({"ok": ok, "phase": self.sup.phase, "services": svcs}, status=200 if ok else 503)

    async def metrics(self, _: web.Request) -> web.Response:
        out: list[str] = []

        def gauge(name: str, value: Any, **labels: str) -> None:
            if value is None:
                return
            ls = ",".join(f'{k}="{v}"' for k, v in labels.items())
            out.append(f"chia_testnet_{name}{'{' + ls + '}' if ls else ''} {float(value)}")

        st = self.state
        for s in self.sup.services.values():
            gauge("service_up", int(s.running), service=s.name)
            gauge("service_restarts", s.restarts, service=s.name)
            gauge("service_cpu_percent", s.cpu[-1] if s.cpu else None, service=s.name)
            gauge("service_rss_bytes", s.rss[-1] if s.rss else None, service=s.name)
        node, farm, wallet = st.get("node") or {}, st.get("farm") or {}, st.get("wallet") or {}
        for k, n in (("height", "height"), ("space", "netspace_bytes"), ("difficulty", "difficulty"), ("mempool", "mempool_size")):
            gauge(n, node.get(k))
        gauge("synced", int(bool(node.get("synced"))) if node else None)
        gauge("peers", len(st.get("peers") or []) if node else None)
        gauge("farm_plots", farm.get("plots"))
        gauge("farm_bytes", farm.get("size"))
        gauge("farm_share", farm.get("share"))
        gauge("wallet_confirmed_mojos", wallet.get("confirmed"))
        for kind, n in ((st.get("chain") or {}).get("ours_by_kind") or {}).items():
            gauge("blocks_won", n, kind=kind)
        return web.Response(text="\n".join(out) + "\n", content_type="text/plain")

    # ---- RPC ----------------------------------------------------------
    def _rpc_target(self, service: str) -> tuple[int, ssl.SSLContext]:
        net = self.sup.net
        if net is None:
            raise web.HTTPServiceUnavailable
        # By network object, not path: a relaunched testnet can reuse its name with new certificates.
        if self._rpc_net is not net:
            self._rpc_cache, self._rpc_net = {}, net
        root = net.root
        if service not in self._rpc_cache:
            cfg = yaml.safe_load((root / "config/config.yaml").read_text())
            ctx = ssl.create_default_context(cafile=str(root / "config/ssl/ca/private_ca.crt"))
            ctx.check_hostname = False
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            d = root / "config/ssl" / service
            ctx.load_cert_chain(d / f"private_{service}.crt", d / f"private_{service}.key")
            self._rpc_cache[service] = (int(cfg[service]["rpc_port"]), ctx)
        return self._rpc_cache[service]

    async def call(self, service: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """An internal RPC call; None when it fails or the service is down."""
        try:
            port, ctx = self._rpc_target(service)
            assert self.http is not None
            async with self.http.post(f"https://127.0.0.1:{port}/{endpoint}", json=payload, ssl=ctx) as r:
                body = await r.json()
        except Exception:
            return None
        return body if body.get("success") else None

    async def rpc(self, request: web.Request) -> web.Response:
        service, endpoint = request.match_info["service"], request.match_info["endpoint"]

        def fail(status: int, msg: str) -> web.Response:
            return web.json_response({"success": False, "error": msg}, status=status)

        if service not in RPC_SERVICES:
            return fail(404, f"unknown service; use one of {', '.join(RPC_SERVICES)}")
        if not ENDPOINT_RE.match(endpoint):
            return fail(404, "bad endpoint name")
        if endpoint in BLOCKED and not S.UNSAFE_RPC:
            return fail(403, f"{endpoint} is blocked on this port; set UNSAFE_RPC=1 to allow it")
        if request.method == "GET":
            # GET is for reading, e.g. from a browser address bar or plain curl.
            if not (endpoint.startswith("get_") or endpoint == "healthz"):
                return fail(405, "use POST for this endpoint")
            payload: Any = dict(request.query)
        else:
            raw = await request.read()
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except ValueError:
                return fail(400, "body is not JSON")
            if not isinstance(payload, dict):
                return fail(400, "body must be a JSON object")
        if self.sup.root is None:
            return fail(503, f"no network running ({self.sup.phase})")
        port, ctx = self._rpc_target(service)
        assert self.http is not None
        try:
            async with self.http.post(
                f"https://127.0.0.1:{port}/{endpoint}", json=payload, ssl=ctx, timeout=ClientTimeout(total=120)
            ) as r:
                body = await r.read()
                return web.Response(body=body, status=r.status, content_type="application/json")
        except asyncio.TimeoutError:
            return fail(504, f"{service} took too long")
        except OSError as e:
            return fail(502, f"{service} is not answering: {e}")

    # ---- uploads and controls ----------------------------------------
    async def upload_config(self, request: web.Request) -> web.Response:
        raw = await request.content.read(network.MAX_CONFIG_BYTES + 1)
        while len(raw) <= network.MAX_CONFIG_BYTES and (more := await request.content.read(65536)):
            raw += more
        try:
            net = await asyncio.to_thread(network.store_upload, raw, "dashboard upload")
        except ConfigError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        except OSError as e:
            return web.json_response({"success": False, "error": f"can't write to /config: {e}"}, status=500)
        return web.json_response({"success": True, "network": net.name})

    async def upload_plot(self, request: web.Request) -> web.Response:
        def fail(status: int, msg: str) -> web.Response:
            return web.json_response({"success": False, "error": msg}, status=status)

        name = request.match_info["name"]
        if not PLOT_NAME_RE.match(name):
            return fail(400, "not a plot file name (plot-….plot or plot-….gplot)")
        size = request.content_length
        if not size:
            return fail(411, "Content-Length needed")
        v2_bytes = sum(p["size"] for p in self.plots if p["kind"] == "v2")
        if name.endswith(".gplot") and v2_bytes + size > S.V2_LIMIT_GB * 1e9:
            return fail(413, f"that would go over the {S.V2_LIMIT_GB:g} GB v2 limit for this testnet")
        if shutil.disk_usage(S.PLOTS_DIR).free < size + 1e9:
            return fail(507, "not enough disk space")
        target = S.PLOTS_DIR / name
        if target.exists():
            return fail(409, "that plot is already here")
        partial = S.PLOTS_DIR / f".{name}.partial"
        got = 0
        try:
            with partial.open("wb") as f:
                async for chunk in request.content.iter_chunked(1 << 20):
                    got += len(chunk)
                    if got > size:
                        break
                    await asyncio.to_thread(f.write, chunk)
            if got != size:
                return fail(400, "upload was cut short")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        log(f"received {name} ({size / 1e9:.2f} GB)")
        return web.json_response({"success": True}, status=201)

    async def restart(self, request: web.Request) -> web.Response:
        name = request.match_info["service"]
        if name not in self.sup.services:
            return web.json_response({"success": False, "error": "unknown service"}, status=404)
        await self.sup.restart(name)
        return web.json_response({"success": True})

    def app(self) -> web.Application:
        app = web.Application(middlewares=[guard], client_max_size=4_000_000)
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/plotter", self.plotter)
        r.add_get("/plotter/", self.plotter)
        r.add_get(r"/_astro/{path:.+}", self.plotter_asset)
        r.add_get(r"/wasm/{path:.+}", self.plotter_asset)
        r.add_get(r"/static/{path:.+}", self.static)
        r.add_get("/healthz", self.healthz)
        r.add_get("/metrics", self.metrics)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/box", self.api_box)
        r.add_get("/api/logs", self.api_logs)
        r.add_put("/api/config", self.upload_config)
        r.add_put("/api/plots/{name}", self.upload_plot)
        r.add_post("/api/services/{service}/restart", self.restart)
        r.add_get("/rpc/{service}/{endpoint}", self.rpc)
        r.add_post("/rpc/{service}/{endpoint}", self.rpc)
        r.add_route("OPTIONS", "/{path:.*}", self._preflight)
        return app

    @staticmethod
    async def _preflight(_: web.Request) -> web.Response:
        return web.Response(status=403)
