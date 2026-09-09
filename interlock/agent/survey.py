"""The first node: Gemini holds the Grafana toolset and interrogates the stack itself.

Every other Grafana integration in this field starts from a query string somebody
typed. Interlock does not know, when it starts, what this stack calls its datasources
or whether it has the ones the run needs. So the first node of the graph is an
`LlmAgent` holding a real `google.adk.tools.mcp_tool.McpToolset` over the official
`grafana/mcp-grafana` server, and it is asked to find out.

The toolset is the ADK primitive, not a wrapper of our own. ADK connects to the
server, reads its tool list, and hands Gemini each tool with the server's own JSON
schema, so the model selects a tool and shapes its arguments from what mcp-grafana
declares. Nothing in this module decides which call gets made. What the model does with
that, on the stack this was built against, is call `list_datasources`, pick the
datasource that can carry a 100 ms loudness series onto a panel, locate Grafana's own
alert state history, and prove that one answers by running `query_loki_logs` against it.

READ-ONLY BY CONSTRUCTION

`tool_filter` is an allowlist of the read tools, and that is a boundary rather than a
convenience. mcp-grafana offers 81 tools against this stack, 20 of which mutate it,
including `update_dashboard`, `alerting_manage_rules`, `create_datasource` and
`install_plugin`. A discovery agent has no business holding any of them: the writes in
this product are deterministic, they belong to the nodes that own a post-condition, and
an exploring model with `update_dashboard` in reach can overwrite the delivery record it
was sent to describe. So the filter names 21 tools that only read, and the model
chooses freely inside that.

WHY THIS IS NOT DECORATION

`probe` consumes this survey. A survey that names a datasource this stack does not
have stops the run in `resolve_survey` below, because a dashboard written against an
invented datasource uid renders an empty panel, Grafana accepts that write happily, and
an empty panel is the exact failure this product exists to prevent. So the model's
answer changes what executes and a wrong answer is fatal rather than absorbed. The agent
chooses, Grafana adjudicates, and neither side is trusted alone.

TWO CONNECTIONS TO ONE SERVER, ON PURPOSE

The toolset opens its own stdio connection to mcp-grafana, alongside the session the
deterministic nodes use in `interlock/grafana/client.py`. That is two child processes of
the same pinned binary against the same stack, and it is the honest arrangement: ADK owns
the agent's connection and its lifecycle, Interlock owns the writer's. What the model
called is recorded from the ADK event stream rather than from Interlock's own call log,
so the agent's tool rail comes from the runner rather than from anything this module
reports about itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..grafana.client import GrafanaRequired, server_command, server_env

# Every mcp-grafana tool that only reads. The survey node gets these and nothing else.
# Checked against the 81 this server offers: the 60 left out either mutate the stack or
# belong to a subsystem the survey has no question about.
READ_ONLY_TOOLS = [
    "user_info",
    "list_datasources",
    "get_datasource",
    "check_datasources_health",
    "grafana_api_request",
    "search_dashboards",
    "search_folders",
    "get_dashboard_by_uid",
    "get_dashboard_summary",
    "get_dashboard_property",
    "get_dashboard_panel_queries",
    "get_annotations",
    "get_annotation_tags",
    "query_loki_logs",
    "query_loki_stats",
    "list_loki_label_names",
    "list_loki_label_values",
    "query_prometheus",
    "list_prometheus_metric_names",
    "list_alert_groups",
    "get_alert_group",
]


class StackSurvey(BaseModel):
    """What the agent found out about this Grafana stack, by asking it."""

    tools_offered: int = Field(
        description="how many tools grafana_tool_catalog reported this server offers"
    )
    series_datasource_uid: str = Field(
        description=(
            "uid of the datasource that can carry an inline CSV of 100 ms loudness "
            "samples onto a timeseries panel. On Grafana Cloud that is the Infinity "
            "datasource. Empty string only if this stack genuinely has none."
        )
    )
    series_datasource_type: str = Field(
        description="the type string of that datasource, exactly as the stack reports it"
    )
    alert_state_history_datasource_uid: str = Field(
        description=(
            "uid of the datasource holding Grafana's own alert state transition log. "
            "Empty string if this stack has none."
        )
    )
    metrics_datasource_uid: str = Field(
        description="uid of the stack's main Prometheus datasource, or empty string"
    )
    logs_datasource_uid: str = Field(
        description="uid of the stack's main Loki application log datasource, or empty string"
    )
    query_you_ran: str = Field(
        description=(
            "the exact query you sent to the alert state history datasource to prove it "
            "answers. Empty string if this stack has no such datasource."
        )
    )
    query_result: str = Field(
        description=(
            "what came back from that query, in one or two sentences, including whether "
            "it returned rows and what they were. Do not invent rows: an empty answer is "
            "a real answer and must be reported as empty."
        )
    )
    tools_called: list[str] = Field(
        description="the MCP tool names you actually invoked, in order"
    )
    reasoning: str = Field(
        description=(
            "two or three sentences: how you decided which datasource carries the "
            "series, naming the type you matched on. No em dashes."
        )
    )


SURVEY_INSTRUCTION = """\
You are establishing what a Grafana Cloud stack you have never seen can do, before a
delivery pipeline writes anything into it. Your tools are the read tools of the Grafana
MCP server itself, so their names, their arguments and their answers are that server's,
not a description of it. Do not guess a uid: every uid you report must have come back
from a call you made.

Work in this order.

1. List every datasource on the stack, with its uid, name and type.

2. Decide which one can carry an inline CSV of loudness samples onto a timeseries panel.
   The pipeline's panel query is inline CSV, which on Grafana Cloud is the Infinity
   datasource, type `yesoreyeram-infinity-datasource`. Report its uid and its exact type
   string as the stack gave them to you.

3. Find the datasource holding Grafana's own alert state history. It is a Loki
   datasource and its name says so. Then PROVE it answers: query it with the LogQL
   selector `{from="state-history"}`.

   Use the relative strings `now-60m` and `now` for the start and end of the range, not
   absolute timestamps and never a date you remember. Grafana resolves the relative form
   against its own clock, which is the only clock that matters here, and an absolute range
   picked from memory lands in a window with no data in it and proves nothing about the
   datasource.

   Report the exact query you sent and what came back, including how many log lines it
   returned. If it returned none, say it returned none: an empty answer over the last hour
   means this stack has evaluated no alert rules recently, which is information and not
   something to paper over. Never write down a row you did not receive.

4. Also report the stack's main Prometheus datasource uid and its main Loki application
   log datasource uid if it has them, or empty strings if it does not.

5. In `tools_offered`, put the number of tools you were given. In `tools_called`, put the
   names of the ones you actually invoked, in order.

You hold read tools only, so there is nothing here that can change the stack. The
pipeline that runs after you refuses to start if a uid you report does not resolve, so
accuracy matters more than completeness.
"""


# --- the toolset the agent holds ------------------------------------------


def grafana_toolset(tool_filter: list[str] | None = None):
    """The official mcp-grafana server as an ADK toolset, filtered to its read tools.

    ADK owns this connection: it starts the pinned binary, reads the tool list off the
    server, and gives the model each tool with the server's own schema. The environment
    is the same one `interlock/grafana/client.py` builds, so the token reaches the child
    process and appears nowhere else.
    """
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    from mcp import StdioServerParameters

    cmd = server_command()
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=cmd[0], args=cmd[1:], env=server_env()
            ),
            timeout=120.0,
        ),
        tool_filter=list(READ_ONLY_TOOLS if tool_filter is None else tool_filter),
    )


def build_survey_agent(model: str):
    """The LlmAgent that interrogates the stack. First node of the graph."""
    from google.adk.agents import LlmAgent

    return LlmAgent(
        name="survey",
        model=model,
        description="Interrogates the Grafana MCP server to find out what this stack has.",
        instruction=SURVEY_INSTRUCTION,
        tools=[grafana_toolset()],
        output_schema=StackSurvey,
        output_key="survey",
    )


# --- the guard that makes the survey load-bearing -------------------------


async def resolve_survey(session: Any, survey: dict) -> dict:
    """Check every uid the agent reported against the stack, and stop if one is wrong.

    A dashboard written against a datasource uid that does not exist renders an empty
    panel, and Grafana accepts the write happily: the dashboard save succeeds and the
    defect only appears when a person opens it. That is precisely the failure mode this
    product exists to catch in films, so it is not one to ship in the tool. Each uid the
    model named is therefore read back from the stack before anything is written, and a
    uid that does not resolve stops the run.

    Returns the resolved survey with each uid's confirmed type, plus the reads that
    confirmed it.
    """
    named = {
        "series_datasource_uid": survey.get("series_datasource_uid", "") or "",
        "alert_state_history_datasource_uid": (
            survey.get("alert_state_history_datasource_uid", "") or ""
        ),
        "metrics_datasource_uid": survey.get("metrics_datasource_uid", "") or "",
        "logs_datasource_uid": survey.get("logs_datasource_uid", "") or "",
    }
    resolved: dict[str, dict] = {}
    for field_name, uid in named.items():
        if not uid:
            resolved[field_name] = {"uid": "", "resolved": False, "detail": "not reported"}
            continue
        payload = await session.call_json(
            "grafana_api_request",
            {"endpoint": f"/api/datasources/uid/{uid}", "method": "GET"},
            required=False,
        )
        body = payload.get("data", {}) if isinstance(payload, dict) else {}
        status = payload.get("status") if isinstance(payload, dict) else None
        ok = status == 200 and isinstance(body, dict) and body.get("uid") == uid
        resolved[field_name] = {
            "uid": uid,
            "resolved": bool(ok),
            "type": str(body.get("type", "")) if isinstance(body, dict) else "",
            "name": str(body.get("name", "")) if isinstance(body, dict) else "",
            "detail": (
                f"GET /api/datasources/uid/{uid} returned {status}"
                if ok
                else f"GET /api/datasources/uid/{uid} returned {status}, so this uid is not on this stack"
            ),
        }

    series = resolved["series_datasource_uid"]
    if not series["resolved"]:
        raise GrafanaRequired(
            "the survey node named "
            f"{series['uid']!r} as the datasource that will carry the measured loudness "
            f"series, and this stack does not have it: {series['detail']}. Interlock "
            "stops here rather than writing a dashboard whose panel would render empty."
        )
    if series["type"] != "yesoreyeram-infinity-datasource":
        raise GrafanaRequired(
            f"the survey node named {series['uid']!r} as the series datasource, but this "
            f"stack reports its type as {series['type']!r}. The measured series is an "
            "inline CSV query, which only the Infinity datasource reads, so a panel "
            "built on that uid would render nothing."
        )

    return {
        "reported": survey,
        "resolved": resolved,
        "series_uid": series["uid"],
        "series_type": series["type"],
        "alert_state_history_uid": (
            resolved["alert_state_history_datasource_uid"]["uid"]
            if resolved["alert_state_history_datasource_uid"]["resolved"]
            else ""
        ),
        "metrics_uid": (
            resolved["metrics_datasource_uid"]["uid"]
            if resolved["metrics_datasource_uid"]["resolved"]
            else ""
        ),
        "logs_uid": (
            resolved["logs_datasource_uid"]["uid"]
            if resolved["logs_datasource_uid"]["resolved"]
            else ""
        ),
    }
