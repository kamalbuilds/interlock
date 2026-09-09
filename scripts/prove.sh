set -u
cd /Users/kamal/Desktop/win/projects/interlock
PY=./.venv/bin/python
say(){ printf "\n=== %s ===\n" "$1"; }

# 1. SLO can come back green: widen the corridor so nothing can ever breach.
say "1 BREAK: corridor widened to 60 LU, so no title can ever leave it"
/usr/bin/sed -i '' 's/^CORRIDOR_LU = 8.0$/CORRIDOR_LU = 60.0/' interlock/spec.py
$PY -m pytest tests/test_slo.py -q -k "offender or breach_spans or decimated" 2>&1 | tail -4
say "1 RESTORE"
/usr/bin/sed -i '' 's/^CORRIDOR_LU = 60.0$/CORRIDOR_LU = 8.0/' interlock/spec.py
$PY -m pytest tests/test_slo.py -q -k "offender or breach_spans or decimated" 2>&1 | tail -3

# 2. Silent master must not read as clean.
say "2 BREAK: gate check removed, so a silent file measures as 100 percent available"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/measure.py"); s = p.read_text()
s = s.replace('    if not [s for s in samples if s.gated]:', '    if False:')
p.write_text(s)
PYEOF
$PY -m pytest tests/test_slo.py -q -k "no_audio" 2>&1 | tail -4
say "2 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/measure.py"); s = p.read_text()
s = s.replace('    if False:', '    if not [s for s in samples if s.gated]:')
p.write_text(s)
PYEOF
$PY -m pytest tests/test_slo.py -q -k "no_audio" 2>&1 | tail -3

# 3. Repair gate must be the number, not an improvement.
say "3 BREAK: landed redefined as any improvement"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/repair.py"); s = p.read_text()
s = s.replace("        return abs(self.delta_after) <= spec.EBU_R128_TOLERANCE_LU",
              "        return abs(self.delta_after) < abs(self.delta_before)")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_execution.py -q -k "landed" 2>&1 | tail -4
say "3 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/repair.py"); s = p.read_text()
s = s.replace("        return abs(self.delta_after) < abs(self.delta_before)",
              "        return abs(self.delta_after) <= spec.EBU_R128_TOLERANCE_LU")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_execution.py -q -k "landed" 2>&1 | tail -3

# 4. ffmpeg must not escape the work directory.
say "4 BREAK: the workdir guard returns the path unchecked"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/repair.py"); s = p.read_text()
s = s.replace("    if root != resolved and root not in resolved.parents:",
              "    if False:")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_execution.py -q -k "outside_the_work" 2>&1 | tail -4
say "4 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/repair.py"); s = p.read_text()
s = s.replace("    if False:", "    if root != resolved and root not in resolved.parents:")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_execution.py -q -k "outside_the_work" 2>&1 | tail -3

# 5. The alert threshold must be able to come back inactive.
say "5 BREAK: rule threshold set to 0, so every title fires forever"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/grafana/surfaces.py"); s = p.read_text()
s = s.replace('"params": [spec.EBU_R128_TOLERANCE_LU]', '"params": [0.0]')
p.write_text(s)
PYEOF
$PY -m pytest tests/test_execution.py -q -k "rule" 2>&1 | tail -4
say "5 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/grafana/surfaces.py"); s = p.read_text()
s = s.replace('"params": [0.0]', '"params": [spec.EBU_R128_TOLERANCE_LU]')
p.write_text(s)
PYEOF
$PY -m pytest tests/test_execution.py -q -k "rule" 2>&1 | tail -3

# 6. The guard on the survey node's datasource choice must actually read the uid back.
say "6 BREAK: the survey guard trusts whatever uid Gemini named"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/agent/survey.py"); s = p.read_text()
s = s.replace("        ok = status == 200 and isinstance(body, dict) and body.get(\"uid\") == uid",
              "        ok = True")
s = s.replace("    if series[\"type\"] != \"yesoreyeram-infinity-datasource\":",
              "    if False:")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_survey.py -q -k "invented or wrong_type" 2>&1 | tail -4
say "6 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/agent/survey.py"); s = p.read_text()
s = s.replace("        ok = True",
              "        ok = status == 200 and isinstance(body, dict) and body.get(\"uid\") == uid")
s = s.replace("    if False:",
              "    if series[\"type\"] != \"yesoreyeram-infinity-datasource\":")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_survey.py -q -k "invented or wrong_type" 2>&1 | tail -3

# 7. The series read-back must compare the numbers, not just count the rows.
say "7 BREAK: series read-back stops comparing sample values"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/grafana/surfaces.py"); s = p.read_text()
s = s.replace("""    mismatches = [
        i
        for i, (want, have) in enumerate(zip(expected, got))
        if abs(want[1] - have[1]) > 0.001 or want[0] != have[0]
    ]""", "    mismatches = []")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_survey.py -q -k "altered_sample" 2>&1 | tail -4
say "7 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/grafana/surfaces.py"); s = p.read_text()
s = s.replace("    mismatches = []", """    mismatches = [
        i
        for i, (want, have) in enumerate(zip(expected, got))
        if abs(want[1] - have[1]) > 0.001 or want[0] != have[0]
    ]""")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_survey.py -q -k "altered_sample" 2>&1 | tail -3

# 8. Removing the Gemini credentials must stop the run, not degrade it.
say "8 BREAK: GEMINI_MODEL alone counts as a credential again"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/agent/pipeline.py"); s = p.read_text()
s = s.replace("    vertex = os.getenv(\"GOOGLE_GENAI_USE_VERTEXAI\", \"\").strip().lower() in (\"1\", \"true\", \"yes\")",
              "    return model\n    vertex = os.getenv(\"GOOGLE_GENAI_USE_VERTEXAI\", \"\").strip().lower() in (\"1\", \"true\", \"yes\")")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_credentials.py -q -k "vertex" 2>&1 | tail -4
say "8 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/agent/pipeline.py"); s = p.read_text()
s = s.replace("    return model\n    vertex = os.getenv(\"GOOGLE_GENAI_USE_VERTEXAI\", \"\").strip().lower() in (\"1\", \"true\", \"yes\")",
              "    vertex = os.getenv(\"GOOGLE_GENAI_USE_VERTEXAI\", \"\").strip().lower() in (\"1\", \"true\", \"yes\")")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_credentials.py -q -k "vertex" 2>&1 | tail -3

# 9. Removing the Grafana token must stop the run before ffmpeg spends a minute.
say "9 BREAK: the Grafana preflight stops checking for a token"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/grafana/client.py"); s = p.read_text()
s = s.replace("    url = grafana_url()\n    if not token_present():",
              "    url = grafana_url()\n    if False:")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_credentials.py -q -k "grafana_token or grafana_refusal" 2>&1 | tail -4
say "9 RESTORE"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("interlock/grafana/client.py"); s = p.read_text()
s = s.replace("    url = grafana_url()\n    if False:",
              "    url = grafana_url()\n    if not token_present():")
p.write_text(s)
PYEOF
$PY -m pytest tests/test_credentials.py -q -k "grafana_token or grafana_refusal" 2>&1 | tail -3

say "FINAL: whole suite on the restored tree"
$PY -m pytest tests/ -q 2>&1 | tail -3
say "GIT: tree must be identical to before the proof"
git -C /Users/kamal/Desktop/win status --porcelain projects/interlock | head
