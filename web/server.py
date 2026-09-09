"""The delivery record, served.

One route that renders the last real run, with the run record embedded in the HTML so
the page has evidence on first paint and never shows a loading state or an empty form.
One route that starts a new run against live Grafana and Gemini, and one that reports
its progress, because a judge should be able to make the thing execute rather than
read about it having executed.

There is no fixture. If no run has happened, the page says so in the words of this
product and prints the command that produces one. It does not draw an empty chart.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RECORD = HERE / "run.json"

sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

_LOCK = threading.Lock()
_STATE: dict = {"running": False, "started_at": None, "finished_at": None, "error": None, "stage": ""}


def _record() -> dict | None:
    if not RECORD.exists():
        return None
    try:
        return json.loads(RECORD.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fleet_ready() -> dict:
    """How many masters are on disk, and which ones are not."""
    try:
        from interlock import catalog
    except ImportError:
        return {"downloaded": 0, "total": 0, "missing": [], "complete": False}
    have = catalog.downloaded()
    everything = [t.title_id for t in catalog.FLEET]
    return {
        "downloaded": len(have),
        "total": len(everything),
        "missing": [t for t in everything if t not in have],
        "complete": len(have) == len(everything),
    }


def _run_in_thread(title_ids: list[str] | None) -> None:
    import asyncio

    from interlock.agent.pipeline import run_fleet

    with _LOCK:
        _STATE.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "error": None,
                "stage": "measuring with ffmpeg and probing the Grafana stack",
            }
        )
    try:
        asyncio.run(run_fleet(title_ids))
        with _LOCK:
            _STATE.update({"stage": "done", "error": None})
    except Exception as exc:  # noqa: BLE001 - the message is the product's output here
        with _LOCK:
            _STATE.update(
                {
                    "stage": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        traceback.print_exc()
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["finished_at"] = datetime.now(timezone.utc).isoformat()


class Handler(BaseHTTPRequestHandler):
    server_version = "interlock"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._page()
        if path == "/api/run":
            record = _record()
            return self._json(200, record or {"empty": True})
        if path == "/api/status":
            with _LOCK:
                state = dict(_STATE)
            record = _record()
            state["has_record"] = record is not None
            state["record_generated_at"] = (record or {}).get("generated_at")
            # Which masters are actually on disk. Without this the run button can only
            # fail after the fact: the container fetches 190 MB from archive.org on
            # first boot, and a request that arrives during the fetch gets a refusal
            # naming a title, which reads as a broken product rather than a busy one.
            state["fleet_ready"] = _fleet_ready()
            return self._json(200, state)
        # Both spellings on purpose. Cloud Run's front end answers /healthz itself with
        # a Google 404 and never forwards it to the container, which was found by
        # curling the deployed URL and getting Google's error page for a route the
        # server demonstrably serves. /api/health reaches the container.
        if path in ("/api/health", "/healthz"):
            return self._json(200, {"ok": True, "has_record": RECORD.exists()})
        self._send(404, b"no such route", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/run":
            return self._send(404, b"no such route", "text/plain; charset=utf-8")
        with _LOCK:
            if _STATE["running"]:
                return self._json(409, {"error": "a run is already in flight", **_STATE})
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        title_ids = payload.get("title_ids") or None
        threading.Thread(target=_run_in_thread, args=(title_ids,), daemon=True).start()
        time.sleep(0.2)
        with _LOCK:
            return self._json(202, dict(_STATE))

    def _page(self) -> None:
        template = (HERE / "index.html").read_text(encoding="utf-8")
        record = _record()
        embedded = json.dumps(record if record is not None else {"empty": True})
        html = template.replace("/*__RUN_JSON__*/null", embedded)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"interlock listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
