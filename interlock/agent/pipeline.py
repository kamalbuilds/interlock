"""The orchestration graph: google.adk.workflow.Workflow over one MCP session.

    START -> survey -> probe -> measure_fleet -> triage -> file -> repair -> review -> close -> publish
                                                            |                                    ^
                                                            +---- hold --------------------------+

Nine nodes, six of them functions and three of them LlmAgents, and one branch. The
shape is deterministic: the same fleet measured twice takes the same path, because the
branch is taken on a structured field, not on prose. What is not deterministic is the
judgement inside three of the nodes, and that is deliberate.

WHERE GEMINI IS LOAD-BEARING

`survey` holds a `google.adk.tools.mcp_tool.McpToolset` over the official
`grafana/mcp-grafana` server, filtered to that server's read tools, and finds out what
this stack has. ADK gives Gemini each tool with mcp-grafana's own schema, so the model
picks the call and shapes the arguments: nothing in `interlock/agent/survey.py` decides
which tool runs. It lists the datasources, chooses the one that can carry a 100 ms
loudness series onto a panel, locates Grafana's own alert state history and proves it
answers by querying it. The uids it returns are the uids every later write goes to, and
a uid this stack does not have stops the run in `resolve_survey`. So no query string in
this product was chosen by a person who had already seen the stack.

`triage` reads the measured fleet and answers a question arithmetic cannot: given
finite mix-stage hours this week, which single title is escalated first, and is its
defect the kind one gain change fixes. Those two answers decide which title gets
written into Grafana and whether the repair node runs at all. There is no offline
branch that produces them. If Gemini is unreachable the run raises, because a queue
Interlock did not reason about is a queue an operator should not be handed.

`review` reads the before and after measurements and can only ever WITHHOLD closure.
Closing the record requires two independent gates to pass: `RepairResult.landed`,
which is arithmetic over a second ffmpeg pass, and the reviewer's approval. A model
cannot grant closure on its own, so no amount of confident prose can resolve an alert
over a file that is still out of spec. That asymmetry is the point: the model is
allowed to stop the loop and not to complete it.

TWO CONNECTIONS TO MCP-GRAFANA, AND WHY

The deterministic nodes share one session. mcp-grafana is a child process, and opening
one per node would spawn it five times, split the tool-call rail across five transcripts
and repeat the capability probe's cost. So the orchestrator opens one session, stores it
on a run-scoped context, and `probe`, `file`, `close` and `publish` all use it.
Measurement objects and the live session are not JSON, so they live on the context and
only their serialisable summaries go into workflow state.

The survey node's toolset is ADK's, and ADK owns its connection and its lifecycle. That
is a second child process of the same pinned binary against the same stack. It is worth
the duplication: the alternative is a hand-rolled tool wrapper, and then the claim that
the agent drives the partner's own toolset would not be true. What the model called is
read off the runner's event stream by `_collect_agent_tool_calls` and lands in the run
record next to the deterministic calls, so the two rails are reported separately rather
than merged into one number that hides which side made which call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import catalog, spec
from ..measure import Measurement, measure
from ..repair import RepairResult, repair
from ..grafana import surfaces
from ..grafana.capability import Capabilities, probe as probe_stack
from ..grafana.client import (
    GrafanaRequired,
    GrafanaSession,
    grafana_preflight,
    open_session,
)
from .survey import build_survey_agent, resolve_survey

RECORD_PATH = Path(__file__).resolve().parent.parent.parent / "web" / "run.json"


class GeminiRequired(RuntimeError):
    """Gemini is not reachable, so Interlock will not start.

    Three of the graph's nodes are LlmAgents and all three change what executes: the
    survey node picks the datasources every write goes to, triage picks the title and
    decides whether the repair node runs at all, and review can withhold closure. There
    is no arithmetic that produces those answers, so there is nothing to fall back to.
    A run that quietly skipped them would file a title nobody reasoned about into a
    delivery queue, which is worse than not running.
    """


def require_gemini() -> str:
    """Refuse to build the graph unless Gemini can actually be called.

    `GEMINI_MODEL` alone is not enough. With Vertex AI the SDK also needs a project and
    a location, and without them the failure surfaces halfway through the run as a
    transport error from inside the model call, after ffmpeg has already spent minutes
    measuring. Checking here turns that into one line before any work starts.
    """
    model = os.getenv("GEMINI_MODEL", "").strip()
    if not model:
        raise GeminiRequired(
            "GEMINI_MODEL is not set. Interlock's survey, triage and review decisions "
            "are made by Gemini and there is no offline branch that produces them. Set "
            "GEMINI_MODEL, for example gemini-2.5-flash."
        )

    vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes")
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if vertex:
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLOUD_PROJECT", project),
                ("GOOGLE_CLOUD_LOCATION", location),
            )
            if not value
        ]
        if missing:
            raise GeminiRequired(
                f"GOOGLE_GENAI_USE_VERTEXAI is on but {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set, so the Gemini call has "
                "no endpoint to reach. Interlock stops here rather than measuring eight "
                "features and then failing inside the triage node."
            )
        return model
    if not api_key:
        raise GeminiRequired(
            "no Gemini credential is present. Either set GOOGLE_GENAI_USE_VERTEXAI=true "
            "with GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION, or set GOOGLE_API_KEY. "
            "Interlock has no offline branch for the survey, triage and review decisions."
        )
    return model


# --- what Gemini must return ---------------------------------------------


class TriageDecision(BaseModel):
    """The queue, and the disposition of its head."""

    queue: list[str] = Field(
        description=(
            "title_id values ordered by which should reach a mix stage first. Every "
            "title_id from the input appears exactly once."
        )
    )
    escalate: str = Field(description="the single title_id to file into Grafana this run")
    severity: str = Field(description="one of clear, minor, major, critical")
    auto_repairable: bool = Field(
        description=(
            "whether the escalated title's own defect is one a single gain change "
            "delivers. true only when the integrated loudness is offset from target and "
            "the loudness range is normal. false when the loudness range is wide, the "
            "offset is extreme, or the true peak is already over the ceiling, because "
            "then a single gain change would bury dialogue or clip and a person must "
            "decide."
        )
    )
    repair_target: str = Field(
        default="",
        description=(
            "the highest-ranked title_id in the queue whose defect one loudnorm pass "
            "delivers, so it can be cleared this run without a person. May be the same "
            "as escalate. Empty string when no title in the fleet qualifies."
        )
    )
    disposition: str = Field(
        description=(
            "two or three sentences an operator reads first: what is wrong with this "
            "title, and what happens next. Name the measured figures."
        )
    )
    operator_instruction: str = Field(
        description="one imperative sentence naming the next physical action."
    )


class RepairReview(BaseModel):
    """Whether the repaired file is fit to close the record on."""

    approve: bool = Field(
        description=(
            "true only if the re-measured figures show the title now meets the "
            "integrated loudness target and the true peak ceiling. false if anything "
            "about the repair looks wrong, including a true peak that rose too far or "
            "a loudness range that collapsed."
        )
    )
    reasoning: str = Field(description="two sentences citing the before and after numbers")
    residual_work: str = Field(
        description=(
            "what a human still has to do on this title after the repair, or the "
            "single word none"
        )
    )


# --- run-scoped context ---------------------------------------------------


@dataclass
class RunContext:
    """Everything a run holds that is not JSON."""

    session: GrafanaSession
    caps: Capabilities | None = None
    anchor: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    window_override: int | None = None
    title_ids: list[str] = field(default_factory=list)
    measurements: dict[str, Measurement] = field(default_factory=dict)
    filings: dict[str, surfaces.Filing] = field(default_factory=dict)
    repairs: dict[str, RepairResult] = field(default_factory=dict)
    assertions: list[surfaces.Assertion] = field(default_factory=list)
    fleet_url: str = ""
    survey: dict = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    emitted: list[str] = field(default_factory=list)
    agent_tool_calls: list[dict] = field(default_factory=list)

    def entered(self, node_name: str, kind: str, detail: str) -> None:
        """Record that a node actually ran, and what it did while it was there.

        The graph is a claim until something writes down which of its nodes executed.
        Every node appends here as its last act, so the run record carries its own
        execution trace and a node that was skipped by the branch is visibly absent
        rather than assumed present.
        """
        self.trace.append(
            {
                "node": node_name,
                "kind": kind,
                "at": datetime.now(timezone.utc).isoformat(),
                "mcp_calls_so_far": len(self.session.calls),
                "detail": detail,
            }
        )


_CONTEXT: RunContext | None = None


def context() -> RunContext:
    if _CONTEXT is None:
        raise GrafanaRequired(
            "no Interlock run context. Nodes run inside run_fleet, which owns the "
            "mcp-grafana session for the whole graph."
        )
    return _CONTEXT


# --- workflow state -------------------------------------------------------


class RunState(BaseModel):
    request: dict = Field(default_factory=dict)
    survey: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    fleet: list = Field(default_factory=list)
    triage: dict = Field(default_factory=dict)
    filing: dict = Field(default_factory=dict)
    repair: dict = Field(default_factory=dict)
    review: dict = Field(default_factory=dict)
    outcome: dict = Field(default_factory=dict)


# --- nodes ----------------------------------------------------------------


def _summary_for_model(m: Measurement) -> dict:
    """The evidence the model sees. Numbers only, plus the citations that frame them."""
    breaches = m.breaches()
    return {
        "title_id": m.title_id,
        "title": f"{m.title} ({catalog.BY_ID[m.title_id].year})" if m.title_id in catalog.BY_ID else m.title,
        "window_seconds": m.window_seconds,
        "integrated_lufs": round(m.integrated_lufs, 1),
        "delta_from_target_lu": round(m.integrated_delta_lu, 1),
        "target_lufs": spec.EBU_R128_TARGET_LUFS,
        "tolerance_lu": spec.EBU_R128_TOLERANCE_LU,
        "true_peak_dbtp": None if m.true_peak_dbfs is None else round(m.true_peak_dbfs, 1),
        "true_peak_ceiling_dbtp": spec.TRUE_PEAK_CEILING_DBTP,
        "loudness_range_lu": None if m.lra_lu is None else round(m.lra_lu, 1),
        "availability": round(m.availability, 4),
        "availability_target": spec.AVAILABILITY_TARGET,
        "error_budget_seconds": round(m.budget_seconds, 1),
        "burned_seconds": round(m.burned_seconds, 1),
        "burn_x_budget": None if m.burn_ratio == float("inf") else round(m.burn_ratio, 1),
        "arithmetic_severity": m.severity.name,
        "in_spec": m.in_spec,
        "out_of_corridor_spans": len(breaches),
        "worst_spans": [b.as_dict() for b in breaches[:4]],
        "caption_failures": [f["check"] for f in m.subtitle_findings if not f["passed"]],
    }


async def _probe(ctx, survey: dict) -> dict:
    """Establish what this Grafana stack will actually do, by doing it.

    `survey` is the structured output of the graph's first node, where Gemini
    interrogated the MCP server itself. Its datasource uids are checked against the
    stack before anything is written, and a uid the stack does not have stops the run
    rather than producing a dashboard whose panel renders empty.
    """
    run = context()
    resolved = await resolve_survey(run.session, survey)
    run.survey = resolved
    run.entered(
        "survey",
        "llm",
        "Gemini called "
        + ", ".join(survey.get("tools_called", []) or ["nothing"])
        + f" and chose series datasource {resolved['series_uid']} "
        f"({resolved['series_type']}), alert state history "
        f"{resolved['alert_state_history_uid'] or 'none'}",
    )
    run.caps = await probe_stack(run.session, resolved)
    if run.caps.escalation_path == "none":
        raise GrafanaRequired(
            "this stack accepted no breach-filing write during the probe, so Interlock "
            f"has nothing to execute. {run.caps.escalation_reason}"
        )
    ctx.state["capabilities"] = run.caps.as_dict()
    run.entered(
        "probe",
        "function",
        f"{len(run.caps.items)} capabilities attempted for real, escalation path "
        f"{run.caps.escalation_path}",
    )
    return {"capabilities": run.caps.as_dict()}


async def _measure_fleet(ctx, request: dict) -> dict:
    """ffmpeg over every requested title. No model, no network."""
    run = context()
    rows = []
    for title_id in run.title_ids:
        title = catalog.BY_ID[title_id]
        window = run.window_override or title.window_seconds
        m = measure(
            title.local_video,
            title_id=title.title_id,
            title=title.title,
            seconds=window,
            subtitle_text=catalog.subtitle_text(title),
        )
        run.measurements[title_id] = m
        rows.append(_summary_for_model(m))
    ctx.state["fleet"] = rows
    run.entered(
        "measure_fleet",
        "function",
        f"{len(rows)} titles measured with ffmpeg ebur128, "
        f"{sum(len(m.samples) for m in run.measurements.values())} samples at 100 ms",
    )
    return {
        "spec": {
            "citations": spec.SPEC_CITATIONS,
            "corridor_lufs": list(spec.corridor()),
        },
        "measured_titles": rows,
    }


async def _file(ctx, triage: dict) -> Any:
    """Write the record into Grafana, then route on whether to attempt a repair.

    The branch is taken on `auto_repairable`, a boolean field of the model's
    structured output, so the graph shape stays reproducible while the judgement
    inside it does not have to be.
    """
    from google.adk.events.event import Event

    run = context()
    caps = run.caps
    assert caps is not None

    # The LlmAgent node ran if and only if there is a structured decision here for a
    # function node to consume. Recording it at the point of consumption is stronger
    # evidence than an event author name: it says the model's output was load-bearing.
    run.entered(
        "triage",
        "llm",
        f"Gemini queued {len(triage.get('queue', []))} titles, escalate="
        f"{triage.get('escalate', '')!r}, severity={triage.get('severity', '')!r}, "
        f"auto_repairable={triage.get('auto_repairable')}, "
        f"repair_target={triage.get('repair_target', '')!r}",
    )

    escalate = triage.get("escalate", "")
    if escalate not in run.measurements:
        # The model named a title that was not measured. That is a triage failure, not
        # something to paper over by picking one ourselves: filing the wrong title into
        # a delivery queue is worse than failing loudly.
        raise ValueError(
            f"triage escalated {escalate!r}, which is not in the measured set "
            f"{sorted(run.measurements)}"
        )

    target = triage.get("repair_target", "") or ""
    if target and target not in run.measurements:
        raise ValueError(
            f"triage nominated {target!r} for repair, which is not in the measured set "
            f"{sorted(run.measurements)}"
        )

    # File the escalated title, and the repair target too when it is a different one.
    # Both are real breaches on the board; the difference is only which of them a
    # person has to touch. A delivery lead escalates the worst and clears the cheapest
    # win in the same pass, and this is that behaviour.
    to_file = [escalate] + ([target] if target and target != escalate else [])
    for title_id in to_file:
        m = run.measurements[title_id]
        severity = (
            spec.SEVERITIES.get(triage.get("severity", ""), m.severity)
            if title_id == escalate
            else m.severity
        )
        breaches = m.breaches()[:12]
        filing = await surfaces.publish_title(
            run.session,
            caps,
            m,
            run.anchor,
            gemini_summary=triage.get("disposition", ""),
            severity=severity,
            breaches=[] if m.in_spec else breaches,
        )
        run.filings[title_id] = filing
        run.assertions.extend(
            await surfaces.verify(
                run.session, caps, filing, m, expected_breaches=0 if m.in_spec else len(breaches)
            )
        )
        if filing.rule_uid:
            run.assertions.append(
                await surfaces.set_group_interval(
                    run.session, caps.folder_uid, surfaces.RULE_GROUP
                )
            )
            # An accepted create is not a firing alert. Wait for the ruler to agree
            # with the measurement before calling this title filed.
            run.assertions.append(
                await surfaces.await_rule_state(run.session, filing.rule_uid, "firing")
            )
            run.assertions.append(
                await surfaces.alert_state_history(run.session, caps, filing.rule_uid)
            )

    head = run.filings[escalate]
    ctx.state["filing"] = head.as_dict()

    repairable = bool(target) and not run.measurements[target].in_spec
    route = "repair" if repairable else "hold"
    run.entered(
        "file",
        "function",
        f"filed {to_file} into Grafana, routed to {route}",
    )
    yield Event(
        output={
            "filed": [run.filings[t].as_dict() for t in to_file],
            "repair_target": target if repairable else "",
            "route": route,
        },
        route=route,
    )


async def _repair(ctx, triage: dict) -> dict:
    """Change the file. Two ffmpeg passes, then a third that measures the result."""
    run = context()
    title_id = triage.get("repair_target") or ""
    if title_id not in run.measurements:
        raise ValueError(f"repair reached with no measured target: {title_id!r}")
    before = run.measurements[title_id]
    result = repair(before, title_id=title_id, title=before.title)
    run.repairs[title_id] = result
    ctx.state["repair"] = result.as_dict()
    run.entered(
        "repair",
        "function",
        f"loudnorm two-pass on {title_id}, re-measured "
        f"{result.after.integrated_lufs:.1f} LUFS, landed={result.landed}",
    )
    before_lra = result.before.lra_lu or 0.0
    after_lra = result.after.lra_lu or 0.0
    return {
        "repair": result.as_dict(),
        "checks_for_the_reviewer": {
            "true_peak_after_dbtp": result.after.true_peak_dbfs,
            "true_peak_ceiling_dbtp": spec.TRUE_PEAK_CEILING_DBTP,
            "true_peak_headroom_db": (
                None
                if result.after.true_peak_dbfs is None
                else round(spec.TRUE_PEAK_CEILING_DBTP - result.after.true_peak_dbfs, 1)
            ),
            "loudness_range_before_lu": round(before_lra, 1),
            "loudness_range_after_lu": round(after_lra, 1),
            "loudness_range_kept_fraction": (
                None if before_lra <= 0 else round(after_lra / before_lra, 2)
            ),
            "availability_before": round(result.before.availability, 4),
            "availability_after": round(result.after.availability, 4),
            "availability_target": spec.AVAILABILITY_TARGET,
            "availability_went_down": result.after.availability < result.before.availability,
            "availability_was_already_at_target": (
                result.before.availability >= spec.AVAILABILITY_TARGET
            ),
        },
        "gate": {
            "landed_by_measurement": result.landed,
            "blocked_by": result.blocked_by,
            "not_measured": result.after.unmeasured,
            "note": (
                "landed_by_measurement is arithmetic over a second ffmpeg pass on the "
                "written file. Your approval is a separate gate and cannot override it. "
                "Anything under not_measured is a criterion this pass could not evaluate, "
                "which is never a pass."
            ),
        },
    }


async def _close(ctx, repair: dict, review: dict) -> dict:
    """Resolve the record, but only when both gates pass."""
    run = context()
    caps = run.caps
    assert caps is not None

    title_id = repair["title_id"]
    result = run.repairs[title_id]
    filing_obj = run.filings[title_id]

    landed = bool(result.landed)
    approved = bool(review.get("approve"))
    run.entered(
        "review",
        "llm",
        f"Gemini approve={approved}, residual_work="
        f"{(review.get('residual_work') or '')[:60]!r}",
    )
    closing = landed and approved
    if closing:
        withheld = ""
    elif not landed:
        withheld = "the measurement gate failed: " + "; ".join(result.blocked_by)
    else:
        withheld = (
            "the measurement gate passed but the reviewer withheld approval: "
            f"{review.get('reasoning', 'no reason given')}"
        )

    assertions = await surfaces.close_out(
        run.session,
        caps,
        filing_obj,
        result.after,
        run.anchor,
        repair_note=result.note,
        landed=closing,
        withheld_because=withheld,
    )
    run.assertions.extend(assertions)

    if filing_obj.rule_uid and closing:
        # The rule now holds the re-measured figure. Watch it clear, rather than
        # assuming the update did it.
        run.assertions.append(
            await surfaces.await_rule_state(run.session, filing_obj.rule_uid, "inactive")
        )
    if filing_obj.rule_uid:
        run.assertions.append(
            await surfaces.alert_state_history(run.session, caps, filing_obj.rule_uid)
        )

    outcome = {
        "title_id": title_id,
        "closed": closing,
        "landed_by_measurement": landed,
        "approved_by_review": approved,
        "gate": "both gates passed" if closing else withheld,
        "integrated_lufs_before": round(result.before.integrated_lufs, 1),
        "integrated_lufs_after": round(result.after.integrated_lufs, 1),
        "availability_before": round(result.before.availability, 4),
        "availability_after": round(result.after.availability, 4),
        "blocked_by": result.blocked_by,
        "not_measured_after_repair": result.after.unmeasured,
        "residual_work": review.get("residual_work", ""),
        "repaired_file": str(result.output),
    }
    ctx.state["outcome"] = outcome
    run.entered(
        "close",
        "function",
        f"closed={closing} on the re-measurement, gate: {outcome['gate']}",
    )
    return outcome


async def _publish(ctx, capabilities: dict) -> dict:
    """The fleet board, then the run record the web surface reads."""
    run = context()
    caps = run.caps
    assert caps is not None

    measurements = [run.measurements[t] for t in run.title_ids if t in run.measurements]
    run.fleet_url = await surfaces.publish_fleet(run.session, caps, measurements, run.filings)

    record = surfaces.run_record(
        caps, measurements, run.filings, run.assertions, run.fleet_url, run.session
    )
    record["triage"] = ctx.state.get("triage", {})
    record["review"] = ctx.state.get("review", {})
    record["outcome"] = ctx.state.get("outcome", {})
    record["repair"] = ctx.state.get("repair", {})
    record["catalog"] = [catalog.BY_ID[t].as_dict() for t in run.title_ids if t in catalog.BY_ID]
    # The series for the titles this run actually acted on, so Interlock's own surface
    # draws the same decimated samples the Grafana panel query carries.
    record["series"] = {
        title_id: {
            "before": surfaces.series_rows(run.measurements[title_id]),
            "after": (
                surfaces.series_rows(run.repairs[title_id].after)
                if title_id in run.repairs
                else None
            ),
            "breaches": [b.as_dict() for b in run.measurements[title_id].breaches()[:12]],
            "corridor": list(spec.corridor()),
        }
        for title_id in run.filings
    }
    record["gemini_model"] = os.getenv("GEMINI_MODEL", "")
    record["stack_survey"] = run.survey
    run.entered("publish", "function", f"fleet board written, {len(run.assertions)} assertions")
    record["node_trace"] = list(run.trace)
    record["agent_tool_calls"] = list(run.agent_tool_calls)
    record["spec"] = {
        "citations": spec.SPEC_CITATIONS,
        "corridor_lufs": list(spec.corridor()),
        "target_lufs": spec.EBU_R128_TARGET_LUFS,
        "tolerance_lu": spec.EBU_R128_TOLERANCE_LU,
        "availability_target": spec.AVAILABILITY_TARGET,
    }
    surfaces.write_run_record(record, RECORD_PATH)
    return {"fleet_dashboard_url": run.fleet_url, "assertions": len(run.assertions)}


# --- the graph ------------------------------------------------------------

TRIAGE_INSTRUCTION = """\
You are the delivery QC lead at a film restoration house. Every title in the input has
already been measured by ffmpeg; the figures are facts and you must not restate them
wrongly or invent any.

The delivery spec is an SLO. Integrated loudness must sit within the stated tolerance
of the target. True peak must stay under the ceiling. Availability is the fraction of
gated programme time inside the loudness corridor, and burn_x_budget is how many times
over its error budget the title has gone.

Your two decisions change what happens next, so make them on the evidence:

1. `escalate`: which single title_id reaches the mix stage first. Rank by delivery
   risk, not by how bad a number looks. A title one gain change away from passing is
   cheap to clear; a title needing a human is expensive and blocks a bay.
2. `auto_repairable`: true only when the defect is a flat offset a single loudnorm
   pass corrects, meaning the integrated loudness is off target while the loudness
   range is unremarkable. Say false when the loudness range is wide, when the offset is
   extreme, or when the true peak is already over the ceiling, because a gain change
   then buries dialogue or clips, and a person must make that call.

3. `repair_target`: the highest-ranked title_id in the queue that one loudnorm pass
   would actually deliver, so it can be cleared this run without occupying a person.
   This may be the same title as `escalate`, and it must be the empty string when no
   title in the fleet qualifies. Do not nominate a title that already passes.

`queue` must contain every title_id from the input exactly once.
Write `disposition` and `operator_instruction` for a delivery operator who has never
opened a metrics dashboard. Cite the measured numbers. No em dashes.
"""

REVIEW_INSTRUCTION = """\
You are reviewing a repair that has already been applied and re-measured. Your job is
to withhold approval when something looks wrong, and nothing more: approval alone does
not close the record. A separate arithmetic gate has already checked whether the
re-measured integrated loudness is inside tolerance, and that gate cannot be
overridden by you.

Withhold approval only for one of these specific reasons, each of which is a real way
a loudnorm pass can make a master worse:

  the after true peak is over the ceiling, or within 0.5 dB of it
  the after loudness range is less than half the before loudness range, which means
    the dynamics were flattened rather than the level corrected
  availability went DOWN, which means the pass pushed programme audio out of the
    corridor that used to be inside it

Otherwise approve. In particular, availability that did not change is not a reason to
withhold when it was already at or above the SLO target: a title whose only defect was
a level offset has nothing left for availability to gain, and refusing it there would
block every clean repair. Do not invent a reason outside the three above.

`residual_work` names what a person still has to do on this title, or the single word
none. Cite the before and after numbers. No em dashes.
"""


def build_workflow():
    from google.adk.agents import LlmAgent
    from google.adk.workflow import START, RetryConfig, Workflow, node

    model = require_gemini()

    # Retries go on the read-only and idempotent nodes only. `file` and `close` post
    # annotations, and re-running one after a partial failure would file the same
    # breach span twice, so their transient failures are absorbed inside the MCP client
    # around the single throttled call instead. A retry that duplicates the record is
    # not resilience.
    idempotent_retry = RetryConfig(max_attempts=3, initial_delay=2.0, backoff_factor=2.0)

    survey_agent = build_survey_agent(model)
    probe_node = node(_probe, name="probe", timeout=240.0)
    measure_node = node(_measure_fleet, name="measure_fleet", timeout=1800.0)
    file_node = node(_file, name="file", timeout=900.0)
    repair_node = node(_repair, name="repair", timeout=1800.0)
    close_node = node(_close, name="close", timeout=900.0)
    publish_node = node(_publish, name="publish", timeout=600.0, retry_config=idempotent_retry)

    triage = LlmAgent(
        name="triage",
        model=model,
        description="Ranks the measured fleet and decides the disposition of its head.",
        instruction=TRIAGE_INSTRUCTION,
        output_schema=TriageDecision,
        output_key="triage",
    )
    review = LlmAgent(
        name="review",
        model=model,
        description="Can withhold closure of a repaired title. Cannot grant it.",
        instruction=REVIEW_INSTRUCTION,
        output_schema=RepairReview,
        output_key="review",
    )

    return Workflow(
        name="interlock",
        description="Measures an archive against a delivery SLO and files the breach in Grafana.",
        state_schema=RunState,
        edges=[
            (START, survey_agent, probe_node, measure_node, triage, file_node),
            (file_node, {"repair": repair_node, "hold": publish_node}),
            (repair_node, review, close_node, publish_node),
        ],
    )


def _collect_agent_tool_calls(sink: list[dict], event) -> None:
    """Record what an LlmAgent asked the Grafana toolset for, from the runner's events.

    The survey node's tools are an ADK `McpToolset`, which holds its own connection to
    mcp-grafana, so those calls do not appear in Interlock's own call log the way the
    deterministic nodes' calls do. Reading them off the event stream is the better source
    anyway: it is the runner's account of what the model did, not the model's account of
    itself, and the two are compared in the run record.
    """
    content = getattr(event, "content", None)
    for part in getattr(content, "parts", None) or []:
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None):
            sink.append(
                {
                    "node": event.author,
                    "tool": call.name,
                    "argument_keys": sorted((getattr(call, "args", None) or {}).keys()),
                    "direction": "call",
                }
            )
            continue
        response = getattr(part, "function_response", None)
        if response is not None and getattr(response, "name", None):
            payload = getattr(response, "response", None)
            sink.append(
                {
                    "node": event.author,
                    "tool": response.name,
                    "response_chars": len(str(payload)) if payload is not None else 0,
                    "direction": "response",
                }
            )


# --- entry point ----------------------------------------------------------


async def run_fleet(
    title_ids: list[str] | None = None,
    *,
    window_seconds: int | None = None,
) -> dict:
    """Open one mcp-grafana session and run the graph over it."""
    global _CONTEXT

    from google.adk.runners import InMemoryRunner
    from google.genai import types

    # Both credentials before any work. The Grafana check is deliberately first: a run
    # with no Grafana token has nowhere to file a breach, so there is no output to
    # produce and measuring eight features first would be minutes spent on nothing.
    grafana_preflight()
    require_gemini()

    requested = title_ids or [t.title_id for t in catalog.FLEET]
    unknown = [t for t in requested if t not in catalog.BY_ID]
    if unknown:
        raise ValueError(f"unknown title_ids: {unknown}")
    missing = [t for t in requested if not catalog.BY_ID[t].local_video.exists()]
    if missing:
        raise RuntimeError(
            f"these titles are not downloaded yet: {missing}. "
            "Run `python -m interlock.catalog` to fetch the fleet from archive.org."
        )

    workflow = build_workflow()

    async with open_session() as session:
        run = RunContext(session=session, window_override=window_seconds, title_ids=requested)
        _CONTEXT = run
        try:
            runner = InMemoryRunner(node=workflow, app_name="interlock")
            state = {
                "request": {
                    "title_ids": requested,
                    "window_seconds": window_seconds,
                    "started_at": run.anchor.isoformat(),
                }
            }
            created = await runner.session_service.create_session(
                app_name="interlock", user_id="operator", state=state
            )
            events: list[dict] = []
            async for event in runner.run_async(
                user_id="operator",
                session_id=created.id,
                new_message=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=(
                                "Measure this archive against the delivery SLO, file the "
                                "worst breach in Grafana, repair it if one gain change "
                                "will deliver it, and close the record only on the "
                                "re-measurement."
                            )
                        )
                    ],
                ),
            ):
                _collect_agent_tool_calls(run.agent_tool_calls, event)
                if event.output is not None:
                    events.append({"author": event.author, "output": event.output})
                    # The runner's own view of which node emitted, kept beside the
                    # nodes' self-reports so the two can be compared rather than
                    # taken on trust.
                    if not run.emitted or run.emitted[-1] != event.author:
                        run.emitted.append(event.author)
            final = await runner.session_service.get_session(
                app_name="interlock", user_id="operator", session_id=created.id
            )
            return {
                "state": dict(final.state),
                "events": len(events),
                "node_trace": list(run.trace),
                "nodes_that_emitted": list(run.emitted),
                "agent_tool_calls": list(run.agent_tool_calls),
                "mcp_calls": len(session.calls),
                "fleet_dashboard_url": run.fleet_url,
                "record_path": str(RECORD_PATH),
            }
        finally:
            _CONTEXT = None
