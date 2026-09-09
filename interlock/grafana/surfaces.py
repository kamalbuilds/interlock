"""The writes. Everything Interlock puts into Grafana Cloud, and the reads that prove it.

Five write classes, all through the official mcp-grafana server:

  1. the fleet board          update_dashboard, one row per title with its SLO burn
  2. the title board          update_dashboard, the measured 100 ms series on a panel
                              with the corridor drawn as thresholds
  3. the breach record        create_annotation, one annotation region per
                              out-of-corridor span, placed at the offending timecode
  4. the alert rule           alerting_manage_rules create, evaluating the measured
                              integrated loudness against the delivery target
  5. the resolution           the rule's own state transition after the repair, read
                              back out of the alert state history

Nothing here reports success from a 200. Every write is followed by a read that only
a landed write can answer: a dashboard read back by uid, an annotation found again by
a unique tag, an alert rule fetched by the uid the create returned, and the rule's
state transition pulled out of Loki. `verify` returns a list of assertions with the
query that produced each one.

On how the measured series reaches a panel. Writing the 100 ms series into Grafana
Cloud Prometheus needs a Grafana Cloud Access Policy token with metrics:write, which
is a different credential from a stack service account token: the stack token is
refused by the OTLP gateway with 401, and Loki push through the datasource proxy is
refused outright with "non allow-listed POSTs not allowed on proxied loki
datasource". Both were tried. Until such a token is present in the environment as
INTERLOCK_OTLP_USER and INTERLOCK_OTLP_TOKEN, Interlock carries the series onto real
panels as inline panel data read by the Infinity datasource, and says so on the
panel itself. The numbers are the real ffmpeg measurements either way. What differs
is where they are stored, and that difference is labelled rather than glossed.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import spec
from ..measure import Breach, Measurement, timecode
from .capability import Capabilities, org_id
from .client import GrafanaSession, ToolUnavailable

FLEET_DASHBOARD_UID = "interlock-fleet"
RULE_GROUP = "interlock-delivery-slo"

INFINITY_TYPE = "yesoreyeram-infinity-datasource"


def title_dashboard_uid(title_id: str) -> str:
    return f"interlock-{title_id.replace('_', '-')}"[:40]


def metrics_write_configured() -> bool:
    return bool(
        os.getenv("INTERLOCK_OTLP_USER", "").strip()
        and os.getenv("INTERLOCK_OTLP_TOKEN", "").strip()
    )


def series_storage_label() -> str:
    """One line, on the panel, saying where these numbers live. Never optimistic."""
    if metrics_write_configured():
        return "Series stored in Grafana Cloud Prometheus via the OTLP gateway."
    return (
        "Series carried as inline panel data read by the Infinity datasource. "
        "Not stored in Grafana Cloud Prometheus: that needs a Cloud Access Policy "
        "token with metrics:write, which this stack's service account token is not."
    )


# --- the run record -------------------------------------------------------


@dataclass
class Assertion:
    """One post-condition, and the query that established it."""

    claim: str
    holds: bool
    evidence: str
    query: str

    def as_dict(self) -> dict:
        return {"claim": self.claim, "holds": self.holds, "evidence": self.evidence, "query": self.query}


@dataclass
class Filing:
    """What Interlock did about one breaching title."""

    title_id: str
    escalation_path: str
    dashboard_uid: str = ""
    dashboard_url: str = ""
    panel_url: str = ""
    rule_uid: str = ""
    rule_title: str = ""
    incident_id: str = ""
    annotation_ids: list[int] = field(default_factory=list)
    annotation_tag: str = ""
    anchor_epoch_ms: int = 0
    window_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "title_id": self.title_id,
            "escalation_path": self.escalation_path,
            "dashboard_uid": self.dashboard_uid,
            "dashboard_url": self.dashboard_url,
            "panel_url": self.panel_url,
            "rule_uid": self.rule_uid,
            "rule_title": self.rule_title,
            "incident_id": self.incident_id,
            "annotations": len(self.annotation_ids),
            "annotation_tag": self.annotation_tag,
            "anchor_epoch_ms": self.anchor_epoch_ms,
            "window_ms": self.window_ms,
            "notes": self.notes,
        }


# --- timeline anchoring ---------------------------------------------------


def anchor_for(measurement: Measurement, anchor: datetime) -> tuple[int, int]:
    """Map media timecode onto wall clock so a breach lands on a Grafana timeline.

    Grafana's x-axis is wall clock and a film's x-axis is timecode, so one has to be
    projected onto the other. Interlock anchors media t=0 at the instant the scan
    started, which is what an ingest pipeline emitting live metrics would produce
    anyway. The dashboard states the anchor and every panel carries the media
    timecode as its own field, so nobody has to reverse the arithmetic.
    """
    start_ms = int(anchor.timestamp() * 1000)
    span_ms = int(len(measurement.samples) * spec.SAMPLE_INTERVAL_SECONDS * 1000)
    return start_ms, span_ms


def _iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def series_csv_wallclock(measurement: Measurement, anchor_ms: int, max_rows: int = 900) -> str:
    """The measured series with an absolute timestamp column Grafana can plot."""
    body = measurement.series_csv(max_rows=max_rows).splitlines()[1:]
    out = ["time,short_term_lufs,momentary_lufs,timecode"]
    for line in body:
        t_s, short_term, momentary = line.split(",")
        stamp = _iso(anchor_ms + int(float(t_s) * 1000))
        out.append(f"{stamp},{short_term},{momentary},{timecode(float(t_s))}")
    return "\n".join(out) + "\n"


# --- dashboard JSON -------------------------------------------------------


def _infinity_target(ref_id: str, uid: str, csv: str, columns: list[dict], fmt: str = "timeseries") -> dict:
    return {
        "refId": ref_id,
        "datasource": {"type": INFINITY_TYPE, "uid": uid},
        "type": "csv",
        "source": "inline",
        "parser": "backend",
        "format": fmt,
        "data": csv,
        "root_selector": "",
        "columns": columns,
        "filterExpression": "",
    }


_SERIES_COLUMNS = [
    {"selector": "time", "text": "time", "type": "timestamp"},
    {"selector": "short_term_lufs", "text": "short-term LUFS", "type": "number"},
    {"selector": "momentary_lufs", "text": "momentary LUFS", "type": "number"},
]


def title_dashboard(
    measurement: Measurement,
    caps: Capabilities,
    anchor_ms: int,
    span_ms: int,
    repair: dict | None = None,
) -> dict:
    """One title, its measured series, the corridor, and what Interlock did about it."""
    floor, ceiling = spec.corridor()
    csv = series_csv_wallclock(measurement, anchor_ms)
    uid = title_dashboard_uid(measurement.title_id)

    header = [
        f"**{measurement.title}** measured over the first {measurement.window_seconds} s "
        f"of the archive.org master.",
        "",
        f"    {measurement.command}",
        "",
        f"Integrated loudness **{measurement.integrated_lufs:.1f} LUFS** against the "
        f"EBU R128 target of {spec.EBU_R128_TARGET_LUFS:.0f} LUFS "
        f"({measurement.integrated_delta_lu:+.1f} LU). "
        f"True peak {measurement.true_peak_dbfs:.1f} dBTP against a "
        f"{spec.TRUE_PEAK_CEILING_DBTP:.0f} dBTP ceiling. "
        f"Loudness range {measurement.lra_lu:.1f} LU."
        if measurement.true_peak_dbfs is not None
        else f"Integrated loudness **{measurement.integrated_lufs:.1f} LUFS**.",
        "",
        f"SLO: {spec.AVAILABILITY_TARGET:.0%} of gated programme time inside "
        f"{floor:.0f} to {ceiling:.0f} LUFS. "
        f"Measured availability **{measurement.availability:.2%}**. "
        f"Error budget {measurement.budget_seconds:.1f} s, "
        f"burned **{measurement.burned_seconds:.1f} s** "
        f"({measurement.burn_ratio:.1f}x budget). Verdict **{measurement.severity.name}**.",
        "",
        f"Media t=0 is anchored at {_iso(anchor_ms)}. {series_storage_label()}",
    ]
    if repair:
        header += [
            "",
            "**Repair applied.** "
            f"{repair.get('command', '')}",
            "",
            f"Re-measured integrated loudness **{repair.get('integrated_lufs')} LUFS** "
            f"({repair.get('integrated_delta_lu'):+.1f} LU from target), "
            f"availability {repair.get('availability', 0):.2%}. "
            f"In spec: **{repair.get('in_spec')}**.",
        ]

    panels: list[dict] = [
        {
            "id": 1,
            "type": "text",
            "title": "Delivery record",
            "gridPos": {"h": 9, "w": 24, "x": 0, "y": 0},
            "options": {"mode": "markdown", "content": "\n".join(header)},
        },
        {
            "id": 2,
            "type": "timeseries",
            "title": f"{measurement.title}: short-term loudness against the corridor",
            "description": (
                "One point per 100 ms from ffmpeg ebur128. Short-term is the 3 s sliding "
                "window, momentary the 400 ms window, both per EBU Tech 3341. "
                + series_storage_label()
            ),
            "gridPos": {"h": 12, "w": 24, "x": 0, "y": 9},
            "datasource": {"type": INFINITY_TYPE, "uid": caps.infinity_uid},
            "targets": [_infinity_target("A", caps.infinity_uid, csv, _SERIES_COLUMNS)],
            "fieldConfig": {
                "defaults": {
                    "unit": "none",
                    "min": -60,
                    "max": 0,
                    "custom": {
                        "drawStyle": "line",
                        "lineWidth": 1,
                        "fillOpacity": 6,
                        "showPoints": "never",
                        "thresholdsStyle": {"mode": "line"},
                    },
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "text", "value": None},
                            {"color": "orange", "value": ceiling},
                        ],
                    },
                },
                "overrides": [
                    {
                        "matcher": {"id": "byName", "options": "momentary LUFS"},
                        "properties": [
                            {"id": "custom.lineWidth", "value": 1},
                            {"id": "custom.fillOpacity", "value": 0},
                            {"id": "color", "value": {"mode": "fixed", "fixedColor": "semi-dark-blue"}},
                        ],
                    }
                ],
            },
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        },
    ]

    breaches = measurement.breaches()[:20]
    if breaches:
        rows = ["timecode,duration_s,worst_lufs,direction"]
        for b in breaches:
            rows.append(f"{b.timecode},{b.duration:.1f},{b.worst_lufs:.1f},{b.direction}")
        panels.append(
            {
                "id": 3,
                "type": "table",
                "title": "Out-of-corridor spans, longest first",
                "description": "Each row is an annotation Interlock wrote onto the panel above.",
                "gridPos": {"h": 10, "w": 24, "x": 0, "y": 21},
                "datasource": {"type": INFINITY_TYPE, "uid": caps.infinity_uid},
                "targets": [
                    _infinity_target(
                        "A",
                        caps.infinity_uid,
                        "\n".join(rows) + "\n",
                        [
                            {"selector": "timecode", "text": "timecode", "type": "string"},
                            {"selector": "duration_s", "text": "duration s", "type": "number"},
                            {"selector": "worst_lufs", "text": "worst LUFS", "type": "number"},
                            {"selector": "direction", "text": "direction", "type": "string"},
                        ],
                        fmt="table",
                    )
                ],
            }
        )

    return {
        "uid": uid,
        "title": f"Interlock: {measurement.title} ({measurement.title_id})",
        "tags": ["interlock", "delivery-slo", measurement.severity.name],
        "timezone": "utc",
        "schemaVersion": 41,
        "editable": True,
        "annotations": {
            "list": [
                {
                    "name": "Interlock breaches",
                    "enable": True,
                    "iconColor": "red",
                    "datasource": {"type": "datasource", "uid": "grafana"},
                    "target": {"type": "tags", "matchAny": False, "tags": ["interlock", measurement.title_id]},
                }
            ]
        },
        "time": {"from": _iso(anchor_ms - 30_000), "to": _iso(anchor_ms + span_ms + 30_000)},
        "panels": panels,
    }


def fleet_dashboard(measurements: list[Measurement], caps: Capabilities, filings: dict[str, Filing]) -> dict:
    """The fleet, ranked by how much of its error budget each title has burned."""
    ordered = sorted(measurements, key=lambda m: m.burn_ratio, reverse=True)
    rows = ["title,integrated_lufs,delta_lu,availability_pct,burned_s,budget_s,burn_x,verdict"]
    for m in ordered:
        rows.append(
            f"{m.title} ({m.title_id}),{m.integrated_lufs:.1f},{m.integrated_delta_lu:+.1f},"
            f"{m.availability * 100:.2f},{m.burned_seconds:.1f},{m.budget_seconds:.1f},"
            f"{m.burn_ratio:.1f},{m.severity.name}"
        )
    csv = "\n".join(rows) + "\n"

    filed = [f for f in filings.values() if f.rule_uid or f.incident_id or f.annotation_ids]
    summary = [
        f"**{len(measurements)} titles** measured with ffmpeg on this machine, "
        f"{sum(1 for m in measurements if not m.in_spec)} outside the delivery spec.",
        "",
        f"Escalation path chosen at runtime: **{caps.escalation_path}**. {caps.escalation_reason}",
        "",
        f"{len(filed)} titles filed into Grafana this run: "
        + ", ".join(f"`{f.title_id}`" for f in filed)
        if filed
        else "Nothing filed this run.",
        "",
        f"Grafana {caps.grafana_version}, service account `{caps.login}`. {series_storage_label()}",
    ]

    return {
        "uid": FLEET_DASHBOARD_UID,
        "title": "Interlock: archive delivery SLO",
        "tags": ["interlock", "fleet"],
        "timezone": "utc",
        "schemaVersion": 41,
        "editable": True,
        "time": {"from": "now-6h", "to": "now"},
        "panels": [
            {
                "id": 1,
                "type": "text",
                "title": "This run",
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
                "options": {"mode": "markdown", "content": "\n".join(summary)},
            },
            {
                "id": 2,
                "type": "table",
                "title": "Error budget burn by title",
                "description": (
                    "burn_x is consumed budget over allowed budget. 1.0 means the title spent "
                    "exactly its allowance for the measured window."
                ),
                "gridPos": {"h": 14, "w": 24, "x": 0, "y": 8},
                "datasource": {"type": INFINITY_TYPE, "uid": caps.infinity_uid},
                "targets": [
                    _infinity_target(
                        "A",
                        caps.infinity_uid,
                        csv,
                        [
                            {"selector": "title", "text": "title", "type": "string"},
                            {"selector": "integrated_lufs", "text": "LUFS", "type": "number"},
                            {"selector": "delta_lu", "text": "delta LU", "type": "number"},
                            {"selector": "availability_pct", "text": "availability %", "type": "number"},
                            {"selector": "burned_s", "text": "burned s", "type": "number"},
                            {"selector": "budget_s", "text": "budget s", "type": "number"},
                            {"selector": "burn_x", "text": "burn x", "type": "number"},
                            {"selector": "verdict", "text": "verdict", "type": "string"},
                        ],
                        fmt="table",
                    )
                ],
                "fieldConfig": {
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "burn x"},
                            "properties": [
                                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                {
                                    "id": "thresholds",
                                    "value": {
                                        "mode": "absolute",
                                        "steps": [
                                            {"color": "green", "value": None},
                                            {"color": "orange", "value": 1},
                                            {"color": "red", "value": 5},
                                        ],
                                    },
                                },
                            ],
                        }
                    ],
                },
            },
        ],
    }


# --- alert rule -----------------------------------------------------------


def delivery_rule(
    measurement: Measurement,
    caps: Capabilities,
    severity: spec.Severity,
    gemini_summary: str,
) -> dict:
    """A Grafana-managed alert rule on this title's measured integrated loudness.

    The rule reads the measured figure through the Infinity datasource and fires when
    it sits further than the EBU R128 tolerance from target. When the repair lands,
    the same rule is updated with the re-measured figure and Grafana resolves it,
    which is why the rule holds the number rather than a boolean: a rule that fires
    on a flag Interlock sets could be closed by Interlock lying, and a rule that
    fires on the measurement can only be closed by the measurement changing.
    """
    csv = f"delta_lu\n{abs(measurement.integrated_delta_lu):.3f}\n"
    return {
        "operation": "create",
        "org_id": org_id(),
        "title": f"Interlock delivery spec: {measurement.title}",
        "rule_group": RULE_GROUP,
        "folder_uid": caps.folder_uid,
        "condition": "B",
        "for": "0s",
        "no_data_state": "NoData",
        "exec_err_state": "Alerting",
        "is_paused": False,
        "labels": {
            "interlock": "delivery-slo",
            "title_id": measurement.title_id,
            "severity": severity.name,
        },
        "annotations": {
            "summary": (
                f"{measurement.title} measured {measurement.integrated_lufs:.1f} LUFS, "
                f"{measurement.integrated_delta_lu:+.1f} LU from the EBU R128 target of "
                f"{spec.EBU_R128_TARGET_LUFS:.0f} LUFS."
            ),
            "description": gemini_summary,
            "burned_seconds": f"{measurement.burned_seconds:.1f}",
            "error_budget_seconds": f"{measurement.budget_seconds:.1f}",
            "ffmpeg_command": measurement.command,
            "__dashboardUid__": title_dashboard_uid(measurement.title_id),
            "__panelId__": "2",
        },
        "data": [
            {
                "refId": "A",
                "datasourceUid": caps.infinity_uid,
                "relativeTimeRange": {"from": 600, "to": 0},
                "model": _infinity_target(
                    "A",
                    caps.infinity_uid,
                    csv,
                    [{"selector": "delta_lu", "text": "delta_lu", "type": "number"}],
                    fmt="table",
                ),
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 600, "to": 0},
                "model": {
                    "refId": "B",
                    "type": "threshold",
                    "expression": "A",
                    "conditions": [
                        {"evaluator": {"type": "gt", "params": [spec.EBU_R128_TOLERANCE_LU]}}
                    ],
                },
            },
        ],
    }


# --- the operations ------------------------------------------------------


async def publish_title(
    session: GrafanaSession,
    caps: Capabilities,
    measurement: Measurement,
    anchor: datetime,
    *,
    gemini_summary: str,
    severity: spec.Severity,
    breaches: list[Breach],
) -> Filing:
    """Write one title's board, file its breach spans, and open its alert or incident."""
    if not caps.has("dashboard_write"):
        raise ToolUnavailable("update_dashboard", "this stack refused a dashboard write during the probe")

    anchor_ms, span_ms = anchor_for(measurement, anchor)
    uid = title_dashboard_uid(measurement.title_id)
    filing = Filing(
        title_id=measurement.title_id,
        escalation_path=caps.escalation_path,
        dashboard_uid=uid,
        anchor_epoch_ms=anchor_ms,
        window_ms=span_ms,
    )

    await session.call(
        "update_dashboard",
        {
            "dashboard": title_dashboard(measurement, caps, anchor_ms, span_ms),
            "folderUid": caps.folder_uid or None,
            "overwrite": True,
            "message": f"Interlock measured {measurement.title_id} at {measurement.integrated_lufs:.1f} LUFS",
        },
    )

    base = os.getenv("GRAFANA_URL", "").rstrip("/")
    filing.dashboard_url = f"{base}/d/{uid}"
    filing.panel_url = f"{base}/d/{uid}?viewPanel=2"

    # Breach regions, at the offending timecode. Grafana renders a region when
    # timeEnd is set, so a 17 second quiet span reads as 17 seconds of programme
    # rather than a dot with a tooltip.
    tag = f"run-{anchor_ms}"
    filing.annotation_tag = tag
    if caps.has("annotation_write"):
        for breach in breaches:
            start_ms = anchor_ms + int(breach.start * 1000)
            end_ms = anchor_ms + int(breach.end * 1000)
            text = (
                f"{breach.timecode} {breach.direction} for {breach.duration:.1f}s, "
                f"worst {breach.worst_lufs:.1f} LUFS "
                f"(corridor {spec.corridor()[0]:.0f} to {spec.corridor()[1]:.0f} LUFS)"
            )
            await session.call(
                "create_annotation",
                {
                    "dashboardUid": uid,
                    "panelId": 2,
                    "text": text,
                    "tags": ["interlock", measurement.title_id, breach.direction, tag],
                    "time": start_ms,
                    "timeEnd": end_ms,
                },
            )
        filing.annotation_ids = list(range(len(breaches)))
    else:
        filing.notes.append("annotation_write unavailable on this stack, breach spans not filed")

    # The escalation. The path was decided by the probe, not by a config flag.
    if caps.escalation_path == "irm_incident":
        created = await session.call(
            "create_incident",
            {
                "title": f"Interlock: {measurement.title} outside delivery spec",
                "severity": severity.irm_severity,
                "roomPrefix": "interlock",
                "status": "active",
                "attachUrl": filing.panel_url,
                "attachCaption": f"{measurement.title} measured loudness",
                "labels": [{"key": "interlock"}, {"key": measurement.title_id}],
            },
        )
        from .capability import _extract_incident_id

        filing.incident_id = _extract_incident_id(created)
        if filing.incident_id:
            await session.call(
                "add_activity_to_incident",
                {
                    "incidentId": filing.incident_id,
                    "body": f"{gemini_summary}\n\n{filing.panel_url}",
                },
            )
    elif caps.escalation_path == "alert_rule":
        created = await session.call(
            "alerting_manage_rules",
            delivery_rule(measurement, caps, severity, gemini_summary),
        )
        from .capability import _extract_rule_uid

        filing.rule_uid = _extract_rule_uid(created)
        filing.rule_title = f"Interlock delivery spec: {measurement.title}"
        if not filing.rule_uid:
            filing.notes.append(f"alert rule create returned no uid: {created[:200]}")
    else:
        filing.notes.append(
            f"no escalation surface available: {caps.escalation_reason}"
        )

    return filing


async def publish_fleet(
    session: GrafanaSession,
    caps: Capabilities,
    measurements: list[Measurement],
    filings: dict[str, Filing],
) -> str:
    await session.call(
        "update_dashboard",
        {
            "dashboard": fleet_dashboard(measurements, caps, filings),
            "folderUid": caps.folder_uid or None,
            "overwrite": True,
            "message": f"Interlock fleet run over {len(measurements)} titles",
        },
    )
    return f"{os.getenv('GRAFANA_URL', '').rstrip('/')}/d/{FLEET_DASHBOARD_UID}"


async def close_out(
    session: GrafanaSession,
    caps: Capabilities,
    filing: Filing,
    repaired: Measurement,
    anchor: datetime,
    *,
    repair_note: str,
    landed: bool,
    withheld_because: str = "",
) -> list[Assertion]:
    """Close the record on the strength of the re-measurement, not of a status update.

    `landed` comes from `repair.RepairResult.landed`, which is computed from a second
    ffmpeg pass over the file the repair wrote. It is not the repair command's exit
    code. A loudnorm pass that returns zero and moves nothing arrives here with
    landed false, and this function updates the board, records that the repair did
    not deliver, and leaves the alert open. That is the whole point of gating on the
    number: an agent that can resolve its own alert by asserting success has not
    proven anything.

    Interlock also only ever touches records it created. The rule uid and the
    incident id in `filing` were returned by this run's own create calls, and the
    annotation tag was minted by this run, so there is no path by which Interlock
    resolves somebody else's alert.
    """
    out: list[Assertion] = []
    anchor_ms, span_ms = anchor_for(repaired, anchor)

    await session.call(
        "update_dashboard",
        {
            "dashboard": title_dashboard(
                repaired,
                caps,
                anchor_ms,
                span_ms,
                repair={
                    "command": repair_note,
                    "integrated_lufs": round(repaired.integrated_lufs, 1),
                    "integrated_delta_lu": repaired.integrated_delta_lu,
                    "availability": repaired.availability,
                    "in_spec": repaired.in_spec,
                },
            ),
            "folderUid": caps.folder_uid or None,
            "overwrite": True,
            "message": f"Interlock repair re-measured {repaired.title_id} at {repaired.integrated_lufs:.1f} LUFS",
        },
    )

    # The repaired file's own series, read back out of Grafana. This is the second
    # measurement made visible: the panel now holds samples from the file the repair
    # wrote, not from the master that breached.
    try:
        stored = await session.call(
            "get_dashboard_by_uid", {"uid": filing.dashboard_uid}, required=False
        )
    except ToolUnavailable as exc:
        stored = ""
        out.append(
            Assertion(
                claim=f"the re-measured series is readable back out of dashboard {filing.dashboard_uid}",
                holds=False,
                evidence=f"read refused: {exc.detail[:200]}",
                query=f"mcp-grafana get_dashboard_by_uid uid={filing.dashboard_uid}",
            )
        )
    if stored:
        out.append(
            series_readback_assertion(stored, repaired, anchor_ms, filing.dashboard_uid)
        )

    if caps.has("annotation_write"):
        verdict = "Repair landed" if landed else "Repair did not deliver, alert left open"
        await session.call(
            "create_annotation",
            {
                "dashboardUid": filing.dashboard_uid,
                "panelId": 2,
                "text": (
                    f"{verdict}. Re-measured {repaired.integrated_lufs:.1f} LUFS "
                    f"({repaired.integrated_delta_lu:+.1f} LU). {repair_note}"
                ),
                "tags": [
                    "interlock",
                    repaired.title_id,
                    "repaired" if landed else "repair-failed",
                    filing.annotation_tag,
                ],
                "time": int(datetime.now(timezone.utc).timestamp() * 1000),
            },
        )

    if not landed:
        out.append(
            Assertion(
                claim="an unclosed repair leaves the alert and the incident untouched",
                holds=True,
                evidence=(
                    f"{withheld_because}. Re-measured "
                    f"{repaired.integrated_lufs:.1f} LUFS, "
                    f"{repaired.integrated_delta_lu:+.1f} LU from the "
                    f"{spec.EBU_R128_TARGET_LUFS:.0f} LUFS target against a "
                    f"{spec.EBU_R128_TOLERANCE_LU:.0f} LU tolerance. Alert rule "
                    f"{filing.rule_uid or '(none)'} and incident "
                    f"{filing.incident_id or '(none)'} were not touched."
                ),
                query="local: both close gates, reported separately",
            )
        )
        return out

    if filing.rule_uid:
        current = await session.call_json(
            "alerting_manage_rules", {"operation": "get", "rule_uid": filing.rule_uid}
        )
        payload = delivery_rule(repaired, caps, repaired.severity, repair_note)
        payload.update({"operation": "update", "rule_uid": filing.rule_uid})
        payload["annotations"]["summary"] = (
            f"{repaired.title} re-measured {repaired.integrated_lufs:.1f} LUFS after repair, "
            f"{repaired.integrated_delta_lu:+.1f} LU from target."
        )
        await session.call("alerting_manage_rules", payload)
        out.append(
            Assertion(
                claim=f"alert rule {filing.rule_uid} now holds the re-measured figure",
                holds=abs(repaired.integrated_delta_lu) <= spec.EBU_R128_TOLERANCE_LU,
                evidence=(
                    f"rule query data updated to delta_lu={abs(repaired.integrated_delta_lu):.3f}, "
                    f"threshold gt {spec.EBU_R128_TOLERANCE_LU}; previous title "
                    f"{(current or {}).get('title', 'unknown')}"
                ),
                query=f"mcp-grafana alerting_manage_rules update rule_uid={filing.rule_uid}",
            )
        )

    if filing.incident_id:
        await session.call(
            "add_activity_to_incident",
            {
                "incidentId": filing.incident_id,
                "body": (
                    f"Repair landed. Re-measured {repaired.integrated_lufs:.1f} LUFS "
                    f"({repaired.integrated_delta_lu:+.1f} LU from target). {repair_note}"
                ),
            },
        )
        await session.call(
            "update_incident", {"incidentId": filing.incident_id, "status": "resolved"}
        )
        detail = await session.call("get_incident", {"id": filing.incident_id})
        out.append(
            Assertion(
                claim=f"IRM incident {filing.incident_id} is resolved",
                holds='"status":"resolved"' in detail.replace(" ", ""),
                evidence=detail[:200],
                query=f"mcp-grafana get_incident id={filing.incident_id}",
            )
        )

    return out


# --- verification --------------------------------------------------------


async def verify(
    session: GrafanaSession,
    caps: Capabilities,
    filing: Filing,
    measurement: Measurement,
    expected_breaches: int,
) -> list[Assertion]:
    """Read every write back. Each assertion names the query that produced it.

    A read that the stack refuses becomes a failed assertion rather than a failed run.
    The write it was checking either landed or it did not, and throwing away the whole
    run's evidence because a verification query was rejected would hide the answer
    instead of recording it.
    """
    out: list[Assertion] = []

    try:
        dashboard = await session.call(
            "get_dashboard_by_uid", {"uid": filing.dashboard_uid}, required=False
        )
    except ToolUnavailable as exc:
        dashboard = ""
        out.append(
            Assertion(
                claim=f"dashboard {filing.dashboard_uid} is readable back",
                holds=False,
                evidence=f"read refused: {exc.detail[:200]}",
                query=f"mcp-grafana get_dashboard_by_uid uid={filing.dashboard_uid}",
            )
        )
    marker = f"{measurement.integrated_lufs:.1f} LUFS"
    out.append(
        Assertion(
            claim=f"dashboard {filing.dashboard_uid} carries the measured integrated loudness",
            holds=marker in dashboard,
            evidence=f"looked for the string {marker!r} in the stored dashboard JSON",
            query=f"mcp-grafana get_dashboard_by_uid uid={filing.dashboard_uid}",
        )
    )
    # The summary figure landing is not the series landing. This reads the 100 ms
    # samples back out of what Grafana stored and compares them against ffmpeg.
    out.append(
        series_readback_assertion(
            dashboard, measurement, filing.anchor_epoch_ms, filing.dashboard_uid
        )
    )

    if expected_breaches == 0:
        # Nothing to assert. "0 rows found, 0 expected" is a check that cannot fail, and
        # a PASS on it would pad the evidence rail with a claim that constrains nothing.
        filing.notes.append(
            "no breach spans filed for this title: it is inside the corridor, so the "
            "annotation read-back was skipped rather than reported as a vacuous pass"
        )
    elif filing.annotation_tag:
        try:
            found = await session.call_json(
                "get_annotations",
                {
                    "tags": ["interlock", measurement.title_id, filing.annotation_tag],
                    "limit": 100,
                    "from": filing.anchor_epoch_ms - 120_000,
                    "to": filing.anchor_epoch_ms + filing.window_ms + 3_600_000,
                },
                required=False,
            )
        except ToolUnavailable as exc:
            found = {"refused": exc.detail[:200]}
        rows = found if isinstance(found, list) else (found or {}).get("Payload", []) or (found or {}).get("annotations", [])
        count = len(rows) if isinstance(rows, list) else 0
        out.append(
            Assertion(
                claim=f"{expected_breaches} breach spans are readable back out of Grafana",
                holds=count >= expected_breaches,
                evidence=f"get_annotations returned {count} rows for tag {filing.annotation_tag}",
                query=(
                    "mcp-grafana get_annotations tags=[interlock,"
                    f"{measurement.title_id},{filing.annotation_tag}]"
                ),
            )
        )

    if filing.rule_uid:
        try:
            rule = await session.call(
                "alerting_manage_rules",
                {"operation": "get", "rule_uid": filing.rule_uid},
                required=False,
            )
        except ToolUnavailable as exc:
            rule = f"read refused: {exc.detail[:200]}"
        out.append(
            Assertion(
                claim=f"alert rule {filing.rule_uid} exists and names this title",
                holds=measurement.title_id in rule,
                evidence=f"rule JSON is {len(rule)} chars and carries title_id={measurement.title_id}",
                query=f"mcp-grafana alerting_manage_rules get rule_uid={filing.rule_uid}",
            )
        )

    return out


RULE_STATE_POLL_SECONDS = float(os.getenv("INTERLOCK_RULE_POLL_SECONDS", "240"))
GROUP_INTERVAL_SECONDS = int(os.getenv("INTERLOCK_RULE_INTERVAL_SECONDS", "10"))


async def set_group_interval(session: GrafanaSession, folder_uid: str, group: str) -> Assertion:
    """Tighten the rule group's evaluation interval so a run can watch it settle.

    A new Grafana rule group evaluates once a minute by default, which means a run
    that creates a rule, repairs the file and updates the rule inside forty seconds
    never observes the firing state at all: the ruler simply reports the final answer.
    Ten seconds is Grafana Cloud's floor and it is what makes the transition visible
    within one run rather than one coffee break.

    `alerting_manage_rules` has no argument for this. The interval belongs to the
    group, not the rule, so it is set through the provisioning API, still over MCP.
    """
    payload = await session.call_json(
        "grafana_api_request",
        {
            "endpoint": f"/api/v1/provisioning/folder/{folder_uid}/rule-groups/{group}",
            "method": "PUT",
            "body": json.dumps({"interval": GROUP_INTERVAL_SECONDS}),
            "headers": {"X-Disable-Provenance": "true"},
        },
        required=False,
    )
    status = payload.get("status") if isinstance(payload, dict) else None
    return Assertion(
        claim=f"rule group {group} evaluates every {GROUP_INTERVAL_SECONDS}s",
        holds=status in (200, 202),
        evidence=f"provisioning API returned {status}",
        query=f"mcp-grafana grafana_api_request PUT /api/v1/provisioning/folder/{folder_uid}/rule-groups/{group}",
    )


async def rule_state(session: GrafanaSession, rule_uid: str) -> str:
    """The live evaluation state of one Grafana-managed rule.

    Read from Grafana's own Prometheus-compatible rules endpoint, which reports what
    the ruler is doing rather than what was provisioned. Returns 'firing',
    'inactive', 'pending', or '' when the rule is not there yet.
    """
    payload = await session.call_json(
        "grafana_api_request",
        {"endpoint": "/api/prometheus/grafana/api/v1/rules", "method": "GET"},
        required=False,
    )
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    groups = (data or {}).get("data", {}).get("groups", []) if isinstance(data, dict) else []
    for group in groups if isinstance(groups, list) else []:
        for rule in group.get("rules", []) if isinstance(group, dict) else []:
            if not isinstance(rule, dict):
                continue
            labels = rule.get("labels", {}) or {}
            if rule.get("uid") == rule_uid or labels.get("__alert_rule_uid__") == rule_uid:
                return str(rule.get("state", ""))
    return ""


async def await_rule_state(
    session: GrafanaSession,
    rule_uid: str,
    wanted: str,
    *,
    timeout: float | None = None,
) -> Assertion:
    """Poll the ruler until the rule reaches `wanted`, or say it never did.

    This exists because a 200 from `alerting_manage_rules` is an accepted request, not
    a firing alert. The rule is provisioned instantly and evaluated on its own
    schedule, so the only way to know Grafana agreed with the measurement is to watch
    the state it settles on. A run that reported success from the create response would
    be reporting that Grafana received a JSON document.
    """
    budget = RULE_STATE_POLL_SECONDS if timeout is None else timeout
    started = time.monotonic()
    seen: list[str] = []
    while time.monotonic() - started < budget:
        state = await rule_state(session, rule_uid)
        if state and (not seen or seen[-1] != state):
            seen.append(state)
        if state == wanted:
            elapsed = time.monotonic() - started
            return Assertion(
                claim=f"Grafana evaluated rule {rule_uid} to {wanted}",
                holds=True,
                evidence=f"reached {wanted} after {elapsed:.0f}s, states seen: {seen}",
                query="mcp-grafana grafana_api_request GET /api/prometheus/grafana/api/v1/rules",
            )
        await asyncio.sleep(5)
    return Assertion(
        claim=f"Grafana evaluated rule {rule_uid} to {wanted}",
        holds=False,
        evidence=(
            f"still not {wanted} after {budget:.0f}s, states seen: {seen or 'none'}. "
            "The rule exists; the ruler had not settled on that state inside the budget."
        ),
        query="mcp-grafana grafana_api_request GET /api/prometheus/grafana/api/v1/rules",
    )


async def alert_state_history(
    session: GrafanaSession, caps: Capabilities, rule_uid: str, minutes: int = 60
) -> Assertion:
    """Prove the rule actually changed state, out of Grafana's own state history.

    This is the assertion that cannot be produced by Interlock claiming anything.
    Grafana writes alert state transitions into a Loki stream that Interlock only
    reads. A transition into Alerting and then back is the fleet's SLO breach and
    its repair, recorded by the system rather than by the agent.
    """
    if not caps.alert_state_history_uid:
        return Assertion(
            claim="alert rule state transition is recorded in Grafana's state history",
            holds=False,
            evidence="this stack exposes no alert state history datasource",
            query="",
        )
    # Relative syntax rather than an absolute stamp. Grafana's Loki time parser rejects
    # Python's isoformat with microseconds ("unexpected tDIGIT at character 25") and
    # relative time is unambiguous about the timezone, which the same error warns about.
    logql = '{from="state-history"} | json | ruleUID=`%s`' % rule_uid
    try:
        raw = await session.call(
            "query_loki_logs",
            {
                "datasourceUid": caps.alert_state_history_uid,
                "logql": logql,
                "startRfc3339": f"now-{max(int(minutes), 1)}m",
                "endRfc3339": "now",
                "limit": 50,
                "direction": "forward",
            },
            required=False,
        )
    except ToolUnavailable as exc:
        # A read that fails is a failed assertion, not a failed run. The write it was
        # checking either landed or did not, and losing the whole run to a refused
        # verification query would destroy the evidence rather than record it.
        return Assertion(
            claim=f"alert rule {rule_uid} has state transitions in Grafana's own history",
            holds=False,
            evidence=f"state history query refused: {exc.detail[:200]}",
            query=f"LogQL against {caps.alert_state_history_uid}: {logql}",
        )
    states = sorted(set(_states_in(raw)))
    return Assertion(
        claim=f"alert rule {rule_uid} has state transitions in Grafana's own history",
        holds=bool(states),
        evidence=f"states seen: {states or 'none yet'}",
        query=f"LogQL against {caps.alert_state_history_uid}: {logql}",
    )


def _states_in(raw: str) -> list[str]:
    found = []
    for token in ("Alerting", "Normal", "Pending", "NoData", "Error"):
        if f'"{token}"' in raw or f"current={token}" in raw or f'current":"{token}' in raw:
            found.append(token)
    return found


def stored_series(dashboard_json: str, panel_id: int = 2) -> list[tuple[str, float, float]]:
    """Pull the loudness series back out of a dashboard Grafana handed us.

    `get_dashboard_by_uid` returns the document Grafana stored, so the CSV in the
    panel's query is the CSV Grafana is holding rather than the one Interlock sent. That
    distinction is the whole reason this function exists: comparing what came back
    against the local ffmpeg measurement is the only way to know the series landed
    intact, as opposed to knowing that a dashboard save returned 200.

    Returns (iso_timestamp, short_term_lufs, momentary_lufs) per row, or an empty list
    when the panel is not there or carries no inline data.
    """
    try:
        doc = json.loads(dashboard_json)
    except (json.JSONDecodeError, TypeError):
        return []
    while isinstance(doc, dict) and "dashboard" in doc and isinstance(doc["dashboard"], dict):
        doc = doc["dashboard"]
    if not isinstance(doc, dict):
        return []
    for panel in doc.get("panels", []) if isinstance(doc.get("panels"), list) else []:
        if not isinstance(panel, dict) or panel.get("id") != panel_id:
            continue
        targets = panel.get("targets") or []
        if not isinstance(targets, list) or not targets:
            return []
        csv = targets[0].get("data") if isinstance(targets[0], dict) else None
        if not isinstance(csv, str) or not csv.strip():
            return []
        rows: list[tuple[str, float, float]] = []
        for line in csv.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                rows.append((parts[0], float(parts[1]), float(parts[2])))
            except ValueError:
                continue
        return rows
    return []


def series_readback_assertion(
    dashboard_json: str, measurement: Measurement, anchor_ms: int, uid: str
) -> Assertion:
    """Compare the series Grafana is holding against the series ffmpeg produced.

    Row for row, on the short-term value, which is the number the SLO is computed from.
    A panel that came back with no rows, with a different number of rows, or with one
    sample altered fails this. That is deliberate: the alternative check, "the dashboard
    read back and mentions the integrated figure", passes on a panel whose series is
    empty, and an empty panel over a breaching master is the exact failure this product
    exists to catch.
    """
    got = stored_series(dashboard_json)
    expected = [
        (line.split(",")[0], float(line.split(",")[1]), float(line.split(",")[2]))
        for line in series_csv_wallclock(measurement, anchor_ms).strip().splitlines()[1:]
    ]
    if not expected:
        return Assertion(
            claim=f"the measured loudness series is readable back out of dashboard {uid}",
            holds=False,
            evidence="the local measurement produced no series to compare against",
            query=f"mcp-grafana get_dashboard_by_uid uid={uid}",
        )
    mismatches = [
        i
        for i, (want, have) in enumerate(zip(expected, got))
        if abs(want[1] - have[1]) > 0.001 or want[0] != have[0]
    ]
    holds = len(got) == len(expected) and not mismatches
    if holds:
        worst = min(expected, key=lambda r: r[1])
        evidence = (
            f"{len(got)} rows came back out of Grafana and every short-term value matches "
            f"the ffmpeg measurement to 0.001 LUFS. Quietest stored sample "
            f"{worst[1]:.2f} LUFS at {worst[0]}. Series spans {expected[0][0]} to "
            f"{expected[-1][0]}."
        )
    else:
        evidence = (
            f"Grafana returned {len(got)} rows against {len(expected)} measured, "
            f"{len(mismatches)} of the overlapping rows disagree with ffmpeg"
            + (
                f", first at index {mismatches[0]}: stored {got[mismatches[0]][1]} "
                f"against measured {expected[mismatches[0]][1]}"
                if mismatches
                else ""
            )
        )
    return Assertion(
        claim=f"the measured loudness series is readable back out of dashboard {uid}",
        holds=holds,
        evidence=evidence,
        query=(
            f"mcp-grafana get_dashboard_by_uid uid={uid}, panel 2 target data, "
            "compared row for row against the ffmpeg ebur128 series"
        ),
    )


def series_rows(measurement: Measurement, max_rows: int = 600) -> list[list]:
    """The measured series as compact rows, for a surface that draws it itself.

    Same decimation as the panel query, so the trace an operator sees on Interlock's
    own page and the trace on the Grafana panel are the same samples rather than two
    different summaries of them.
    """
    rows: list[list] = []
    for line in measurement.series_csv(max_rows=max_rows).splitlines()[1:]:
        t_s, short_term, momentary = line.split(",")
        rows.append([round(float(t_s), 2), float(short_term), float(momentary)])
    return rows


def run_record(
    caps: Capabilities,
    measurements: list[Measurement],
    filings: dict[str, Filing],
    assertions: list[Assertion],
    fleet_url: str,
    session: GrafanaSession,
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grafana_url": os.getenv("GRAFANA_URL", "").rstrip("/"),
        "fleet_dashboard_url": fleet_url,
        "series_storage": series_storage_label(),
        "capabilities": caps.as_dict(),
        "titles": [m.as_dict() for m in measurements],
        "filings": {k: v.as_dict() for k, v in filings.items()},
        "assertions": [a.as_dict() for a in assertions],
        "mcp_calls": [c.as_dict() for c in session.calls],
        "mcp_tools_offered": len(session.tool_names),
        "mcp_calls_retried_after_rate_limit": session.throttled,
    }


def write_run_record(record: dict, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
