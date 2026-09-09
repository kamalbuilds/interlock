"""Repair a master with ffmpeg, then prove it by measuring the result.

This is the step the rest of the field stops short of. An observability agent files a
breach and hands it to a person. Interlock changes the file, measures the file it
changed, and only then closes its own alert.

The repair is ffmpeg's two-pass `loudnorm`. Pass one measures the source and prints
the offsets it would apply. Pass two applies exactly those offsets, which is what
makes the result deterministic rather than a single-pass approximation that drifts
with the analysis window. The commands are recorded verbatim so a judge can run them
on the same archive.org file and land on the same number.

Three guardrails, all enforced here rather than trusted upstream:

  1. ffmpeg writes only under this project's `work/` directory. `_safe_output`
     refuses any destination outside it, so no repair can ever touch a source
     download, another project, or anything a caller passes in by mistake.
  2. the source is opened read-only and never written. A restoration house's master
     is the one artefact that must survive the tool.
  3. success is the re-measured number, not the exit code. `RepairResult.landed` is
     computed from a second ffmpeg pass over the output file. A loudnorm run that
     exits zero and moves nothing leaves `landed` false, and the caller must leave
     the alert open.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import spec
from .measure import Measurement, ToolMissing, _run, measure

WORK_ROOT = Path(__file__).resolve().parent.parent / "work"


class UnsafeOutput(RuntimeError):
    """A repair tried to write outside the project workdir."""


class RepairFailed(RuntimeError):
    """ffmpeg could not produce a repaired file at all."""


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def _safe_output(path: Path) -> Path:
    """Refuse any destination that is not inside this project's work directory."""
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    root = WORK_ROOT.resolve()
    if root != resolved and root not in resolved.parents:
        raise UnsafeOutput(
            f"repair output {resolved} is outside {root}. ffmpeg in Interlock writes "
            "only under the project work directory, so a repair can never overwrite "
            "a source master or reach into another project."
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@dataclass
class RepairResult:
    """What the repair did, and whether the second measurement says it worked."""

    title_id: str
    source: Path
    output: Path
    analysis_command: str
    apply_command: str
    measured_offsets: dict
    before: Measurement
    after: Measurement

    @property
    def delta_before(self) -> float:
        return self.before.integrated_delta_lu

    @property
    def delta_after(self) -> float:
        return self.after.integrated_delta_lu

    @property
    def blocked_by(self) -> list[str]:
        """Every reason the re-measured file is not fit to close a record on.

        Three kinds of reason, and the third is the one that matters most. The level can
        still be wrong. The true peak can be over the ceiling, which a level-only gate
        used to wave through: `loudnorm` raising a quiet master by 6 LU can push its
        peaks over -1.0 dBTP, and a record closed on that has swapped one rejection for
        another. And a criterion can be UNMEASURED, which is not a pass. A repaired file
        whose true peak could not be read has not been shown to meet the ceiling, and an
        alert closed on a check that could not fail is the worst outcome available to a
        product whose whole claim is that it closes on a second measurement.
        """
        reasons: list[str] = []
        if abs(self.delta_after) > spec.EBU_R128_TOLERANCE_LU:
            reasons.append(
                f"integrated loudness is {self.delta_after:+.1f} LU from the "
                f"{spec.EBU_R128_TARGET_LUFS:.0f} LUFS target, outside the "
                f"{spec.EBU_R128_TOLERANCE_LU:.0f} LU tolerance"
            )
        peak = self.after.true_peak_dbfs
        if peak is not None and peak > spec.TRUE_PEAK_CEILING_DBTP:
            reasons.append(
                f"true peak of the repaired file is {peak:.1f} dBTP, over the "
                f"{spec.TRUE_PEAK_CEILING_DBTP:.0f} dBTP ceiling"
            )
        reasons.extend(f"NOT MEASURED, {gap}" for gap in self.after.unmeasured)
        return reasons

    @property
    def landed(self) -> bool:
        """True only when the re-measured file is provably inside the delivery spec.

        Deliberately not "the command exited zero" and deliberately not "the number
        improved". A repair that moves a title from 12 LU out to 4 LU out has not
        delivered it, and an alert closed on an improvement is an alert closed on a file
        that will be rejected again. Also not "the level is right": see `blocked_by`.
        """
        return not self.blocked_by

    @property
    def improvement_lu(self) -> float:
        return abs(self.delta_before) - abs(self.delta_after)

    @property
    def note(self) -> str:
        return (
            f"{self.apply_command}  ->  re-measured "
            f"{self.after.integrated_lufs:.1f} LUFS ({self.delta_after:+.1f} LU from target), "
            f"moved {self.improvement_lu:+.1f} LU"
        )

    def as_dict(self) -> dict:
        return {
            "title_id": self.title_id,
            "source": str(self.source),
            "output": str(self.output),
            "analysis_command": self.analysis_command,
            "apply_command": self.apply_command,
            "measured_offsets": self.measured_offsets,
            "integrated_lufs_before": round(self.before.integrated_lufs, 1),
            "integrated_lufs_after": round(self.after.integrated_lufs, 1),
            "delta_lu_before": round(self.delta_before, 1),
            "delta_lu_after": round(self.delta_after, 1),
            "improvement_lu": round(self.improvement_lu, 1),
            "availability_before": round(self.before.availability, 5),
            "availability_after": round(self.after.availability, 5),
            "true_peak_before": self.before.true_peak_dbfs,
            "true_peak_after": self.after.true_peak_dbfs,
            "landed": self.landed,
            "blocked_by": self.blocked_by,
            "unmeasured_after": self.after.unmeasured,
            "in_spec_after": self.after.in_spec,
            "note": self.note,
        }


def analyse(source: Path, seconds: int | None) -> tuple[dict, str]:
    """Pass one: ask loudnorm what it would change, without writing anything."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(source)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += [
        "-af",
        f"loudnorm=I={spec.EBU_R128_TARGET_LUFS}:TP={spec.TRUE_PEAK_CEILING_DBTP}:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    result = _run(cmd)
    match = _LOUDNORM_JSON.search(result.stderr)
    if not match:
        raise RepairFailed(
            f"loudnorm analysis produced no measurement block for {source}. "
            f"ffmpeg exit {result.returncode}, stderr tail: {result.stderr.strip()[-300:]}"
        )
    import json as _json

    return _json.loads(match.group(0)), " ".join(cmd)


def apply_offsets(source: Path, output: Path, offsets: dict, seconds: int | None) -> str:
    """Pass two: apply the offsets pass one measured, and write the repaired audio."""
    output = _safe_output(output)
    if output.exists():
        output.unlink()
    filt = (
        "loudnorm="
        f"I={spec.EBU_R128_TARGET_LUFS}:TP={spec.TRUE_PEAK_CEILING_DBTP}:LRA=11:"
        f"measured_I={offsets['input_i']}:"
        f"measured_LRA={offsets['input_lra']}:"
        f"measured_TP={offsets['input_tp']}:"
        f"measured_thresh={offsets['input_thresh']}:"
        f"offset={offsets['target_offset']}:"
        "linear=true:print_format=summary"
    )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(source)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-map", "0:a:0", "-af", filt, "-c:a", "pcm_s24le", "-ar", "48000", str(output)]
    result = _run(cmd)
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        raise RepairFailed(
            f"loudnorm apply failed for {source}: exit {result.returncode}, "
            f"stderr tail: {result.stderr.strip()[-300:]}"
        )
    return " ".join(cmd)


def repair(before: Measurement, *, title_id: str, title: str) -> RepairResult:
    """Run the two-pass repair over the measured window and measure the result.

    The window matches the measurement window on purpose. Repairing 300 s and then
    reporting a figure measured over 300 s is a claim about the same span; repairing
    the whole feature and reporting the prefix would not be.
    """
    if shutil.which("ffmpeg") is None:
        raise ToolMissing("ffmpeg is not on PATH")

    source = Path(before.source)
    if not source.exists():
        raise RepairFailed(f"source master {source} is gone, nothing to repair")

    output = WORK_ROOT / title_id / "repaired.wav"
    offsets, analysis_command = analyse(source, before.window_seconds)
    apply_command = apply_offsets(source, output, offsets, before.window_seconds)

    after = measure(
        output,
        title_id=title_id,
        title=title,
        seconds=before.window_seconds,
        subtitle_text=None,
    )
    # The caption findings are carried over rather than re-run, and that is correct here
    # for one specific reason: `apply_offsets` maps `0:a:0` and writes audio only, so the
    # repaired artefact has no subtitle track to measure. Re-measuring it would return
    # zero caption failures on a file with no captions, which is absence rendered as
    # success. The findings that travel are the ones measured on the master's own sidecar,
    # and a loudnorm pass cannot change them.
    after.subtitle_findings = before.subtitle_findings

    return RepairResult(
        title_id=title_id,
        source=source,
        output=output,
        analysis_command=analysis_command,
        apply_command=apply_command,
        measured_offsets=offsets,
        before=before,
        after=after,
    )
