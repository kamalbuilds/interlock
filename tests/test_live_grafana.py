"""The tests that touch the real thing.

Everything in the rest of the suite is arithmetic and payload shape. These are the ones
that would have caught a Grafana integration that never worked, and they are the reason
the tally at the end of a run is worth reading: they skip on a bare clone with a red
banner saying so, and they FAIL rather than skip when the environment names a stack that
does not answer.

What is deliberately not here: a mock Grafana. A fake stack agrees with whatever the code
sends it, which is the definition of a check that cannot fail. The only way to know that
`update_dashboard` stored a 100 ms loudness series is to ask Grafana for the dashboard
back and compare what it returns against ffmpeg, and that needs Grafana.
"""

from __future__ import annotations

from interlock.grafana import surfaces
from interlock.agent.survey import READ_ONLY_TOOLS
from interlock.grafana.capability import PROBE_DASHBOARD_UID

# The tools Interlock actually invokes at runtime. If mcp-grafana or the stack stops
# offering one of these, that is a break in the integration and this is where it shows.
TOOLS_INTERLOCK_NEEDS = (
    "user_info",
    "list_datasources",
    "create_folder",
    "update_dashboard",
    "get_dashboard_by_uid",
    "create_annotation",
    "get_annotations",
    "alerting_manage_rules",
    "query_loki_logs",
    "grafana_api_request",
)


def test_the_mcp_server_answers_and_the_stack_knows_who_we_are(live_grafana):
    assert live_grafana["login"], "user_info returned no login for this token"
    assert live_grafana["version"], "GET /api/health returned no Grafana version"
    assert live_grafana["mcp_calls"] > 5


def test_every_tool_interlock_calls_is_offered_by_this_server(live_grafana):
    offered = set(live_grafana["tools"])
    missing = [t for t in TOOLS_INTERLOCK_NEEDS if t not in offered]
    assert not missing, f"mcp-grafana on this stack does not offer {missing}"
    # And the list is a real list, not one entry that happens to match.
    assert len(offered) >= 40, f"only {len(offered)} tools offered, which is not a full server"


def test_the_stack_has_a_datasource_that_can_carry_the_measured_series(live_grafana):
    assert live_grafana["datasource_count"] > 0
    assert live_grafana["infinity_uid"], (
        "no Infinity datasource on this stack, so the 100 ms loudness series has nothing "
        "to be plotted through"
    )


def test_a_dashboard_written_through_mcp_can_be_read_back(live_grafana):
    assert live_grafana["dashboard_written_and_read"], (
        "update_dashboard reported success and get_dashboard_by_uid could not find the uid"
    )


def test_the_loudness_series_survives_the_round_trip_through_grafana(live_grafana):
    """The assertion a 200 from update_dashboard cannot make.

    A dashboard save succeeds whether or not the panel carries any data, so the only
    proof that the measured series landed is to compare what Grafana gives back against
    what ffmpeg produced, sample by sample.
    """
    readback = live_grafana["series_readback"]
    assert readback.holds, readback.evidence
    # Not a vacuous comparison over an empty series.
    assert len(live_grafana["measurement"].samples) > 40
    assert "rows came back out of Grafana" in readback.evidence


def test_an_annotation_written_through_mcp_is_findable_by_its_own_tag(live_grafana):
    assert live_grafana["annotations_found"] >= 1, (
        f"create_annotation reported success and get_annotations found "
        f"{live_grafana['annotations_found']} rows for tag {live_grafana['annotation_tag']}"
    )
    assert live_grafana["annotation_tag"] in live_grafana["annotations_raw"]


def test_the_suite_cleans_up_the_scratch_dashboard_it_wrote(live_grafana):
    """A test that leaves objects behind is a test that changes the next run's answer."""
    assert live_grafana["scratch_deleted_status"] == 200
    # And the delivery run's own probe dashboard is a different uid, so nothing the suite
    # does can remove a dashboard a run wrote.
    assert surfaces.FLEET_DASHBOARD_UID != PROBE_DASHBOARD_UID


# --- Gemini, against the same live stack ----------------------------------


def test_gemini_finds_this_stacks_datasources_by_itself(live_survey):
    """The survey node, run for real: no uid in this test, only what came back.

    The model is handed the MCP session and nothing else. Its answer has to name a
    datasource this stack really has and really is of the type an inline CSV query needs,
    which `resolve_survey` establishes by reading the uid back out of Grafana.
    """
    resolved = live_survey["resolved"]
    assert resolved["series_uid"], "the survey node named no series datasource"
    assert resolved["series_type"] == "yesoreyeram-infinity-datasource"
    assert resolved["resolved"]["series_datasource_uid"]["resolved"] is True


def test_gemini_actually_called_the_mcp_server_rather_than_guessing(live_survey):
    """From the runner's event stream, not from what the model says about itself."""
    called = [c["tool"] for c in live_survey["toolset_calls"] if c["direction"] == "call"]
    assert called, "the survey node returned an answer without calling a single tool"
    assert any("datasource" in name for name in called), (
        f"the survey node named datasource uids without asking for a datasource: {called}"
    )
    # Every tool it reached is one the allowlist admits, so discovery cannot become a write.
    assert set(called) <= set(READ_ONLY_TOOLS), f"unexpected tools reached: {called}"


def test_every_tool_the_survey_agent_may_hold_exists_on_this_stack(live_grafana):
    """An allowlist naming a tool this server does not offer silently shrinks the agent.

    ADK's tool_filter drops unknown names without complaining, so a typo would remove a
    capability from the model with nothing to say so. This compares the allowlist against
    the server's own list.
    """
    offered = set(live_grafana["tools"])
    unknown = [name for name in READ_ONLY_TOOLS if name not in offered]
    assert not unknown, f"the survey allowlist names tools this server does not offer: {unknown}"


def test_gemini_reports_what_the_state_history_query_returned(live_survey):
    """The model must write down the query it sent and what came back, not a summary of hope."""
    reported = live_survey["reported"]
    assert reported.get("query_you_ran"), "the survey node ran no query against the state history"
    assert reported.get("query_result"), "the survey node reported no result for its own query"
    assert "state-history" in reported["query_you_ran"]
