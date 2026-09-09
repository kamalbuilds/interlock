"""The gate that decides whether a run touched Grafana and Gemini, or only itself.

A one-line green tally is the most reliable way to ship an integration that never
executed. pytest counts a skip as a non-failure, so a suite whose every partner-facing
test skipped still prints a number with no red in it, and the number is the thing people
read. Interlock's whole claim is a live Grafana integration, so this file exists to make
that impossible to misread.

Three rules.

**A configured endpoint that does not answer is red, never a skip.** If `GRAFANA_URL`
and `GRAFANA_SERVICE_ACCOUNT_TOKEN` are both set, this run asserted that a stack is
there. A gate that then skips is reporting a broken configuration as an absent one. Only
a bare clone with nothing configured may skip quietly, and it says in words what it did
not prove.

**The live work happens once and every assertion reads it.** Opening mcp-grafana per
test would spawn the child process a dozen times and split the transcript across a dozen
records. The fixtures below do the real interaction once and hand back what came out of
it, so each test constrains a different post-condition of the same real run.

**The run says at the end whether the integration was exercised.**
`pytest_terminal_summary` prints either the stack it reached or a red banner naming what
this run does not prove.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# What each gate turned away, so the summary can say what the run does not prove.
_NOT_EXERCISED: dict[str, list[str]] = {"grafana": [], "gemini": []}
_EXERCISED: dict[str, str] = {}

HOW_TO_REACH_GRAFANA = (
    "Set GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN for a Grafana Cloud stack whose "
    "service account can write dashboards, annotations and alert rules, and install the "
    "server with `go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest`. "
    "Without them the offline suite still constrains the SLO arithmetic, the payload "
    "shapes and the credential gates, and proves nothing whatever about Grafana."
)

HOW_TO_REACH_GEMINI = (
    "Set GEMINI_MODEL plus either GOOGLE_GENAI_USE_VERTEXAI=true with "
    "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION, or GOOGLE_API_KEY. Without them "
    "the three LlmAgent nodes are never called and this run proves nothing about them."
)

SUITE_DASHBOARD_UID = "interlock-suite-live-check"


def grafana_configured() -> bool:
    """True when this run claimed a real stack rather than being a bare clone."""
    url = os.getenv("GRAFANA_URL", "").strip()
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    return bool(url) and bool(token) and "localhost" not in url and "127.0.0.1" not in url


def gemini_configured() -> bool:
    if not os.getenv("GEMINI_MODEL", "").strip():
        return False
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes"):
        return bool(
            os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            and os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
        )
    return bool(
        os.getenv("GOOGLE_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    )


def _describe(exc: BaseException, depth: int = 0) -> str:
    """Flatten an exception, including a TaskGroup's sub-exceptions.

    Without this the failure from an unreachable stack reads as "ExceptionGroup:
    unhandled errors in a TaskGroup (1 sub-exception)", which names the async plumbing
    and hides the DNS or auth error that is the actual finding. anyio wraps everything
    the MCP stdio client raises, so the useful message is always one level down.
    """
    text = f"{type(exc).__name__}: {exc}"
    inner = getattr(exc, "exceptions", None)
    if inner and depth < 3:
        text += " <- " + " | ".join(_describe(e, depth + 1) for e in inner)
    elif exc.__cause__ is not None and depth < 3:
        text += " <- " + _describe(exc.__cause__, depth + 1)
    return text


def _absent(integration: str, reason: str) -> None:
    """Skip a bare clone. Fail a run that said the endpoint was there and was wrong."""
    _NOT_EXERCISED[integration].append(reason)
    configured = grafana_configured() if integration == "grafana" else gemini_configured()
    if configured:
        pytest.fail(
            f"this run is configured to exercise {integration} and could not: {reason} "
            "A configured endpoint that does not answer is a broken configuration, not "
            "an absent one, so this is red rather than skipped."
        )
    pytest.skip(reason)


# --- the live Grafana interaction, performed once --------------------------


@pytest.fixture(scope="session")
def live_grafana() -> dict:
    """Real work against the real stack through the real mcp-grafana server.

    Everything here is a read, or a write to a scratch dashboard this fixture owns and
    deletes on the way out. Nothing touches the delivery folder a run writes into.
    """
    if not grafana_configured():
        _absent(
            "grafana",
            "GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN are not both set, so there "
            f"is no stack to exercise. {HOW_TO_REACH_GRAFANA}",
        )

    from interlock.grafana.client import GrafanaRequired

    try:
        evidence = asyncio.run(_exercise_grafana())
    except GrafanaRequired as exc:
        _absent("grafana", str(exc))
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        _absent("grafana", _describe(exc))
    _EXERCISED["grafana"] = os.getenv("GRAFANA_URL", "")
    return evidence


async def _exercise_grafana() -> dict:
    """One session: identify, discover, write, read back, tear down."""
    import json
    from datetime import datetime, timezone

    from interlock.grafana.capability import Capabilities
    from interlock.grafana.client import open_session
    from interlock.grafana.surfaces import (
        anchor_for,
        series_readback_assertion,
        title_dashboard,
    )

    async with open_session() as session:
        who = await session.call_json("user_info", {})
        health = await session.call_json("grafana_api_request", {"endpoint": "/api/health"})
        datasources = await session.call_json("list_datasources", {"limit": 100})

        rows = (
            datasources
            if isinstance(datasources, list)
            else (datasources or {}).get("datasources", [])
        )
        infinity = next(
            (
                str(r.get("uid"))
                for r in rows
                if isinstance(r, dict) and r.get("type") == "yesoreyeram-infinity-datasource"
            ),
            "",
        )

        # A real ffmpeg measurement, written onto a real panel and read back out of what
        # Grafana stored. The signal is generated by ffmpeg here rather than downloaded
        # so this check does not need 190 MB of film present, but every number in it came
        # out of the ebur128 filter.
        measurement = _measure_a_generated_signal()

        caps = Capabilities(infinity_uid=infinity)
        anchor = datetime.now(timezone.utc)
        anchor_ms, span_ms = anchor_for(measurement, anchor)
        dashboard = title_dashboard(measurement, caps, anchor_ms, span_ms)
        dashboard["uid"] = SUITE_DASHBOARD_UID
        dashboard["title"] = "Interlock test suite live check"
        dashboard["tags"] = ["interlock", "test-suite"]

        await session.call(
            "update_dashboard",
            {
                "dashboard": dashboard,
                "overwrite": True,
                "message": "Interlock test suite live check",
            },
        )
        stored = await session.call("get_dashboard_by_uid", {"uid": SUITE_DASHBOARD_UID})
        readback = series_readback_assertion(
            stored, measurement, anchor_ms, SUITE_DASHBOARD_UID
        )

        tag = f"interlock-suite-{int(time.time())}"
        await session.call(
            "create_annotation",
            {
                "dashboardUid": SUITE_DASHBOARD_UID,
                "panelId": 2,
                "text": (
                    "Interlock test suite live check, "
                    f"{measurement.integrated_lufs:.1f} LUFS"
                ),
                "tags": ["interlock", "test-suite", tag],
                "time": anchor_ms,
                "timeEnd": anchor_ms + 1000,
            },
        )
        found = await session.call_json(
            "get_annotations",
            {
                "tags": ["interlock", "test-suite", tag],
                "limit": 10,
                # Both bounds. Grafana's annotation search with only `from` set answered
                # zero rows for an annotation it had just accepted, which is how this
                # test earned its place: the write returned 200 and the read found
                # nothing, and only asking properly told the two apart.
                "from": anchor_ms - 120_000,
                "to": anchor_ms + 3_600_000,
            },
        )
        # mcp-grafana wraps this endpoint's answer as {"Payload": [...]}, so a caller
        # that expects a bare list reads every successful query as zero rows. That is
        # what this test found on its first live run: the write returned 200, the read
        # said nothing was there, and the shape was the reason.
        annotation_rows = (
            found
            if isinstance(found, list)
            else (found or {}).get("Payload") or (found or {}).get("annotations") or []
        )
        if not isinstance(annotation_rows, list):
            annotation_rows = []

        deleted = await session.call_json(
            "grafana_api_request",
            {"endpoint": f"/api/dashboards/uid/{SUITE_DASHBOARD_UID}", "method": "DELETE"},
            required=False,
        )

        return {
            "url": os.getenv("GRAFANA_URL", ""),
            "tools": list(session.tool_names),
            "login": str(who.get("login", "")) if isinstance(who, dict) else "",
            "version": str(
                ((health or {}).get("data", {}) if isinstance(health, dict) else {}).get(
                    "version", ""
                )
            ),
            "datasource_count": len(rows) if isinstance(rows, list) else 0,
            "infinity_uid": infinity,
            "measurement": measurement,
            "dashboard_written_and_read": SUITE_DASHBOARD_UID in stored,
            "series_readback": readback,
            "annotation_tag": tag,
            "annotations_found": len(annotation_rows),
            "annotations_raw": json.dumps(annotation_rows)[:600],
            "scratch_deleted_status": (
                (deleted or {}).get("status") if isinstance(deleted, dict) else None
            ),
            "mcp_calls": len(session.calls),
        }


def _measure_a_generated_signal():
    """A real ffmpeg ebur128 measurement over a signal ffmpeg generated here.

    Six seconds at two very different levels, so the series has a real step in it. A
    constant tone would let a broken round trip match by accident.
    """
    import shutil
    import subprocess
    import tempfile

    from interlock.measure import measure

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not on PATH, so no measurement can be made")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signal.wav"
        built = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-y",
                "-filter_complex",
                "sine=frequency=220:duration=3,volume=-8dB[a];"
                "sine=frequency=440:duration=3,volume=-26dB[b];[a][b]concat=n=2:v=0:a=1[out]",
                "-map", "[out]",
                "-ar", "48000", "-c:a", "pcm_s16le", str(path),
            ],
            capture_output=True,
            text=True,
        )
        if built.returncode != 0 or not path.exists():
            raise RuntimeError(f"could not build the test signal: {built.stderr[-400:]}")
        return measure(path, title_id="suite_signal", title="Interlock suite signal", seconds=6)


# --- the live Gemini interaction, performed once ---------------------------


@pytest.fixture(scope="session")
def live_survey(live_grafana) -> dict:
    """Run the real survey node against the real stack and return what Gemini decided.

    This is the joint test, and it needs both partner integrations at once, because the
    thing being checked is that a model handed nothing but an MCP session finds the
    datasources this stack really has.
    """
    if not gemini_configured():
        _absent("gemini", f"no Gemini credential is present. {HOW_TO_REACH_GEMINI}")
    try:
        outcome = asyncio.run(_exercise_survey())
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        _absent("gemini", _describe(exc))
    _EXERCISED["gemini"] = os.getenv("GEMINI_MODEL", "")
    return outcome


async def _exercise_survey() -> dict:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from interlock.agent import pipeline
    from interlock.agent.pipeline import _collect_agent_tool_calls
    from interlock.agent.survey import build_survey_agent, resolve_survey
    from interlock.grafana.client import open_session

    async with open_session() as session:
        pipeline._CONTEXT = pipeline.RunContext(session=session)
        try:
            agent = build_survey_agent(os.environ["GEMINI_MODEL"].strip())
            runner = InMemoryRunner(agent=agent, app_name="interlock-suite")
            created = await runner.session_service.create_session(
                app_name="interlock-suite", user_id="suite", state={}
            )
            toolset_calls: list[dict] = []
            async for event in runner.run_async(
                user_id="suite",
                session_id=created.id,
                new_message=types.Content(
                    role="user",
                    parts=[types.Part(text="Find out what this Grafana stack has.")],
                ),
            ):
                _collect_agent_tool_calls(toolset_calls, event)
            final = await runner.session_service.get_session(
                app_name="interlock-suite", user_id="suite", session_id=created.id
            )
            reported = dict(final.state).get("survey") or {}
            resolved = await resolve_survey(session, reported)
            return {
                "reported": reported,
                "resolved": resolved,
                # From the runner's own events. The survey node's tools are an ADK
                # McpToolset with its own connection to mcp-grafana, so its calls do not
                # appear in this session's log, and the event stream is the honest source.
                "toolset_calls": toolset_calls,
            }
        finally:
            pipeline._CONTEXT = None


# --- the summary that makes a green tally unambiguous ---------------------


def pytest_terminal_summary(terminalreporter) -> None:
    for integration, how in (
        ("grafana", HOW_TO_REACH_GRAFANA),
        ("gemini", HOW_TO_REACH_GEMINI),
    ):
        reasons = _NOT_EXERCISED[integration]
        if integration in _EXERCISED and not reasons:
            terminalreporter.write_line(
                f"{integration}: exercised for real against {_EXERCISED[integration]}",
                green=True,
            )
            continue
        if not reasons:
            if integration == "gemini" and _NOT_EXERCISED["grafana"]:
                # The Gemini live test runs the survey node over the same MCP session, so
                # losing Grafana takes it with it. Saying "nothing ran" here would read as
                # "no test covers this", which is a different and untrue statement.
                terminalreporter.write_sep(
                    "=", "gemini integration NOT exercised", red=True
                )
                terminalreporter.write_line(
                    "the live Gemini test drives the survey node over the same "
                    "mcp-grafana session, which was not available, so it never ran."
                )
                continue
            terminalreporter.write_line(
                f"{integration}: no live test ran in this selection, so this run says "
                "nothing about it either way",
                yellow=True,
            )
            continue
        terminalreporter.write_sep("=", f"{integration} integration NOT exercised", red=True)
        terminalreporter.write_line(reasons[0])
        if how not in reasons[0]:
            terminalreporter.write_line(how)
