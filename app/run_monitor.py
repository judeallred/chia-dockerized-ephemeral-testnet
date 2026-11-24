"""Runs the unmodified chia-testnet-monitor, translating one RPC answer it doesn't know yet.

Newer harvesters describe v2 plots in get_plots with "strength" and "group_plot_id" and leave "size" and
"plot_id" empty. The monitor expects size = 0x80 | strength and a plot_id, and fails on every refresh without
them. The v2 plot filter uses the group ID, so the two shapes carry the same information. Once the monitor
no longer reads the old shape, this does nothing.

Usage: python run_monitor.py <server.py> [its arguments]
"""

from __future__ import annotations

import io
import json
import runpy
import sys
import urllib.request
from pathlib import Path
from typing import Any

OLD_SHAPE = '["size"] & 0x80'


def adapt(body: dict[str, Any]) -> dict[str, Any]:
    for p in body.get("plots") or []:
        if p.get("size") is None and p.get("strength") is not None:
            p["size"] = 0x80 | int(p["strength"])
            p["plot_id"] = p.get("plot_id") or p.get("group_plot_id")
    return body


def main() -> None:
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    if OLD_SHAPE in Path(script).read_text():
        real = urllib.request.urlopen

        def urlopen(req: Any, *args: Any, **kwargs: Any) -> Any:
            resp = real(req, *args, **kwargs)
            url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
            if not url.endswith("/get_plots"):
                return resp
            with resp:
                return io.BytesIO(json.dumps(adapt(json.load(resp))).encode())

        urllib.request.urlopen = urlopen
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
