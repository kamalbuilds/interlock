"""Tests over the SLO arithmetic and the measurement parser.

Written so each one can fail. Where a threshold is asserted, the test also asserts the
value on the other side of it, because a test that only ever exercises the passing side
of a comparison does not constrain the comparison at all.

These use real ffmpeg output shapes and the real downloaded masters where present.
Nothing here mocks Grafana: the Grafana behaviour is proven by a live run, not by a
fake that agrees with whatever the code does.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from interlock import catalog, spec
from interlock.measure import (
    MeasurementFailed,
    Sample,
    measure,
    parse_srt,
    subtitle_findings,
    timecode,
)

FIXTURE_FRAME = (
    "[Parsed_ebur128_0 @ 0x1] t: 0.0999773  TARGET:-23 LUFS    M:-26.4 S:-24.1     "
    "I: -25.0 LUFS       LRA:   4.0 LU  FTPK: -12.0 -12.0 dBFS  TPK: -11.0 -11.0 dBFS\n"
)


# --- spec constants ------------------------------------------------------


def test_corridor_is_symmetric_about_the_target():
    floor, ceiling = spec.corridor()
    assert ceiling - spec.EBU_R128_TARGET_LUFS == pytest.approx(spec.CORRIDOR_LU)
    assert spec.EBU_R128_TARGET_LUFS - floor == pytest.approx(spec.CORRIDOR_LU)


def test_error_budget_is_the_stated_fraction_of_programme_time():
    # A 90 minute feature at a 99 percent objective allows 54 seconds.
    assert spec.error_budget_seconds(5400.0) == pytest.approx(54.0)
    assert spec.error_budget_seconds(0.0) == 0.0


def test_severity_discriminates_rather_than_condemning_everything():
    # The four outcomes must all be reachable, otherwise the classifier is a constant.
    assert spec.severity_for(0.0, 0.0) is spec.CLEAR
    assert spec.severity_for(2.0, 0.5) is spec.MINOR
    assert spec.severity_for(6.0, 0.5) is spec.MAJOR
    assert spec.severity_for(0.5, 12.0) is spec.CRITICAL


def test_severity_reads_both_inputs_and_not_just_one():
    # Same burn, different integrated error, different verdict. If severity only looked
    # at burn_ratio these two would collapse into one answer.
    low_offset = spec.severity_for(6.0, 0.2)
    high_offset = spec.severity_for(6.0, 9.0)
    assert low_offset is not high_offset
    assert high_offset is spec.CRITICAL


# --- gating and corridor membership --------------------------------------


def test_silence_below_the_absolute_gate_does_not_burn_budget():
    quiet = Sample(t=0.1, momentary_lufs=-120.7, short_term_lufs=-120.7)
    assert not quiet.gated
    loud_enough = Sample(t=0.2, momentary_lufs=-30.0, short_term_lufs=-30.0)
    assert loud_enough.gated


def test_corridor_membership_flips_on_the_stated_boundary():
    floor, ceiling = spec.corridor()
    inside = Sample(t=0.1, momentary_lufs=ceiling, short_term_lufs=ceiling)
    outside = Sample(t=0.2, momentary_lufs=ceiling + 0.1, short_term_lufs=ceiling + 0.1)
    assert inside.in_corridor
    assert not outside.in_corridor
    assert Sample(t=0.3, momentary_lufs=floor, short_term_lufs=floor).in_corridor
    assert not Sample(t=0.4, momentary_lufs=floor - 0.1, short_term_lufs=floor - 0.1).in_corridor


def test_timecode_is_the_form_an_operator_can_type():
    assert timecode(0) == "00:00:00.000"
    assert timecode(3725.5) == "01:02:05.500"
    assert timecode(-1) == "00:00:00.000"


# --- captions -------------------------------------------------------------

SRT = """1
00:00:01,000 --> 00:00:01,400
This cue is far too fast for anybody to read comfortably

2
00:00:05,000 --> 00:00:09,000
A calm line.

3
00:00:10,000 --> 00:00:10,400
Short.
"""


def test_srt_parses_all_three_cues():
    cues = parse_srt(SRT)
    assert len(cues) == 3
    assert cues[0]["start"] == pytest.approx(1.0)
    assert cues[1]["end"] == pytest.approx(9.0)


def test_caption_checks_catch_the_fast_cue_and_pass_the_calm_one():
    findings = {f["check"]: f for f in subtitle_findings(SRT)}
    assert findings["subtitle_reading_speed"]["passed"] is False
    assert findings["subtitle_reading_speed"]["measured"] >= 1
    assert findings["subtitle_min_duration"]["passed"] is False

    calm_only = """1
00:00:05,000 --> 00:00:09,000
A calm line.
"""
    calm = {f["check"]: f for f in subtitle_findings(calm_only)}
    assert calm["subtitle_reading_speed"]["passed"] is True
    assert calm["subtitle_min_duration"]["passed"] is True
    assert calm["subtitle_line_length"]["passed"] is True


def test_no_captions_yields_no_caption_findings():
    assert subtitle_findings("") == []


# --- the measurement path over real files --------------------------------

CONTROL = catalog.BY_ID["niagara_falls"]
OFFENDER = catalog.BY_ID["vicki_1953"]
needs_media = pytest.mark.skipif(
    not CONTROL.local_video.exists() or not OFFENDER.local_video.exists(),
    reason="run `python -m interlock.catalog` to fetch the fleet from archive.org first",
)


@needs_media
def test_a_control_title_passes_the_slo():
    """The SLO must be able to come back green.

    A delivery check that fails every title is indistinguishable from a broken
    measurement, so this is the more important half of the pair below.
    """
    m = measure(
        CONTROL.local_video,
        title_id=CONTROL.title_id,
        title=CONTROL.title,
        seconds=CONTROL.window_seconds,
    )
    assert m.availability == pytest.approx(1.0)
    assert m.burn_ratio == pytest.approx(0.0)
    assert abs(m.integrated_delta_lu) <= spec.EBU_R128_TOLERANCE_LU
    assert m.in_spec
    assert m.severity is spec.CLEAR


@needs_media
def test_a_known_offender_fails_the_slo_at_the_published_figure():
    m = measure(OFFENDER.local_video, title_id=OFFENDER.title_id, title=OFFENDER.title, seconds=300)
    # Independently reproduced: Vicki (1953), first 300 s, I = -26.1 LUFS.
    assert m.integrated_lufs == pytest.approx(-26.1, abs=0.15)
    assert m.integrated_delta_lu < -spec.EBU_R128_TOLERANCE_LU
    assert m.burn_ratio > 1.0
    assert not m.in_spec
    assert m.severity is not spec.CLEAR


@needs_media
def test_the_series_is_a_series_and_not_just_a_summary():
    m = measure(CONTROL.local_video, title_id=CONTROL.title_id, title=CONTROL.title, seconds=30)
    # 30 s at one sample per 100 ms. Allow for the filter's start-up offset.
    assert 250 <= len(m.samples) <= 310
    assert m.samples[1].t > m.samples[0].t


@needs_media
def test_breach_spans_carry_a_real_duration_and_a_timecode():
    m = measure(OFFENDER.local_video, title_id=OFFENDER.title_id, title=OFFENDER.title, seconds=300)
    breaches = m.breaches()
    assert breaches, "the known offender must produce at least one out-of-corridor span"
    first = breaches[0]
    assert first.end > first.start
    assert first.duration >= 0.5
    assert first.timecode.count(":") == 2
    # Sorted longest first, so an operator reads the worst damage at the top.
    assert all(
        breaches[i].duration >= breaches[i + 1].duration for i in range(len(breaches) - 1)
    )


@needs_media
def test_decimated_csv_keeps_the_excursion_rather_than_averaging_it_away():
    m = measure(OFFENDER.local_video, title_id=OFFENDER.title_id, title=OFFENDER.title, seconds=300)
    rows = m.series_csv(max_rows=200).splitlines()
    assert rows[0] == "t_seconds,short_term_lufs,momentary_lufs"
    assert 2 <= len(rows) - 1 <= 220
    worst_raw = min(s.short_term_lufs for s in m.samples if s.gated)
    worst_csv = min(float(r.split(",")[1]) for r in rows[1:])
    # The decimation picks the sample furthest from target in each bucket, so the
    # plotted minimum has to be close to the true minimum. A mean would lose it.
    assert worst_csv <= worst_raw + 1.0


def test_a_file_with_no_audio_is_a_failed_measurement_not_a_passing_one(tmp_path: Path):
    """The worst possible failure mode for a QC tool is silence reading as clean."""
    silent = tmp_path / "silence.wav"
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "5", str(silent),
        ],
        capture_output=True,
        check=True,
    )
    with pytest.raises(MeasurementFailed):
        measure(silent, title_id="silence", title="silence", seconds=5)


def test_nostats_would_hide_the_series_which_is_why_it_is_not_passed():
    """Guards the build note in measure.py rather than restating it in a comment."""
    from interlock.measure import loudness_command

    cmd = loudness_command("x.mp4", 30)
    assert "-nostats" not in cmd
    assert "framelog=verbose" not in " ".join(cmd)
    assert "ebur128=peak=true" in " ".join(cmd)
