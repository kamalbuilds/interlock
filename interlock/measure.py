"""Measure a title with ffmpeg and turn the result into SLO arithmetic.

Every number this module returns came out of ffmpeg on this machine. The command is
recorded alongside the result so the figure can be reproduced without reading the
code:

    ffmpeg -i <file> -t <window> -af ebur128=peak=true -f null -

That one invocation yields two things at once. Its stderr carries a measurement line
every 100 ms holding momentary (400 ms window) and short-term (3 s window) loudness,
and its final block carries the gated integrated loudness, the loudness range and the
true peak. Both are parsed here.

Two build notes worth keeping, both found by running the thing rather than reading
about it, and both of which produce a summary with no series, which looks like a
working loudness measurement right up until you ask for the shape of the programme.

  1. `-nostats` suppresses the per-100 ms lines. The command above does not pass it.
  2. On ffmpeg 9.0.1, `ebur128=framelog=verbose` prints only the summary block. The
     per-frame lines come from the default log level, so framelog is left unset.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import spec


class ToolMissing(RuntimeError):
    """ffmpeg or ffprobe is not on PATH."""


class MeasurementFailed(RuntimeError):
    """ffmpeg ran but produced no usable loudness measurement."""


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ToolMissing(f"{cmd[0]} is not on PATH") from exc


# --- containers -----------------------------------------------------------


@dataclass
class Sample:
    """One 100 ms ebur128 measurement line."""

    t: float
    momentary_lufs: float
    short_term_lufs: float

    @property
    def gated(self) -> bool:
        """True when this sample is above the -70 LUFS absolute gate.

        Silence between reels is not a loudness defect and must not consume error
        budget, otherwise a title with a long black head slate fails for being
        quiet where it is supposed to be quiet.
        """
        return self.short_term_lufs > spec.ABSOLUTE_GATE_LUFS

    @property
    def in_corridor(self) -> bool:
        floor, ceiling = spec.corridor()
        return floor <= self.short_term_lufs <= ceiling


@dataclass
class Breach:
    """A contiguous run of out-of-corridor samples, as a timecode span."""

    start: float
    end: float
    worst_lufs: float
    direction: str

    @property
    def duration(self) -> float:
        return max(self.end - self.start, spec.SAMPLE_INTERVAL_SECONDS)

    @property
    def timecode(self) -> str:
        return timecode(self.start)

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "worst_lufs": round(self.worst_lufs, 1),
            "direction": self.direction,
            "timecode": self.timecode,
        }


@dataclass
class Measurement:
    """Everything one ffmpeg pass over one title produced."""

    title_id: str
    title: str
    source: str
    command: str
    window_seconds: int | None
    duration_seconds: float | None
    integrated_lufs: float
    true_peak_dbfs: float | None
    lra_lu: float | None
    samples: list[Sample] = field(default_factory=list)
    subtitle_findings: list[dict] = field(default_factory=list)

    # --- SLO arithmetic ---------------------------------------------------

    @property
    def gated_samples(self) -> list[Sample]:
        return [s for s in self.samples if s.gated]

    @property
    def gated_seconds(self) -> float:
        return len(self.gated_samples) * spec.SAMPLE_INTERVAL_SECONDS

    @property
    def out_of_corridor_samples(self) -> list[Sample]:
        return [s for s in self.gated_samples if not s.in_corridor]

    @property
    def burned_seconds(self) -> float:
        return len(self.out_of_corridor_samples) * spec.SAMPLE_INTERVAL_SECONDS

    @property
    def budget_seconds(self) -> float:
        return spec.error_budget_seconds(self.gated_seconds)

    @property
    def burn_ratio(self) -> float:
        budget = self.budget_seconds
        if budget <= 0:
            return 0.0 if self.burned_seconds == 0 else float("inf")
        return self.burned_seconds / budget

    @property
    def availability(self) -> float:
        total = len(self.gated_samples)
        if not total:
            return 1.0
        return (total - len(self.out_of_corridor_samples)) / total

    @property
    def integrated_delta_lu(self) -> float:
        return self.integrated_lufs - spec.EBU_R128_TARGET_LUFS

    @property
    def severity(self) -> spec.Severity:
        return spec.severity_for(self.burn_ratio, self.integrated_delta_lu)

    @property
    def unmeasured(self) -> list[str]:
        """Delivery criteria this pass could not evaluate at all.

        This list exists because of the failure mode it prevents. `in_spec` used to skip
        the true peak check when `true_peak_dbfs` was None, so a pass that produced no
        true peak figure, which is what happens the moment `peak=true` is dropped from
        the filter, reported a title as meeting a ceiling nobody had measured. Absence
        rendered as success, on the one number that decides whether a master clips.

        A criterion that could not be evaluated is therefore never a pass. It appears
        here, it makes `in_spec` false, and it travels into the run record and onto the
        panel by name, so an operator reads NOT MEASURED rather than a green tick.
        """
        gaps: list[str] = []
        if self.true_peak_dbfs is None:
            gaps.append(
                "true_peak: ebur128 returned no true peak block, so the "
                f"{spec.TRUE_PEAK_CEILING_DBTP:.0f} dBTP ceiling was not checked. The "
                "usual cause is the peak=true option missing from the filter."
            )
        if self.lra_lu is None:
            gaps.append(
                "loudness_range: ebur128 returned no LRA figure, so nothing constrains "
                "whether a repair flattened the dynamics of this title."
            )
        if not self.samples:
            gaps.append("corridor: no 100 ms series, so error budget burn is unknown.")
        elif not self.gated_samples:
            gaps.append(
                "corridor: every sample sits below the absolute gate, so no programme "
                "time was available to measure against the corridor."
            )
        return gaps

    @property
    def in_spec(self) -> bool:
        """The full delivery verdict: integrated, true peak, corridor and captions.

        False when any criterion is unmeasured. A delivery check that passes a title on
        the strength of a number it never obtained is worse than one that fails it.
        """
        if self.unmeasured:
            return False
        if abs(self.integrated_delta_lu) > spec.EBU_R128_TOLERANCE_LU:
            return False
        if self.true_peak_dbfs is not None and self.true_peak_dbfs > spec.TRUE_PEAK_CEILING_DBTP:
            return False
        if self.burn_ratio > 1.0:
            return False
        return not [f for f in self.subtitle_findings if not f["passed"]]

    def breaches(self, min_duration: float = 0.5) -> list[Breach]:
        """Out-of-corridor spans, longest burn first.

        A single 100 ms excursion is a transient, not a delivery defect, so spans
        shorter than min_duration are folded out. They still count against the
        error budget, because they are still programme time outside the corridor.
        """
        floor, ceiling = spec.corridor()
        spans: list[Breach] = []
        current: Breach | None = None
        for sample in self.samples:
            if not sample.gated or sample.in_corridor:
                if current is not None:
                    spans.append(current)
                    current = None
                continue
            direction = "loud" if sample.short_term_lufs > ceiling else "quiet"
            if current is None or current.direction != direction:
                if current is not None:
                    spans.append(current)
                current = Breach(
                    start=sample.t,
                    end=sample.t + spec.SAMPLE_INTERVAL_SECONDS,
                    worst_lufs=sample.short_term_lufs,
                    direction=direction,
                )
                continue
            current.end = sample.t + spec.SAMPLE_INTERVAL_SECONDS
            if direction == "loud":
                current.worst_lufs = max(current.worst_lufs, sample.short_term_lufs)
            else:
                current.worst_lufs = min(current.worst_lufs, sample.short_term_lufs)
        if current is not None:
            spans.append(current)

        kept = [s for s in spans if s.duration >= min_duration]
        kept.sort(key=lambda s: s.duration, reverse=True)
        return kept

    def series_csv(self, max_rows: int = 900) -> str:
        """The measured series as CSV, decimated so a panel query stays sane.

        Decimation keeps the extreme sample of each bucket rather than the first,
        because a mean would erase the excursion that caused the breach, and a
        panel that hides the defect is worse than no panel.
        """
        rows = self.samples
        if not rows:
            return "t_seconds,short_term_lufs,momentary_lufs\n"
        stride = max(1, len(rows) // max_rows)
        out = ["t_seconds,short_term_lufs,momentary_lufs"]
        target = spec.EBU_R128_TARGET_LUFS
        for i in range(0, len(rows), stride):
            bucket = rows[i : i + stride]
            pick = max(bucket, key=lambda s: abs(s.short_term_lufs - target))
            out.append(
                f"{pick.t:.3f},{pick.short_term_lufs:.2f},{pick.momentary_lufs:.2f}"
            )
        return "\n".join(out) + "\n"

    def as_dict(self) -> dict:
        return {
            "title_id": self.title_id,
            "title": self.title,
            "source": self.source,
            "command": self.command,
            "window_seconds": self.window_seconds,
            "duration_seconds": self.duration_seconds,
            "integrated_lufs": round(self.integrated_lufs, 1),
            "integrated_delta_lu": round(self.integrated_delta_lu, 1),
            "true_peak_dbfs": None if self.true_peak_dbfs is None else round(self.true_peak_dbfs, 1),
            "lra_lu": None if self.lra_lu is None else round(self.lra_lu, 1),
            "samples": len(self.samples),
            "gated_seconds": round(self.gated_seconds, 1),
            "burned_seconds": round(self.burned_seconds, 1),
            "budget_seconds": round(self.budget_seconds, 1),
            "burn_ratio": None if self.burn_ratio == float("inf") else round(self.burn_ratio, 2),
            "availability": round(self.availability, 5),
            "severity": self.severity.name,
            "in_spec": self.in_spec,
            "unmeasured": self.unmeasured,
            "breaches": [b.as_dict() for b in self.breaches()[:12]],
            "subtitle_findings": self.subtitle_findings,
        }


def timecode(seconds: float) -> str:
    """hh:mm:ss.mmm, the form a conform operator can type into an NLE."""
    if seconds < 0:
        seconds = 0.0
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


# --- ffmpeg parsing -------------------------------------------------------

_FRAME = re.compile(
    r"t:\s*(?P<t>[\d.]+)\s+TARGET:\s*-?\d+\s*LUFS\s+"
    r"M:\s*(?P<m>-?[\d.]+|-inf)\s+S:\s*(?P<s>-?[\d.]+|-inf)"
)
_INTEGRATED = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|-inf)\s*LUFS")
_TRUE_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS")
_LRA = re.compile(r"LRA:\s*(-?[\d.]+)\s*LU\s*\n\s*Threshold")


def _to_float(raw: str) -> float:
    return float("-inf") if raw.strip() == "-inf" else float(raw)


def probe_duration(path: str | Path) -> float | None:
    out = _run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        timeout=120,
    )
    try:
        return float(out.stdout.strip())
    except (TypeError, ValueError):
        return None


def loudness_command(path: str | Path, seconds: int | None) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-af", "ebur128=peak=true", "-f", "null", "-"]
    return cmd


def measure(
    path: str | Path,
    *,
    title_id: str,
    title: str,
    seconds: int | None = None,
    subtitle_text: str | None = None,
) -> Measurement:
    """One ffmpeg pass, parsed into a Measurement.

    Raises rather than degrading. A title with no parseable loudness has not been
    measured, and reporting it as clear would be the worst possible failure mode
    for a delivery QC tool.
    """
    cmd = loudness_command(path, seconds)
    result = _run(cmd)
    text = result.stderr

    samples: list[Sample] = []
    for match in _FRAME.finditer(text):
        m = _to_float(match.group("m"))
        s = _to_float(match.group("s"))
        # -inf sorts below the gate, so it is retained and gated out later rather
        # than dropped: the sample still happened and still occupies programme time.
        samples.append(
            Sample(
                t=float(match.group("t")),
                momentary_lufs=m if m != float("-inf") else -120.7,
                short_term_lufs=s if s != float("-inf") else -120.7,
            )
        )

    integrated_match = _INTEGRATED.search(text)
    if not integrated_match:
        raise MeasurementFailed(
            f"ebur128 produced no integrated loudness for {path}. "
            f"ffmpeg exit {result.returncode}, stderr tail: {text.strip()[-300:]}"
        )
    integrated = _to_float(integrated_match.group(1))
    if integrated == float("-inf"):
        raise MeasurementFailed(
            f"{path} measured -inf LUFS: the file carries no programme audio, "
            "so there is nothing to hold to a loudness spec."
        )

    peak_match = _TRUE_PEAK.search(text)
    lra_match = _LRA.search(text)

    findings: list[dict] = []
    if subtitle_text:
        findings = subtitle_findings(subtitle_text)

    if not samples:
        raise MeasurementFailed(
            f"ebur128 returned a summary for {path} but no 100 ms series. "
            "The most common cause is -nostats being passed to ffmpeg, which "
            "suppresses the per-frame lines the SLO is computed from."
        )

    if not [s for s in samples if s.gated]:
        raise MeasurementFailed(
            f"every sample in {path} sits below the {spec.ABSOLUTE_GATE_LUFS:.0f} LUFS "
            "absolute gate, so the window carries no programme audio. An SLO over an "
            "empty measurement would report 100 percent availability, which is the "
            "one answer a delivery check must never give for a silent master."
        )

    return Measurement(
        title_id=title_id,
        title=title,
        source=str(path),
        command=" ".join(cmd),
        window_seconds=seconds,
        duration_seconds=probe_duration(path),
        integrated_lufs=integrated,
        true_peak_dbfs=_to_float(peak_match.group(1)) if peak_match else None,
        lra_lu=float(lra_match.group(1)) if lra_match else None,
        samples=samples,
        subtitle_findings=findings,
    )


# --- captions -------------------------------------------------------------

_CUE_TIME = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
)


def parse_srt(text: str) -> list[dict]:
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n+", text.strip()):
        lines = [line for line in block.strip().splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        stamp = next((line for line in lines if _CUE_TIME.search(line)), None)
        if stamp is None:
            continue
        match = _CUE_TIME.search(stamp)
        body = lines[lines.index(stamp) + 1 :]
        if not body:
            continue

        def _t(raw: str) -> float:
            hh, mm, rest = raw.replace(",", ".").split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(rest)

        cues.append(
            {
                "start": _t(match.group(1)),
                "end": _t(match.group(2)),
                "lines": body,
                "text": " ".join(body),
            }
        )
    return cues


def subtitle_findings(srt_text: str) -> list[dict]:
    cues = parse_srt(srt_text)
    if not cues:
        return []

    fast: list[dict] = []
    short: list[dict] = []
    wide: list[dict] = []
    for cue in cues:
        duration = max(cue["end"] - cue["start"], 0.001)
        visible = re.sub(r"<[^>]+>", "", cue["text"])
        chars = len(visible.replace(" ", ""))
        cps = chars / duration
        if cps > spec.NETFLIX_MAX_CPS:
            fast.append({"start": cue["start"], "cps": round(cps, 1)})
        if duration < spec.NETFLIX_MIN_CUE_SECONDS:
            short.append({"start": cue["start"], "duration": round(duration, 3)})
        for line in cue["lines"]:
            plain = re.sub(r"<[^>]+>", "", line)
            if len(plain) > spec.NETFLIX_MAX_LINE_CHARS:
                wide.append({"start": cue["start"], "chars": len(plain)})
                break

    total = len(cues)
    return [
        {
            "check": "subtitle_reading_speed",
            "spec": spec.SPEC_CITATIONS["subtitle_speed"],
            "passed": not fast,
            "measured": len(fast),
            "unit": "cues",
            "detail": f"{len(fast)} of {total} cues exceed {spec.NETFLIX_MAX_CPS:.0f} chars/sec",
            "worst": fast[:3],
        },
        {
            "check": "subtitle_min_duration",
            "spec": spec.SPEC_CITATIONS["subtitle_duration"],
            "passed": not short,
            "measured": len(short),
            "unit": "cues",
            "detail": f"{len(short)} of {total} cues held under {spec.NETFLIX_MIN_CUE_SECONDS:.2f}s",
            "worst": short[:3],
        },
        {
            "check": "subtitle_line_length",
            "spec": spec.SPEC_CITATIONS["subtitle_line"],
            "passed": not wide,
            "measured": len(wide),
            "unit": "cues",
            "detail": f"{len(wide)} of {total} cues carry a line over {spec.NETFLIX_MAX_LINE_CHARS} chars",
            "worst": wide[:3],
        },
    ]
