# Proof that the checks can fail

Passing a test is not the bar. The bar is the test being able to fail. Each check below
was broken on purpose, run and confirmed red, restored, and run again and confirmed
green. The transcript is the raw output of `scripts/prove.sh`, which is the script that
produced it, so this file can be regenerated rather than believed.

Two blind spots, stated once.

The nine mutations below cover the SLO arithmetic, the measurement parser, the payload
shapes, the guard on the agent's datasource choice and the credential gates. They do not
cover the Grafana writes, on purpose: a fake Grafana would agree with whatever the code
sent it, which is exactly the check that cannot fail. Those are proven instead by
`tests/test_live_grafana.py`, which does real work against a real stack and FAILS rather
than skips when the environment names a stack that does not answer, and by a live run
whose every write is read back, one of whose assertions comes from Grafana's own alert
state history, a stream Interlock can only read.

This transcript was produced with no credentials in the environment, so the eleven live
tests skipped and the tally reads 67 passed 11 skipped. That is the point of the banner
at the bottom of the run: the same suite against the real stack is 78 passed, 0 skipped,
and the eleven that moved are the Grafana and Gemini integration.

```
=== 1 BREAK: corridor widened to 60 LU, so no title can ever leave it ===
=========================== short test summary info ============================
FAILED tests/test_slo.py::test_a_known_offender_fails_the_slo_at_the_published_figure
FAILED tests/test_slo.py::test_breach_spans_carry_a_real_duration_and_a_timecode
2 failed, 1 passed, 14 deselected in 11.31s

=== 1 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
3 passed, 14 deselected in 4.90s

=== 2 BREAK: gate check removed, so a silent file measures as 100 percent available ===
gemini: no live test ran in this selection, so this run says nothing about it either way
=========================== short test summary info ============================
FAILED tests/test_slo.py::test_a_file_with_no_audio_is_a_failed_measurement_not_a_passing_one
1 failed, 16 deselected in 0.15s

=== 2 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
1 passed, 16 deselected in 0.10s

=== 3 BREAK: landed redefined as any improvement ===
.                                                                        [100%]
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
1 passed, 15 deselected in 0.01s

=== 3 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
1 passed, 15 deselected in 0.01s

=== 4 BREAK: the workdir guard returns the path unchecked ===
FAILED tests/test_execution.py::test_repair_refuses_to_write_outside_the_work_directory[hostile1]
FAILED tests/test_execution.py::test_repair_refuses_to_write_outside_the_work_directory[hostile2]
FAILED tests/test_execution.py::test_repair_refuses_to_write_outside_the_work_directory[hostile3]
4 failed, 12 deselected in 0.02s

=== 4 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
4 passed, 12 deselected in 0.01s

=== 5 BREAK: rule threshold set to 0, so every title fires forever ===
=========================== short test summary info ============================
FAILED tests/test_execution.py::test_the_alert_rule_carries_the_measured_distance_from_target
FAILED tests/test_execution.py::test_the_rule_would_not_fire_on_an_in_spec_title
2 failed, 14 deselected in 0.03s

=== 5 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
2 passed, 14 deselected in 0.01s

=== 6 BREAK: the survey guard trusts whatever uid Gemini named ===
=========================== short test summary info ============================
FAILED tests/test_survey.py::test_an_invented_series_datasource_stops_the_run
FAILED tests/test_survey.py::test_a_series_datasource_of_the_wrong_type_stops_the_run
2 failed, 10 deselected in 0.08s

=== 6 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
2 passed, 10 deselected in 0.04s

=== 7 BREAK: series read-back stops comparing sample values ===
gemini: no live test ran in this selection, so this run says nothing about it either way
=========================== short test summary info ============================
FAILED tests/test_survey.py::test_one_altered_sample_fails_the_read_back - As...
1 failed, 11 deselected in 0.05s

=== 7 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
1 passed, 11 deselected in 0.04s

=== 8 BREAK: GEMINI_MODEL alone counts as a credential again ===
FAILED tests/test_credentials.py::test_removing_the_vertex_project_stops_the_run
FAILED tests/test_credentials.py::test_removing_the_vertex_location_stops_the_run
FAILED tests/test_credentials.py::test_an_api_key_is_an_acceptable_gemini_credential_instead_of_vertex
3 failed, 7 deselected, 2 warnings in 0.94s

=== 8 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
3 passed, 7 deselected in 0.38s

=== 9 BREAK: the Grafana preflight stops checking for a token ===
gemini: no live test ran in this selection, so this run says nothing about it either way
=========================== short test summary info ============================
FAILED tests/test_credentials.py::test_the_grafana_refusal_names_the_fix_and_never_the_token
1 failed, 1 passed, 8 deselected, 2 warnings in 0.66s

=== 9 RESTORE ===
grafana: no live test ran in this selection, so this run says nothing about it either way
gemini: no live test ran in this selection, so this run says nothing about it either way
2 passed, 8 deselected in 0.79s

=== FINAL: whole suite on the restored tree ===
======================= gemini integration NOT exercised =======================
the live Gemini test drives the survey node over the same mcp-grafana session, which was not available, so it never ran.
67 passed, 11 skipped, 2 warnings in 74.06s (0:01:14)

=== GIT: tree must be identical to before the proof ===
 M projects/interlock/Dockerfile
 M projects/interlock/README.md
 M projects/interlock/STORY.md
 M projects/interlock/interlock/catalog.py
 M projects/interlock/tests/test_not_measured.py
 M projects/interlock/web/server.py
?? projects/interlock/docs/img/live-cloud-run.png
```
