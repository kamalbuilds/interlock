"""The delivery spec, expressed as a service level objective.

A film archive behaves like a fleet. Each title is a service, the delivery spec is
its SLO, and every second of programme audio outside the loudness corridor burns
error budget. This module is the only place those numbers live, so a judge can read
the contract in one screen and reproduce it with ffmpeg.

Provenance matters more than tidiness here, so each constant says where it comes
from and whether it is a published standard or a choice Interlock made.

PUBLISHED STANDARDS
  EBU R128        integrated programme loudness -23.0 LUFS, tolerance +/- 1.0 LU
                  https://tech.ebu.ch/docs/r/r128.pdf
  EBU R128        true peak ceiling -1.0 dBTP
  EBU Tech 3341   momentary loudness = 400 ms sliding window
                  short-term loudness = 3 s sliding window
                  absolute gate for the integrated measurement -70 LUFS
  ATSC A/85       (US CALM Act) target -24.0 LKFS, tolerance +/- 2.0 LU
  Netflix TTSS    subtitle reading speed <= 17 chars/sec, minimum cue 5/6 s,
                  maximum 42 characters per line, maximum 2 lines

INTERLOCK'S OWN CHOICES, stated so they are not mistaken for standards
  CORRIDOR_LU         short-term loudness may sit at most 8.0 LU either side of
                      target before a sample counts against the budget. R128
                      constrains the integrated figure and the true peak; it does
                      not publish a short-term corridor for long-form programme.
                      Eight LU is wide enough that a scored feature with real
                      dynamics passes, and narrow enough that a transfer with a
                      dead reel or a blown optical track does not.
  AVAILABILITY_TARGET 0.99 of gated programme time inside the corridor. This is
                      the SLO. One percent of a 90 minute feature is 54 seconds,
                      roughly one reel change plus a head slate, so a clean
                      archival transfer spends its budget on artefacts a human
                      would also forgive.

Nothing in this module reads a model or a network. It is arithmetic over numbers
ffmpeg produced.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- published standards --------------------------------------------------

EBU_R128_TARGET_LUFS = -23.0
EBU_R128_TOLERANCE_LU = 1.0
ATSC_A85_TARGET_LKFS = -24.0
ATSC_A85_TOLERANCE_LU = 2.0
TRUE_PEAK_CEILING_DBTP = -1.0
ABSOLUTE_GATE_LUFS = -70.0
MOMENTARY_WINDOW_SECONDS = 0.400
SHORT_TERM_WINDOW_SECONDS = 3.0

NETFLIX_MAX_CPS = 17.0
NETFLIX_MIN_CUE_SECONDS = 5 / 6
NETFLIX_MAX_LINE_CHARS = 42
NETFLIX_MAX_LINES = 2

# --- Interlock's own SLO ---------------------------------------------------

CORRIDOR_LU = 8.0
AVAILABILITY_TARGET = 0.99

SAMPLE_INTERVAL_SECONDS = 0.100
"""ffmpeg's ebur128 filter emits one measurement line every 100 ms.

Each in-corridor or out-of-corridor sample therefore accounts for 100 ms of
programme time, which is what turns a sample count into a budget in seconds.
"""


def corridor() -> tuple[float, float]:
    """The short-term loudness band, as (floor_lufs, ceiling_lufs)."""
    return (EBU_R128_TARGET_LUFS - CORRIDOR_LU, EBU_R128_TARGET_LUFS + CORRIDOR_LU)


def error_budget_seconds(gated_seconds: float) -> float:
    """Seconds a title may spend outside the corridor before it breaches SLO."""
    return max(gated_seconds, 0.0) * (1.0 - AVAILABILITY_TARGET)


@dataclass(frozen=True)
class Severity:
    """How hard a title missed, in the vocabulary Grafana alerting and IRM use."""

    name: str
    irm_severity: str
    rank: int

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.name


CRITICAL = Severity("critical", "critical", 3)
MAJOR = Severity("major", "major", 2)
MINOR = Severity("minor", "minor", 1)
CLEAR = Severity("clear", "minor", 0)

SEVERITIES = {s.name: s for s in (CLEAR, MINOR, MAJOR, CRITICAL)}


def severity_for(burn_ratio: float, integrated_delta_lu: float) -> Severity:
    """Classify a title from its budget burn and its integrated loudness error.

    burn_ratio is consumed budget over allowed budget: 1.0 means the title spent
    exactly its allowance, 4.0 means four times the allowance.

    The two inputs answer different questions and both change what an operator
    does. burn_ratio says how much of the programme is wrong. integrated_delta_lu
    says whether one gain change would fix it. A title 12 LU off target is a
    mislabelled transfer and needs a person; a title 1.5 LU off with a large burn
    has local damage and needs the timecodes, not a gain change.
    """
    delta = abs(integrated_delta_lu)
    if burn_ratio <= 1.0 and delta <= EBU_R128_TOLERANCE_LU:
        return CLEAR
    if delta >= 6.0 or burn_ratio >= 20.0:
        return CRITICAL
    if delta >= 3.0 or burn_ratio >= 5.0:
        return MAJOR
    return MINOR


SPEC_CITATIONS = {
    "integrated_loudness": "EBU R128 -23.0 LUFS +/- 1.0 LU",
    "true_peak": "EBU R128 true peak ceiling -1.0 dBTP",
    "atsc_a85": "ATSC A/85 CALM Act -24.0 LKFS +/- 2.0 LU",
    "corridor": f"Interlock corridor: short-term within +/- {CORRIDOR_LU:.0f} LU of target",
    "availability": (
        f"Interlock SLO: {AVAILABILITY_TARGET:.0%} of gated programme time in corridor"
    ),
    "subtitle_speed": "Netflix TTSS <= 17 chars/sec",
    "subtitle_duration": "Netflix TTSS >= 5/6 s per cue",
    "subtitle_line": "Netflix TTSS <= 42 characters per line",
}
