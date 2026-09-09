"""The one place Interlock talks to Grafana: the official mcp-grafana server.

Everything Interlock does to Grafana Cloud goes through `grafana/mcp-grafana` held as
a child process over stdio. There is no REST fallback and no HTTP client hiding
behind this module. If the MCP server will not start, or the stack refuses a call,
Interlock fails and says which tool call failed, because a delivery agent that
silently stops filing breaches is worse than one that stops.

Two things this module deliberately does not do.

It does not degrade to an offline mode. Grafana is where the breach record lives, so
without it there is no product, and a fallback that produced the same screen from
local JSON would make the partner integration decorative. `GrafanaRequired` is the
correct outcome of a missing token.

It does not guess at a package name. A sibling project resolved the server with
`uvx --from mcp-grafana mcp-grafana`, which looks like a reasonable last resort and
is in fact a dead branch: mcp-grafana is a Go binary and there is no such PyPI
distribution, so that fallback can only ever raise on the far side of a 90 second
MCP timeout. The resolver below checks real paths and then tells you the one command
that installs it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "grafana_mcp_calls.jsonl"

INSTALL_HINT = (
    "install the official server with: "
    "go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest "
    "(it lands in $(go env GOPATH)/bin), or set INTERLOCK_MCP_GRAFANA to its path"
)


class GrafanaRequired(RuntimeError):
    """Grafana is not reachable, so Interlock cannot do its job.

    Raised rather than caught into a placeholder. Interlock's output is a breach
    record in Grafana; there is no useful degraded version of that.
    """


def grafana_url() -> str:
    url = os.getenv("GRAFANA_URL", "").strip().rstrip("/")
    if not url:
        raise GrafanaRequired("GRAFANA_URL is not set. Point it at your Grafana Cloud stack.")
    return url


def token_present() -> bool:
    return bool(os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip())


def server_command() -> list[str]:
    """Locate the official mcp-grafana executable, or say how to get it."""
    override = os.getenv("INTERLOCK_MCP_GRAFANA", "").strip()
    if override:
        if not Path(override).exists():
            raise GrafanaRequired(f"INTERLOCK_MCP_GRAFANA points at {override}, which does not exist")
        return [override, "-t", "stdio"]

    gopath = os.getenv("GOPATH") or str(Path.home() / "go")
    candidates = [
        Path(gopath) / "bin" / "mcp-grafana",
        Path(sys.executable).parent / "mcp-grafana",
        Path.home() / ".local" / "bin" / "mcp-grafana",
        Path("/usr/local/bin/mcp-grafana"),
        Path("/opt/homebrew/bin/mcp-grafana"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate), "-t", "stdio"]
    found = shutil.which("mcp-grafana")
    if found:
        return [found, "-t", "stdio"]
    raise GrafanaRequired(f"mcp-grafana is not on this machine. {INSTALL_HINT}")


def server_env() -> dict[str, str]:
    """The environment mcp-grafana reads.

    The token is copied from this process's environment into the child's and is
    never written anywhere else. Nothing in Interlock logs, prints or serialises it.
    """
    if not token_present():
        raise GrafanaRequired(
            "GRAFANA_SERVICE_ACCOUNT_TOKEN is not set. Interlock writes breach records "
            "into Grafana Cloud, so without a token there is no output to produce. "
            "Create a service account token in your stack and put it in .env."
        )
    env = os.environ.copy()
    env["GRAFANA_URL"] = grafana_url()
    env["GRAFANA_SERVICE_ACCOUNT_TOKEN"] = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
    # mcp-grafana starts with org_id=0 unless told otherwise, and Grafana's alerting
    # provisioning API rejects that outright: `alerting_manage_rules` create returns
    # "org_id is required and must be greater than 0" while dashboards, folders and
    # annotations all succeed. So a stack can look fully writable and still refuse the
    # one write that opens an alert. Setting it explicitly is the fix.
    env["GRAFANA_ORG_ID"] = os.getenv("GRAFANA_ORG_ID", "1")
    return env


# --- the call rail --------------------------------------------------------


@dataclass
class ToolCall:
    """One MCP tool call, kept so the run can show its own evidence."""

    tool: str
    ok: bool
    elapsed_ms: int
    response_chars: int
    summary: str
    args_keys: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "response_chars": self.response_chars,
            "summary": self.summary,
            "args_keys": self.args_keys,
        }


_REDACT_KEYS = {"token", "password", "secret", "authorization", "apikey", "api_key"}

_RATE_LIMIT_ATTEMPTS = 6


def _errored(result: Any) -> bool:
    """True when the MCP call failed, across both SDK field spellings."""
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", True)
    return bool(flag)



def _safe_arg_keys(args: dict) -> list[str]:
    return sorted(k for k in args if k.lower() not in _REDACT_KEYS)


class GrafanaSession:
    """A live mcp-grafana session plus the record of what was asked of it."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.calls: list[ToolCall] = []
        self.throttled = 0
        self.tool_names: list[str] = []

    async def load_tools(self) -> list[str]:
        listed = await self._session.list_tools()
        self.tool_names = sorted(t.name for t in listed.tools)
        return self.tool_names

    def has(self, tool: str) -> bool:
        return tool in self.tool_names

    async def call(self, tool: str, args: dict, *, required: bool = True) -> str:
        """Invoke one MCP tool and record the outcome.

        `required=False` is for probing: a stack that lacks a capability answers
        with an error, and that answer is information rather than a failure.
        """
        started = time.monotonic()
        result, text, errored = await self._call_with_backoff(tool, args)
        elapsed = int((time.monotonic() - started) * 1000)
        # The MCP Python SDK renamed this field: older releases expose `isError`,
        # current ones expose `is_error` with `isError` only as the wire alias. That is
        # handled in _errored below, which defaults to True so an SDK exposing neither
        # is treated as a failure rather than a silent success.
        record = ToolCall(
            tool=tool,
            ok=not errored,
            elapsed_ms=elapsed,
            response_chars=len(text),
            summary=_first_line(text),
            args_keys=_safe_arg_keys(args),
        )
        self.calls.append(record)
        _append_log(record)
        if errored and required:
            raise GrafanaRequired(f"mcp-grafana {tool} failed: {text[:400]}")
        if errored:
            raise ToolUnavailable(tool, text[:400])
        return text

    async def _call_with_backoff(self, tool: str, args: dict):
        """Ride out Grafana Cloud's annotation rate limit instead of losing the write.

        The annotation endpoint on Grafana Cloud is a small token bucket: about three
        POSTs land, the fourth returns `[POST /annotations] postAnnotation (status
        429): {}`, and the bucket refills within a second. A title with a dozen
        out-of-corridor spans hits that every time.

        Retrying here rather than at the workflow node is deliberate. Retrying the node
        would re-run its dashboard write and re-post the annotations that already
        landed, so a rate limit would turn into duplicate breach records. Backing off
        around the single throttled call keeps the record exactly once.
        """
        delay = 0.6
        last = None
        for attempt in range(_RATE_LIMIT_ATTEMPTS):
            result = await self._session.call_tool(tool, args)
            text = "".join(getattr(part, "text", "") or "" for part in result.content)
            errored = _errored(result)
            last = (result, text, errored)
            if not errored or "429" not in text:
                return last
            if attempt == _RATE_LIMIT_ATTEMPTS - 1:
                break
            self.throttled += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)
        return last

    async def call_json(self, tool: str, args: dict, *, required: bool = True) -> Any:
        raw = await self.call(tool, args, required=required)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


class ToolUnavailable(RuntimeError):
    """A tool exists on the server but this stack will not run it."""

    def __init__(self, tool: str, detail: str) -> None:
        super().__init__(f"{tool}: {detail}")
        self.tool = tool
        self.detail = detail


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()[:1]
    return (line[0] if line else "")[:200]


def _append_log(record: ToolCall) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **record.as_dict()}) + "\n"
        )


def grafana_preflight() -> dict[str, str]:
    """Refuse to start a run that has nowhere to file a breach.

    Three things have to be true before Interlock is worth starting: a stack URL, a
    service account token for it, and the official mcp-grafana binary to reach it
    through. Each of the three raises `GrafanaRequired` with the fix, and none of them
    is recoverable by degrading, because the deliverable of a run is a breach record in
    Grafana Cloud and a local imitation of one is not the product.

    Returns the URL and the resolved server path, neither of which is a secret. The
    token is confirmed present and never returned, printed or logged.
    """
    url = grafana_url()
    if not token_present():
        raise GrafanaRequired(
            "GRAFANA_SERVICE_ACCOUNT_TOKEN is not set. Interlock's output is a breach "
            "record in Grafana Cloud, so without a token there is nothing for a run to "
            "produce. Create a service account token with dashboard, annotation and "
            "alert rule write in your stack and put it in .env."
        )
    return {"grafana_url": url, "mcp_grafana": server_command()[0]}


@asynccontextmanager
async def open_session(timeout: float = 120.0):
    """Start mcp-grafana, initialise, list tools, yield a GrafanaSession."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd = server_command()
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=server_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=None) as raw:
            await raw.initialize()
            session = GrafanaSession(raw)
            await session.load_tools()
            yield session
