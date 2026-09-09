"""Tests over the parts that execute: the repair guardrails, the Grafana payloads,
and the routing that decides what a run does.

The Grafana behaviour that matters is proven by a live run against a real stack, not
here. What these tests constrain is the shape of what gets sent and the conditions
under which it gets sent at all, because those are the places a refactor can quietly
turn a real write into a no-op or turn a guardrail off.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from interlock import spec
from interlock.grafana import surfaces
from interlock.grafana.capability import Capabilities, Capability, org_id
from interlock.measure import Measurement, Sample
from interlock.repair import WORK_ROOT, RepairResult, UnsafeOutput, _safe_output


def _measurement(integrated: float, *, title_id: str = "t", peak: float = -6.0, lra: float = 6.0) -> Measurement:
    samples = [
        Sample(t=round(0.1 * (i + 1), 1), momentary_lufs=integrated, short_term_lufs=integrated)
        for i in range(300)
    ]
    return Measurement(
        title_id=title_id,
        title="A Title",
        source="/tmp/a.mp4",
        command="ffmpeg -i /tmp/a.mp4 -af ebur128=peak=true -f null -",
        window_seconds=30,
        duration_seconds=1800.0,
        integrated_lufs=integrated,
        true_peak_dbfs=peak,
        lra_lu=lra,
        samples=samples,
    )


# --- the repair guardrails ------------------------------------------------


def test_repair_output_must_live_inside_the_project_work_directory():
    inside = _safe_output(WORK_ROOT / "a_title" / "repaired.wav")
    assert WORK_ROOT.resolve() in inside.parents


@pytest.mark.parametrize(
    "hostile",
    [
        Path("/tmp/escape.wav"),
        WORK_ROOT / ".." / ".." / "vault" / "data" / "source.mp4",
        WORK_ROOT.parent / "data" / "vicki_1953" / "Vicki (1953).mp4",
        WORK_ROOT / ".." / "web" / "index.html",
    ],
)
def test_repair_refuses_to_write_outside_the_work_directory(hostile: Path):
    """ffmpeg must never be pointed at a source master or another project.

    The traversal cases matter more than the absolute one: a caller that joins a
    relative path onto the work root can walk straight back out to the downloaded
    masters, and the whole point of the guardrail is that a repair cannot destroy the
    thing it is repairing.
    """
    with pytest.raises(UnsafeOutput):
        _safe_output(hostile)


def _repair_result(before: float, after: float) -> RepairResult:
    return RepairResult(
        title_id="t",
        source=Path("/tmp/a.mp4"),
        output=WORK_ROOT / "t" / "repaired.wav",
        analysis_command="ffmpeg ... print_format=json",
        apply_command="ffmpeg ... measured_I=...",
        measured_offsets={"input_i": str(before)},
        before=_measurement(before),
        after=_measurement(after),
    )


def test_landed_is_the_measured_number_and_not_an_improvement():
    # A repair that halves the error but still misses the spec has not delivered.
    assert not _repair_result(-35.0, -27.0).landed
    assert _repair_result(-26.1, -23.1).landed
    # Exactly on the tolerance boundary counts as delivered; a hair outside does not.
    assert _repair_result(-26.1, -23.0 - spec.EBU_R128_TOLERANCE_LU).landed
    assert not _repair_result(-26.1, -23.0 - spec.EBU_R128_TOLERANCE_LU - 0.05).landed


def test_improvement_is_reported_separately_from_delivery():
    partial = _repair_result(-35.0, -27.0)
    assert partial.improvement_lu == pytest.approx(8.0)
    assert not partial.landed


# --- capability routing ---------------------------------------------------


def _caps(**available: bool) -> Capabilities:
    caps = Capabilities(folder_uid="interlock", infinity_uid="inf")
    for name, ok in available.items():
        caps.add(Capability(name=name, available=ok, detail="probed", proof="probe"))
    return caps


def test_escalation_prefers_the_strongest_surface_the_stack_accepted():
    assert _caps(incident_open=True, alert_rule_write=True, annotation_write=True).escalation_path == "irm_incident"
    assert _caps(incident_open=False, alert_rule_write=True, annotation_write=True).escalation_path == "alert_rule"
    assert _caps(incident_open=False, alert_rule_write=False, annotation_write=True).escalation_path == "annotation_only"
    assert _caps(incident_open=False, alert_rule_write=False, annotation_write=False).escalation_path == "none"


def test_the_refusal_reason_travels_with_the_downgrade():
    caps = Capabilities(folder_uid="f", infinity_uid="i")
    caps.add(Capability("incident_open", False, "Counters_orgID_fk foreign key fails", "probe"))
    caps.add(Capability("alert_rule_write", True, "rule created then deleted", "probe"))
    caps.add(Capability("annotation_write", True, "written and read back", "probe"))
    assert caps.escalation_path == "alert_rule"
    assert "Counters_orgID_fk" in caps.escalation_reason


# --- the payloads that get sent ------------------------------------------


def test_the_alert_rule_carries_the_measured_distance_from_target():
    m = _measurement(-26.1)
    rule = surfaces.delivery_rule(m, _caps(alert_rule_write=True), spec.MAJOR, "because")
    assert rule["operation"] == "create"
    assert rule["org_id"] == org_id() >= 1
    # The rule holds the number, not a flag Interlock could flip on its own.
    query = rule["data"][0]["model"]["data"]
    assert "3.1" in query
    threshold = rule["data"][1]["model"]["conditions"][0]["evaluator"]["params"][0]
    assert threshold == spec.EBU_R128_TOLERANCE_LU
    assert rule["labels"]["title_id"] == "t"
    assert rule["is_paused"] is False


def test_the_rule_would_not_fire_on_an_in_spec_title():
    """The threshold has to be able to come back inactive, or the alert means nothing."""
    in_spec = surfaces.delivery_rule(_measurement(-23.2), _caps(), spec.CLEAR, "")
    out_of_spec = surfaces.delivery_rule(_measurement(-26.1), _caps(), spec.MAJOR, "")
    inside = float(in_spec["data"][0]["model"]["data"].splitlines()[1])
    outside = float(out_of_spec["data"][0]["model"]["data"].splitlines()[1])
    limit = in_spec["data"][1]["model"]["conditions"][0]["evaluator"]["params"][0]
    assert inside <= limit
    assert outside > limit


def test_the_series_panel_carries_the_measured_samples_as_its_query():
    m = _measurement(-26.1)
    anchor = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    anchor_ms, span_ms = surfaces.anchor_for(m, anchor)
    dash = surfaces.title_dashboard(m, _caps(), anchor_ms, span_ms)
    panel = next(p for p in dash["panels"] if p.get("type") == "timeseries")
    csv = panel["targets"][0]["data"]
    assert csv.splitlines()[0] == "time,short_term_lufs,momentary_lufs,timecode"
    assert len(csv.splitlines()) > 10
    assert "-26.10" in csv
    # Threshold drawn at the corridor ceiling, so the breach is visible on the panel.
    steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert any(s.get("value") == spec.corridor()[1] for s in steps)


def test_the_dashboard_states_where_the_series_is_stored(monkeypatch):
    monkeypatch.delenv("INTERLOCK_OTLP_USER", raising=False)
    monkeypatch.delenv("INTERLOCK_OTLP_TOKEN", raising=False)
    without = surfaces.series_storage_label()
    assert "Not stored in Grafana Cloud Prometheus" in without

    monkeypatch.setenv("INTERLOCK_OTLP_USER", "12345")
    monkeypatch.setenv("INTERLOCK_OTLP_TOKEN", "placed-by-the-operator")
    with_token = surfaces.series_storage_label()
    assert with_token != without
    assert "Grafana Cloud Prometheus" in with_token
    assert "Not stored" not in with_token


def test_the_anchor_maps_media_timecode_onto_wall_clock():
    m = _measurement(-26.1)
    anchor = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    anchor_ms, span_ms = surfaces.anchor_for(m, anchor)
    assert anchor_ms == int(anchor.timestamp() * 1000)
    assert span_ms == pytest.approx(len(m.samples) * 100, abs=1)
    csv = surfaces.series_csv_wallclock(m, anchor_ms)
    first = csv.splitlines()[1]
    assert first.startswith("2026-09-09T12:00:00")
    # The media timecode travels with the wall clock, so nobody has to invert it.
    assert first.split(",")[-1].startswith("00:00:00")


def test_series_rows_and_the_panel_query_agree_on_the_same_samples():
    m = _measurement(-26.1)
    rows = surfaces.series_rows(m, max_rows=100)
    csv = m.series_csv(max_rows=100).splitlines()[1:]
    assert len(rows) == len(csv)
    assert rows[0][1] == pytest.approx(float(csv[0].split(",")[1]))


def test_a_title_id_becomes_a_stable_dashboard_uid():
    assert surfaces.title_dashboard_uid("night_tide") == "interlock-night-tide"
    assert surfaces.title_dashboard_uid("a" * 80).startswith("interlock-")
    assert len(surfaces.title_dashboard_uid("a" * 80)) <= 40
