"""The guard on the agent's choices, and the series read-back.

Two things are constrained here.

First, the survey node picks the datasource uids every later write goes to. A model that
names a uid this stack does not have would otherwise produce a dashboard that saves
successfully and renders an empty panel, which is the exact class of defect this product
exists to catch in films. `resolve_survey` reads every uid back before anything is
written, and the tests below make it face a stack that refuses.

Second, `series_readback_assertion` is the check that the 100 ms loudness series landed
in Grafana intact rather than that a dashboard save returned 200. Its ability to fail is
the point, so it is exercised against a round trip that was tampered with, one that came
back empty, and one that came back short.

On the doubles. The stack stand-in here answers 404 for a uid it does not have, which is
the behaviour a real Grafana Cloud stack cannot be asked for on demand: there is no way
to make a live stack forget a datasource for the duration of one test. The logic under
test is the real `resolve_survey` and the real assertion builder. Everything that
depends on Grafana agreeing with Interlock is proven instead by a live run.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from pathlib import Path

from interlock.agent import survey
from interlock.agent.survey import resolve_survey
from interlock.grafana import surfaces
from interlock.grafana.client import GrafanaRequired
from interlock.measure import Measurement, Sample

INFINITY = "yesoreyeram-infinity-datasource"


class StackDouble:
    """A stack that knows exactly which datasource uids it has, and refuses the rest."""

    def __init__(self, datasources: dict[str, tuple[str, str]]) -> None:
        self.datasources = datasources
        self.tool_names = ["grafana_api_request", "list_datasources", "query_loki_logs"]
        self.endpoints: list[str] = []

    def has(self, tool: str) -> bool:
        return tool in self.tool_names

    async def load_tools(self) -> list[str]:
        return self.tool_names

    async def call_json(self, tool: str, args: dict, *, required: bool = True):
        endpoint = args.get("endpoint", "")
        self.endpoints.append(endpoint)
        uid = endpoint.rsplit("/", 1)[-1]
        if uid in self.datasources:
            dtype, name = self.datasources[uid]
            return {"status": 200, "data": {"uid": uid, "type": dtype, "name": name}}
        return {"status": 404, "data": {"message": "Data source not found"}}

    async def call(self, tool: str, args: dict, *, required: bool = True) -> str:
        return json.dumps(await self.call_json(tool, args, required=required))


def _survey(**overrides) -> dict:
    base = {
        "tools_offered": 81,
        "series_datasource_uid": "grafanacloud-infinity",
        "series_datasource_type": INFINITY,
        "alert_state_history_datasource_uid": "grafanacloud-alert-state-history",
        "metrics_datasource_uid": "grafanacloud-prom",
        "logs_datasource_uid": "grafanacloud-logs",
        "query_you_ran": '{from="state-history"}',
        "query_result": "returned 12 rows",
        "tools_called": ["list_datasources", "query_loki_logs"],
        "reasoning": "matched on the type string",
    }
    base.update(overrides)
    return base


def _real_stack() -> StackDouble:
    return StackDouble(
        {
            "grafanacloud-infinity": (INFINITY, "grafanacloud-infinity"),
            "grafanacloud-alert-state-history": ("loki", "grafanacloud-alert-state-history"),
            "grafanacloud-prom": ("prometheus", "grafanacloud-prom"),
            "grafanacloud-logs": ("loki", "grafanacloud-logs"),
        }
    )


# --- the guard on the agent's datasource choice ---------------------------


def test_a_survey_naming_real_datasources_resolves():
    """The paired positive: the guard must let a correct survey through."""
    stack = _real_stack()
    resolved = asyncio.run(resolve_survey(stack, _survey()))
    assert resolved["series_uid"] == "grafanacloud-infinity"
    assert resolved["series_type"] == INFINITY
    assert resolved["alert_state_history_uid"] == "grafanacloud-alert-state-history"
    assert resolved["metrics_uid"] == "grafanacloud-prom"
    # Every uid was actually read back, not taken on the model's word.
    assert any("/api/datasources/uid/grafanacloud-infinity" in e for e in stack.endpoints)


def test_an_invented_series_datasource_stops_the_run():
    """A hallucinated uid must be fatal, not silently written into a panel."""
    stack = _real_stack()
    with pytest.raises(GrafanaRequired) as raised:
        asyncio.run(resolve_survey(stack, _survey(series_datasource_uid="infinity-prod")))
    message = str(raised.value)
    assert "infinity-prod" in message
    assert "404" in message


def test_a_series_datasource_of_the_wrong_type_stops_the_run():
    """Resolving is not enough: the panel query is inline CSV, which only Infinity reads."""
    stack = _real_stack()
    with pytest.raises(GrafanaRequired) as raised:
        asyncio.run(resolve_survey(stack, _survey(series_datasource_uid="grafanacloud-prom")))
    assert "prometheus" in str(raised.value)


def test_a_missing_alert_state_history_is_reported_and_not_fatal():
    """Losing the proof-of-firing datasource degrades the evidence, not the delivery record."""
    stack = StackDouble({"grafanacloud-infinity": (INFINITY, "grafanacloud-infinity")})
    resolved = asyncio.run(
        resolve_survey(stack, _survey(metrics_datasource_uid="", logs_datasource_uid=""))
    )
    assert resolved["alert_state_history_uid"] == ""
    assert resolved["resolved"]["alert_state_history_datasource_uid"]["resolved"] is False


MUTATING_TOOLS = {
    "update_dashboard",
    "create_annotation",
    "update_annotation",
    "create_folder",
    "alerting_manage_rules",
    "alerting_manage_silences",
    "alerting_manage_routing",
    "create_datasource",
    "update_datasource",
    "install_plugin",
    "create_incident",
    "update_incident",
    "add_activity_to_incident",
    "create_snapshot",
    "delete_snapshot",
    "update_alert_group",
    "validate_provisioning_file",
}


def test_the_survey_toolset_holds_no_tool_that_can_change_the_stack():
    """The discovery agent must not be able to overwrite the record it went to describe.

    Every write in this product belongs to a node that owns a post-condition. An
    exploring model with update_dashboard in reach could overwrite the delivery board
    mid-run, and nothing downstream would notice, because the dashboard would still
    exist and still read back.
    """
    held = set(survey.READ_ONLY_TOOLS)
    assert not held & MUTATING_TOOLS, f"the survey node can mutate the stack with {held & MUTATING_TOOLS}"
    # And the allowlist is a real set of tools, not one entry that trivially passes.
    assert len(held) >= 15
    assert "list_datasources" in held and "query_loki_logs" in held


@pytest.fixture
def stack_env(monkeypatch):
    """The toolset needs a stack to point at, so give it placeholder values.

    The token is only ever copied into the child process's environment, so a placeholder
    is enough to build the connection parameters without reaching anything.
    """
    monkeypatch.setenv("GRAFANA_URL", "https://example.grafana.net")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "placed-by-the-operator")
    return monkeypatch


def test_the_toolset_is_the_adk_primitive_over_the_official_server(stack_env):
    """The agent's tools come from mcp-grafana itself, not from a wrapper of ours."""
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    toolset = survey.grafana_toolset()
    assert isinstance(toolset, McpToolset)
    params = toolset._mcp_session_manager._connection_params.server_params
    assert Path(params.command).name == "mcp-grafana"
    assert "stdio" in params.args
    # The token reaches the child process and is not readable from the toolset object.
    assert "GRAFANA_SERVICE_ACCOUNT_TOKEN" in params.env
    assert "GRAFANA_URL" in params.env


def test_the_toolset_filter_is_an_allowlist_and_not_a_denylist(stack_env):
    """A denylist admits every tool a future mcp-grafana release adds, including writes."""
    toolset = survey.grafana_toolset()
    assert sorted(toolset.tool_filter) == sorted(survey.READ_ONLY_TOOLS)
    narrow = survey.grafana_toolset(["user_info"])
    assert narrow.tool_filter == ["user_info"]


# --- the series read-back -------------------------------------------------


def _measurement() -> Measurement:
    samples = [
        Sample(t=round(0.1 * (i + 1), 1), momentary_lufs=-26.1 - (i % 5), short_term_lufs=-26.1 - (i % 7))
        for i in range(400)
    ]
    return Measurement(
        title_id="t",
        title="A Title",
        source="/tmp/a.mp4",
        command="ffmpeg -i /tmp/a.mp4 -af ebur128=peak=true -f null -",
        window_seconds=40,
        duration_seconds=1800.0,
        integrated_lufs=-26.1,
        true_peak_dbfs=-6.0,
        lra_lu=6.0,
        samples=samples,
    )


def _round_trip(measurement: Measurement, anchor_ms: int) -> str:
    """What Grafana hands back after storing the dashboard Interlock sent."""
    caps = type("C", (), {"infinity_uid": "grafanacloud-infinity"})()
    dash = surfaces.title_dashboard(measurement, caps, anchor_ms, 40_000)
    return json.dumps({"dashboard": dash, "meta": {"version": 3}})


def _anchor_ms() -> int:
    return int(datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_an_intact_round_trip_holds():
    m = _measurement()
    anchor = _anchor_ms()
    result = surfaces.series_readback_assertion(_round_trip(m, anchor), m, anchor, "interlock-t")
    assert result.holds
    assert "every short-term value matches" in result.evidence
    # And it is comparing a real number of rows, not zero.
    assert len(surfaces.stored_series(_round_trip(m, anchor))) > 100


def test_one_altered_sample_fails_the_read_back():
    """The check must notice a single wrong number, or it is checking nothing."""
    m = _measurement()
    anchor = _anchor_ms()
    stored = _round_trip(m, anchor)
    rows = surfaces.stored_series(stored)
    target = f",{rows[7][1]:.2f},"
    tampered = stored.replace(target, ",-99.00,", 1)
    assert tampered != stored
    result = surfaces.series_readback_assertion(tampered, m, anchor, "interlock-t")
    assert not result.holds
    assert "disagree with ffmpeg" in result.evidence


def test_an_empty_panel_fails_the_read_back():
    """The failure this replaces: a dashboard that saved fine and carries no series."""
    m = _measurement()
    anchor = _anchor_ms()
    doc = json.loads(_round_trip(m, anchor))
    for panel in doc["dashboard"]["panels"]:
        if panel.get("id") == 2:
            panel["targets"][0]["data"] = ""
    result = surfaces.series_readback_assertion(json.dumps(doc), m, anchor, "interlock-t")
    assert not result.holds
    assert "Grafana returned 0 rows" in result.evidence


def test_a_truncated_series_fails_the_read_back():
    m = _measurement()
    anchor = _anchor_ms()
    doc = json.loads(_round_trip(m, anchor))
    for panel in doc["dashboard"]["panels"]:
        if panel.get("id") == 2:
            lines = panel["targets"][0]["data"].splitlines()
            panel["targets"][0]["data"] = "\n".join(lines[:20]) + "\n"
    result = surfaces.series_readback_assertion(json.dumps(doc), m, anchor, "interlock-t")
    assert not result.holds


def test_a_dashboard_that_is_not_json_fails_rather_than_passing():
    m = _measurement()
    anchor = _anchor_ms()
    result = surfaces.series_readback_assertion("Not found", m, anchor, "interlock-t")
    assert not result.holds
    assert surfaces.stored_series("Not found") == []
