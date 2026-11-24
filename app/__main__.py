from __future__ import annotations

import asyncio
import signal

from aiohttp import web

from app import settings as S
from app.supervisor import Supervisor, log
from app.web import Gateway


async def main() -> None:
    S.DATA.mkdir(parents=True, exist_ok=True)
    sup = Supervisor()
    gw = Gateway(sup)
    runner = web.AppRunner(gw.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", S.PORT).start()
    log(f"dashboard on port {S.PORT}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(c) for c in (gw.run(), sup.watch_config(), sup.sample_loop(), sup.plot_loop(), sup.solver_loop(gw.call))]
    stopper = asyncio.create_task(stop.wait())
    # If a background loop dies, stop and let Docker restart the container rather than limp on.
    done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    failed = [t for t in done if t is not stopper]
    for t in failed:
        log(f"background task ended: {t.exception()!r}")
    log("stopping")
    await sup.shutdown()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()
    if failed:
        raise SystemExit(1)


asyncio.run(main())
