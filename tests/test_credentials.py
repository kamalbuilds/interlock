"""Take a credential away and the run must stop, not quietly produce less.

This is the file that decides whether the two partner integrations are load-bearing or
decorative. A product that keeps working with the Grafana token removed was never
writing to Grafana; a product that keeps working with the Gemini credentials removed had
an arithmetic branch behind the model all along. So each test here deletes one thing
from the environment and asserts that `run_fleet` refuses, and asserts it refuses
*before* opening an MCP session or spending a minute in ffmpeg, because a failure that
arrives after the work is a failure that costs the work.

The sentinels matter as much as the exceptions. `open_session` and `measure` are both
replaced with functions that fail the test if they are reached, so a future refactor
that moves the credential check below them turns these green tests red.
"""

from __future__ import annotations

import asyncio

import pytest

from interlock.agent import pipeline
from interlock.agent.pipeline import GeminiRequired, require_gemini, run_fleet
from interlock.grafana.client import GrafanaRequired, grafana_preflight

CREDENTIAL_VARS = (
    "GRAFANA_URL",
    "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    "GEMINI_MODEL",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


@pytest.fixture
def env(monkeypatch):
    """A complete, working credential set, with placeholder values.

    Every test starts from here and removes exactly one thing, so a test that fails is
    naming the credential it removed rather than an accident of environment.
    """
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRAFANA_URL", "https://example.grafana.net")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "placed-by-the-operator")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    return monkeypatch


@pytest.fixture
def tripwires(monkeypatch):
    """Fail the test if the run reaches Grafana or ffmpeg at all."""
    reached: list[str] = []

    def no_session(*_args, **_kwargs):
        reached.append("open_session")
        raise AssertionError("run_fleet opened an MCP session despite a missing credential")

    def no_measure(*_args, **_kwargs):
        reached.append("measure")
        raise AssertionError("run_fleet ran ffmpeg despite a missing credential")

    monkeypatch.setattr(pipeline, "open_session", no_session)
    monkeypatch.setattr(pipeline, "measure", no_measure)
    return reached


# --- the full credential set is a working baseline ------------------------


def test_the_complete_credential_set_passes_both_preflights(env):
    """Without this, every test below could pass because the checks always raise."""
    assert grafana_preflight()["grafana_url"] == "https://example.grafana.net"
    assert require_gemini() == "gemini-2.5-flash"


# --- Grafana ---------------------------------------------------------------


def test_removing_the_grafana_token_stops_the_run(env, tripwires):
    env.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    with pytest.raises(GrafanaRequired) as raised:
        asyncio.run(run_fleet(["night_tide"]))
    assert "GRAFANA_SERVICE_ACCOUNT_TOKEN" in str(raised.value)
    assert not tripwires, f"the run got as far as {tripwires} before refusing"


def test_removing_the_grafana_url_stops_the_run(env, tripwires):
    env.delenv("GRAFANA_URL")
    with pytest.raises(GrafanaRequired) as raised:
        asyncio.run(run_fleet(["night_tide"]))
    assert "GRAFANA_URL" in str(raised.value)
    assert not tripwires


def test_the_grafana_refusal_names_the_fix_and_never_the_token(env):
    env.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN")
    with pytest.raises(GrafanaRequired) as raised:
        grafana_preflight()
    message = str(raised.value)
    assert "service account token" in message
    # The token value must never travel in an error message.
    env.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa-do-not-print-me")
    assert "glsa-do-not-print-me" not in message


# --- Gemini ----------------------------------------------------------------


def test_removing_the_gemini_model_stops_the_run(env, tripwires):
    env.delenv("GEMINI_MODEL")
    with pytest.raises(GeminiRequired) as raised:
        asyncio.run(run_fleet(["night_tide"]))
    assert "GEMINI_MODEL" in str(raised.value)
    assert not tripwires


def test_removing_the_vertex_project_stops_the_run(env, tripwires):
    """The model name alone is not a credential.

    This is the case that would otherwise fail deep inside the triage node, after
    ffmpeg had measured the whole fleet, as a transport error with no mention of the
    thing that was missing.
    """
    env.delenv("GOOGLE_CLOUD_PROJECT")
    with pytest.raises(GeminiRequired) as raised:
        asyncio.run(run_fleet(["night_tide"]))
    assert "GOOGLE_CLOUD_PROJECT" in str(raised.value)
    assert not tripwires


def test_removing_the_vertex_location_stops_the_run(env, tripwires):
    env.delenv("GOOGLE_CLOUD_LOCATION")
    with pytest.raises(GeminiRequired) as raised:
        asyncio.run(run_fleet(["night_tide"]))
    assert "GOOGLE_CLOUD_LOCATION" in str(raised.value)
    assert not tripwires


def test_an_api_key_is_an_acceptable_gemini_credential_instead_of_vertex(env):
    """The check must not be a hardcoded demand for the way this machine happens to be set up."""
    env.delenv("GOOGLE_GENAI_USE_VERTEXAI")
    env.delenv("GOOGLE_CLOUD_PROJECT")
    env.delenv("GOOGLE_CLOUD_LOCATION")
    with pytest.raises(GeminiRequired):
        require_gemini()
    env.setenv("GOOGLE_API_KEY", "placed-by-the-operator")
    assert require_gemini() == "gemini-2.5-flash"


def test_the_graph_cannot_be_built_without_gemini(env):
    """No graph, not a graph with the model nodes removed.

    The distinction is the whole point. A build that succeeded and returned a workflow
    missing its three LlmAgent nodes would run to completion, write a dashboard, and
    file whichever title happened to sort first, with nothing on screen to say a
    decision had been skipped.
    """
    env.delenv("GEMINI_MODEL")
    with pytest.raises(GeminiRequired):
        pipeline.build_workflow()


def test_the_graph_built_with_gemini_carries_all_three_llm_nodes(env):
    """The paired half of the test above: the nodes are really there when it does build."""
    workflow = pipeline.build_workflow()
    names = _node_names(workflow)
    for expected in ("survey", "triage", "review"):
        assert expected in names, f"{expected} is missing from {sorted(names)}"
    for expected in ("probe", "measure_fleet", "file", "repair", "close", "publish"):
        assert expected in names, f"{expected} is missing from {sorted(names)}"


def _node_names(workflow) -> set[str]:
    """Every node reachable from the workflow's edge declarations.

    An edge in this ADK release is a plain tuple of nodes, and a routed edge is a tuple
    whose last element is a dict of route name to node, so the walk has to handle both
    rather than assume one.
    """
    found: set[str] = set()

    def walk(item) -> None:
        if isinstance(item, (list, tuple)):
            for element in item:
                walk(element)
            return
        if isinstance(item, dict):
            for element in item.values():
                walk(element)
            return
        name = getattr(item, "name", None)
        if isinstance(name, str):
            found.add(name)

    walk(workflow.edges)
    return found
