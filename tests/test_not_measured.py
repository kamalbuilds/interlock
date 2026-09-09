"""A check that could not run must never read as a check that passed.

This file exists because of one concrete defect, found in a sibling product and then
found here. There, picture detection filters ran unconditionally against a repaired file
that had been written audio only, found zero events because there was no picture, and
reported the picture checks as PASSED. Absence rendered as success.

Interlock's exposure was the same shape and sat on the number that decides whether a
master clips. `Measurement.in_spec` checked the true peak with
`if self.true_peak_dbfs is not None and ...`, so a pass that produced no true peak block,
which is what happens the moment `peak=true` is dropped from the filter, skipped the
ceiling check entirely and returned in spec. Worse, `RepairResult.landed` looked only at
integrated loudness, so an alert could be closed on a repaired file whose peaks had been
pushed over the ceiling by the very gain change that fixed its level.

Both are now failures with a named reason. The tests below hold that in place from both
sides: a complete measurement still passes, and each individual absence still fails.
"""

from __future__ import annotations

from pathlib import Path

from interlock import spec
from interlock.measure import Measurement, Sample
from interlock.repair import WORK_ROOT, RepairResult


def _measurement(
    integrated: float = -23.0,
    *,
    peak: float | None = -6.0,
    lra: float | None = 6.0,
    samples: int = 300,
) -> Measurement:
    return Measurement(
        title_id="t",
        title="A Title",
        source="/tmp/a.mp4",
        command="ffmpeg -hide_banner -i /tmp/a.mp4 -af ebur128=peak=true -f null -",
        window_seconds=30,
        duration_seconds=1800.0,
        integrated_lufs=integrated,
        true_peak_dbfs=peak,
        lra_lu=lra,
        samples=[
            Sample(t=round(0.1 * (i + 1), 1), momentary_lufs=integrated, short_term_lufs=integrated)
            for i in range(samples)
        ],
    )


# --- the paired positive: a complete measurement still passes --------------


def test_a_fully_measured_in_spec_title_is_in_spec():
    """Without this, everything below could pass because in_spec always returns False."""
    m = _measurement(-23.0)
    assert m.unmeasured == []
    assert m.in_spec is True


# --- each absence, on its own, fails --------------------------------------


def test_a_missing_true_peak_is_not_measured_and_never_in_spec():
    """The original defect. A ceiling nobody measured is not a ceiling that was met."""
    m = _measurement(-23.0, peak=None)
    assert m.in_spec is False
    assert any(gap.startswith("true_peak") for gap in m.unmeasured)
    assert "peak=true" in " ".join(m.unmeasured)


def test_a_missing_loudness_range_is_not_measured_and_never_in_spec():
    m = _measurement(-23.0, lra=None)
    assert m.in_spec is False
    assert any(gap.startswith("loudness_range") for gap in m.unmeasured)


def test_a_measurement_with_no_series_is_not_measured_and_never_in_spec():
    m = _measurement(-23.0, samples=0)
    assert m.in_spec is False
    assert any(gap.startswith("corridor") for gap in m.unmeasured)


def test_a_window_entirely_below_the_absolute_gate_is_not_measured():
    """A silent window has no programme time, so availability constrains nothing."""
    m = _measurement(-23.0)
    m.samples = [
        Sample(t=round(0.1 * (i + 1), 1), momentary_lufs=-120.7, short_term_lufs=-120.7)
        for i in range(300)
    ]
    assert m.gated_samples == []
    # The arithmetic that used to be reassuring here.
    assert m.availability == 1.0
    # And the verdict that must not be.
    assert m.in_spec is False
    assert any("absolute gate" in gap for gap in m.unmeasured)


def test_the_unmeasured_reason_names_the_criterion_and_the_cause():
    """An operator reading NOT MEASURED has to know which check and why."""
    m = _measurement(-23.0, peak=None, lra=None)
    assert len(m.unmeasured) == 2
    joined = " ".join(m.unmeasured)
    assert f"{spec.TRUE_PEAK_CEILING_DBTP:.0f} dBTP" in joined
    assert "flattened" in joined or "dynamics" in joined


# --- the close gate ------------------------------------------------------


def _repair(before: float, after: float, *, after_peak: float | None = -6.0, after_lra: float | None = 6.0) -> RepairResult:
    return RepairResult(
        title_id="t",
        source=Path("/tmp/a.mp4"),
        output=WORK_ROOT / "t" / "repaired.wav",
        analysis_command="ffmpeg ... print_format=json",
        apply_command="ffmpeg ... measured_I=...",
        measured_offsets={"input_i": str(before)},
        before=_measurement(before, peak=-3.0),
        after=_measurement(after, peak=after_peak, lra=after_lra),
    )


def test_a_repair_that_delivers_the_level_and_the_peak_lands():
    """The paired positive for the close gate."""
    result = _repair(-26.1, -23.1)
    assert result.blocked_by == []
    assert result.landed is True


def test_a_repair_whose_true_peak_went_over_the_ceiling_does_not_land():
    """loudnorm raising a quiet master by 6 LU can push its peaks over the ceiling.

    The level-only gate closed the record on exactly this, which swaps one rejection slip
    for another one.
    """
    result = _repair(-29.0, -23.0, after_peak=-0.4)
    assert result.landed is False
    assert any("dBTP ceiling" in reason for reason in result.blocked_by)
    # The level really was delivered, so the block is the peak and nothing else.
    assert not any("integrated loudness" in reason for reason in result.blocked_by)


def test_a_repair_whose_true_peak_could_not_be_read_does_not_land():
    """The one that matters most: an alert closed on a check that could not fail."""
    result = _repair(-26.1, -23.1, after_peak=None)
    assert result.landed is False
    assert any(reason.startswith("NOT MEASURED") for reason in result.blocked_by)


def test_the_block_reasons_are_reported_and_not_just_counted():
    result = _repair(-35.0, -27.0, after_peak=None, after_lra=None)
    payload = result.as_dict()
    assert payload["landed"] is False
    assert len(payload["blocked_by"]) >= 3
    assert payload["unmeasured_after"], "the record must carry what was not measured"
    # Each reason is a sentence an operator can act on, not a flag.
    for reason in payload["blocked_by"]:
        assert len(reason) > 20, reason


# --- one unavailable master must not cost the other seven -----------------


def test_a_title_archive_org_refuses_costs_only_that_title(monkeypatch, tmp_path):
    """The defect this replaces: the first failed fetch left the whole fleet undownloaded.

    A cold read of The Memphis Belle from a fresh container came back `curl exit 22`
    while the other seven titles were perfectly available, and because `ensure_fleet`
    raised on the first failure, nothing at all was on disk and the product had nothing
    to measure. Now the failure is scoped to its own title and reported by name.
    """
    from interlock import catalog

    refused = catalog.FLEET[0].title_id
    calls: list[str] = []

    def fake_fetch(title, *, prefix_bytes=0, force=False):
        calls.append(title.title_id)
        if title.title_id == refused:
            raise RuntimeError(f"fetch failed for {title.identifier}: curl exit 22")
        path = tmp_path / f"{title.title_id}.mp4"
        path.write_bytes(b"x" * 1024)
        return path

    monkeypatch.setattr(catalog, "fetch", fake_fetch)
    rows = catalog.ensure_fleet()

    # Every title was attempted, not just the ones before the failure.
    assert len(calls) == len(catalog.FLEET)
    assert len(rows) == len(catalog.FLEET)

    bad = [r for r in rows if not r["available"]]
    good = [r for r in rows if r["available"]]
    assert len(bad) == 1 and bad[0]["title_id"] == refused
    assert "curl exit 22" in bad[0]["reason"]
    assert len(good) == len(catalog.FLEET) - 1
    assert all(r["bytes"] > 0 for r in good)


def test_downloaded_reports_only_masters_that_are_really_on_disk(monkeypatch):
    """A zero byte file left by a broken transfer is not a downloaded master."""
    from interlock import catalog

    have = catalog.downloaded()
    everything = [t.title_id for t in catalog.FLEET]
    assert set(have) <= set(everything)
    for title_id in have:
        path = catalog.BY_ID[title_id].local_video
        assert path.exists() and path.stat().st_size > 0
