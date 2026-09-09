# Interlock

**A film archive as a fleet under SLO.** Every title is a service, the delivery spec is
its objective, and every second of programme audio outside the loudness corridor burns
error budget. Interlock measures the masters with ffmpeg, files the breach into Grafana
Cloud through the official `grafana/mcp-grafana` server, repairs the master, measures
the master it repaired, and closes its own alert on the second number.

Live: **https://interlock-yfaenbgx7q-uc.a.run.app**

Two things about it are worth being precise about, because both are unusual.

**The series on the Grafana panel is the film's own loudness, not the CPU of a render
node.** One sample every 100 milliseconds, straight out of ffmpeg's `ebur128` filter,
over public-domain features anyone can download from archive.org and re-measure.

**The agent closes the loop instead of escalating out of it.** It runs an ffmpeg
`loudnorm` pass, writes a new file, measures that file, and resolves the alert only when
the second measurement says the master is inside spec. A `loudnorm` run that exits zero
and moves nothing leaves the alert open.

![The film's own loudness, one sample per 100 ms](docs/img/interlock-film-loudness-series.png)

---

## What one run does

```
START -> survey -> probe -> measure_fleet -> triage -> file -> repair -> review -> close -> publish
                                                        |                                    ^
                                                        +---- hold --------------------------+
```

A `google.adk.workflow.Workflow` graph. Six function nodes, three `LlmAgent` nodes, one
conditional branch taken on a structured field, so the shape of the run is reproducible
while the judgement inside it is not.

| node | what it does | proof it did it |
|---|---|---|
| `survey` | **Gemini**, holding an ADK `McpToolset` over mcp-grafana. Discovers the stack's datasources and picks the ones this run writes to | Every uid it names is read back from Grafana before anything is written, and one that does not resolve stops the run |
| `probe` | Attempts every Grafana write for real against a scratch folder and dashboard | Each capability carries the verbatim acceptance or refusal |
| `measure_fleet` | ffmpeg `ebur128` over each title, 100 ms momentary and 3 s short-term, plus caption checks | The exact command is printed beside every figure |
| `triage` | **Gemini.** Ranks the fleet, picks the title to escalate, decides whether one gain change delivers it, nominates a repair target | Structured output validated against a Pydantic schema |
| `file` | `create_folder`, `update_dashboard`, `create_annotation` per breach span, `alerting_manage_rules` create | Dashboard read back by uid, its 100 ms series compared against ffmpeg sample by sample, annotations found again by a unique tag, ruler polled until Grafana evaluates the rule to `firing` |
| `repair` | Two-pass ffmpeg `loudnorm`, then a third pass that measures the written file | Before and after integrated loudness, both measured |
| `review` | **Gemini.** Can withhold closure. Cannot grant it | Reasoning cites the before and after numbers |
| `close` | Writes the re-measured figure into the alert rule, waits for Grafana to clear it | Ruler polled until `inactive`, plus the transition read out of Grafana's alert state history |
| `publish` | Fleet board and the run record the web surface renders | Every assertion carries the query that established it |

Each node writes one line into the run record as its last act, so the trace on the
product surface is what executed rather than what the diagram promises.

![Which nodes ran, and what each one did](docs/img/adk-workflow-nodes.png)

## Verified end to end, on a live Grafana Cloud stack

A full eight-title run, 2026-09-09, from `python -m interlock`:

```
escalation path alert_rule
mcp calls       70
nodes executed  survey[llm] -> probe -> measure_fleet -> triage[llm] -> file
                -> repair -> review[llm] -> close -> publish
  survey        Gemini called list_datasources, query_loki_logs and chose series
                datasource grafanacloud-infinity, alert state history
                grafanacloud-alert-state-history
  probe         9 capabilities attempted for real, escalation path alert_rule
  measure_fleet 8 titles measured with ffmpeg ebur128, 11408 samples at 100 ms
  triage        Gemini queued 8 titles, escalate='memphis_belle', severity='critical',
                auto_repairable=False, repair_target='night_tide'
  file          filed ['memphis_belle', 'night_tide'] into Grafana, routed to repair
  repair        loudnorm two-pass on night_tide, re-measured -23.1 LUFS, landed=True
  review        Gemini approve=True, residual_work='none'
  close         closed=True on the re-measurement, gate: both gates passed
  publish       fleet board written, 18 assertions
post-conditions 18 of 18 hold
```

The triage line is the model earning its place. The Memphis Belle is the worst title in
the fleet and Gemini escalated it, then refused to auto-repair it, because its true peak
is already over the ceiling and a gain change would clip. It nominated a different title
for the repair, and the branch took `repair` on that one.

An edited selection of those post-conditions, each with the query behind it. The last
three are the loop closing: Grafana fires the rule on the delivered master, Interlock
writes the re-measured figure into it, and Grafana clears it.

```
PASS  the measured loudness series is readable back out of dashboard interlock-night-tide
      1201 rows came back out of Grafana and every short-term value matches the
      ffmpeg measurement to 0.001 LUFS. Quietest stored sample -120.70 LUFS.
      mcp-grafana get_dashboard_by_uid uid=interlock-night-tide, panel 2 target data,
      compared row for row against the ffmpeg ebur128 series

PASS  6 breach spans are readable back out of Grafana
      get_annotations returned 6 rows for tag run-1788972628022
      mcp-grafana get_annotations tags=[interlock,night_tide,run-1788972628022]

PASS  Grafana evaluated rule cfxr32fygp2bkc to firing
      reached firing after 52s, states seen: ['unknown', 'inactive', 'firing']
      mcp-grafana grafana_api_request GET /api/prometheus/grafana/api/v1/rules

PASS  alert rule afxr359kjzw1sa now holds the re-measured figure
      rule query data updated to delta_lu=0.100, threshold gt 1.0
      mcp-grafana alerting_manage_rules update rule_uid=afxr359kjzw1sa

PASS  Grafana evaluated rule afxr359kjzw1sa to inactive
      reached inactive after 16s, states seen: ['firing', 'inactive']
      mcp-grafana grafana_api_request GET /api/prometheus/grafana/api/v1/rules

PASS  alert rule afxr359kjzw1sa has state transitions in Grafana's own history
      states seen: ['Alerting', 'Normal']
      LogQL against grafanacloud-alert-state-history:
        {from="state-history"} | json | ruleUID=`afxr359kjzw1sa`
```

That last one is the assertion Interlock cannot fake. Grafana writes alert state
transitions into a Loki stream that Interlock only ever reads.

The same graph runs on the deployed service, not just the page it serves. POSTing two
title ids to `/api/run` on the URL above, and polling `/api/status` to a terminal state:
all nine nodes, 46 MCP calls with 13 writes, 10 of 10 post-conditions, the series read
back out of Grafana at 1,203 rows matching ffmpeg to 0.001 LUFS, the rule seen going
`firing` then `inactive`, and the record closed on the re-measurement in 2 minutes 32
seconds.

![The delivery record on Cloud Run](docs/img/live-cloud-run.png)

![The delivery record, read back out of Grafana Cloud](docs/img/grafana-delivery-record.png)

The repair, measured twice:

| | Night Tide (1961) | Fit for a King (1937) |
|---|---|---|
| integrated, as delivered | -17.4 LUFS, +5.6 LU off target | -21.0 LUFS, +2.0 LU off target |
| integrated, after repair | -23.1 LUFS, +0.1 LU off target | -22.9 LUFS, +0.1 LU off target |
| availability | 83.36% to 93.76% | 100.00% to 100.00% |
| true peak | -4.4 to -10.0 dBTP | -4.1 to -6.0 dBTP |
| record | closed, both gates passed | closed, both gates passed |

![The two gates that decide whether a record closes](docs/img/alert-closed-on-second-measurement.png)

## The fleet, and why it is not all red

Eight public-domain features from archive.org. Two of them pass the delivery spec
outright. That matters more than the six that fail: an SLO that condemns every title is
indistinguishable from a broken measurement, and a demo where nothing can come back
green proves nothing about the check.

| title | window | integrated | true peak | availability | verdict |
|---|---|---|---|---|---|
| The Memphis Belle (1944) | 120 s | -10.7 LUFS | **+0.9 dBTP** | 30.29% | critical |
| Police Station (1959) | 120 s | -25.9 LUFS | -14.9 dBTP | 72.70% | critical |
| Vicki (1953) | 300 s | -26.1 LUFS | -13.5 dBTP | 75.27% | critical |
| Werewolf In A Girls Dormitory (1961) | 120 s | -22.0 LUFS | -4.3 dBTP | 85.32% | major |
| Night Tide (1961) | 120 s | -17.4 LUFS | -4.4 dBTP | 83.36% | major |
| Fit for a King (1937) | 120 s | -21.0 LUFS | -4.1 dBTP | 100.00% | minor |
| Niagara Falls (1941) | 120 s | -22.3 LUFS | -9.3 dBTP | 100.00% | **clear** |
| Night of the Living Dead (1968) | 120 s | -24.0 LUFS | -10.1 dBTP | 99.20% | **clear** |

The Memphis Belle carries a second, different failure: its true peak is over the ceiling,
so the corpus contains two failure modes rather than eight copies of one. Gemini refuses
to auto-repair it for exactly that reason, and says so.

Reproduce any row:

```bash
curl -L -r 0-24000000 -o vicki.mp4 \
  "https://archive.org/download/vicki-1953/Vicki%20%281953%29.mp4"
ffmpeg -hide_banner -i vicki.mp4 -t 300 -af ebur128=peak=true -f null -
# Integrated loudness:  I: -26.1 LUFS
```

## The delivery spec, and which parts are ours

Provenance matters here, so `interlock/spec.py` labels every constant.

**Published standards.** EBU R128 integrated programme loudness -23.0 LUFS with a
1.0 LU tolerance, true peak ceiling -1.0 dBTP. EBU Tech 3341 momentary (400 ms) and
short-term (3 s) windows, and the -70 LUFS absolute gate. ATSC A/85 for the US CALM Act
figure. Netflix TTSS for the caption checks: 17 chars/sec, 5/6 s minimum cue, 42
characters per line.

**Interlock's own choices, stated so they are not mistaken for standards.** The corridor
is short-term loudness within 8 LU either side of target; R128 constrains the integrated
figure and the true peak but publishes no short-term corridor for long-form programme.
The objective is 99% of gated programme time inside that corridor, which for a 90 minute
feature is a 54 second budget, roughly one reel change plus a head slate.

## Where Gemini is load-bearing

There is no offline branch that produces the same result. `GEMINI_MODEL` unset, or a
Vertex project or location missing, raises before the graph is built and before ffmpeg
runs. Nine tests take one credential away each and assert the run refuses.

`survey` holds `google.adk.tools.mcp_tool.McpToolset` over the official mcp-grafana
server, filtered to that server's read tools. ADK reads the tool list off the server and
hands Gemini each tool with mcp-grafana's own JSON schema, so the model selects the tool
and shapes the arguments. Nothing in `interlock/agent/survey.py` decides which call gets
made. It picks the datasource that carries the 100 ms series, finds Grafana's own alert
state history, and proves that one answers by querying it. Those uids are the uids every
later write goes to.

![Gemini discovering this stack through the Grafana MCP server](docs/img/mcp-grafana-tool-discovery.png)

`triage` decides which single title reaches a mix stage first and whether its defect is
one a single gain change delivers. Both answers change what executes: the first decides
what gets written into Grafana, the second decides whether the repair node runs at all.
Neither is available from the arithmetic, because "cheap to clear" and "needs a person"
is a judgement about a bay's time, not about a number's size.

`review` is deliberately asymmetric. Closing a record needs two gates:
`RepairResult.landed`, which is arithmetic over a second ffmpeg pass on the written file,
**and** the reviewer's approval. The reviewer can only withhold. No amount of confident
prose resolves an alert over a master that is still out of spec, and a model that could
grant closure on its own would make the whole loop unfalsifiable.

## Where Grafana is load-bearing

Interlock holds no Grafana HTTP client. Every call goes through the official
`grafana/mcp-grafana` server over stdio. There are two connections to it, and the
distinction is worth naming. The survey node's is ADK's, opened and owned by the
`McpToolset` the agent holds. The deterministic nodes share one session in
`interlock/grafana/client.py`, opened once per run so the tool-call rail is a single
transcript rather than five. Both are the same pinned binary against the same stack, and
the run record reports the two rails separately rather than merging them into one number.

There is no REST fallback and no offline mode: Grafana is where the breach record lives,
so without a token there is no output to produce, and `GrafanaRequired` is the correct
outcome.

Tools used at runtime: `user_info`, `list_datasources`, `create_folder`, `search_folders`,
`update_dashboard`, `get_dashboard_by_uid`, `create_annotation`, `get_annotations`,
`alerting_manage_rules` (create, get, update, delete), `create_incident`,
`update_incident`, `add_activity_to_incident`, `get_incident`, `query_loki_logs`,
`grafana_api_request`.

The survey node's `tool_filter` is an allowlist of that server's 21 read tools, and it is
a boundary rather than a convenience. mcp-grafana offers 81 tools against this stack and
20 of them mutate it, including `update_dashboard` and `install_plugin`. A model sent to
describe the stack must not be able to overwrite the delivery record while it looks
around.

## The capability probe, which is the design and not a fallback

A tool appearing in mcp-grafana's list of 81 proves nothing about whether a given stack
will run it. On the stack this was built against, `create_incident` is advertised, the
IRM plugin is installed and enabled, its REST surface returns a correct validation error
for an empty body, and a real create fails inside Grafana's own database:

```
create incident: calculateIncidentID: doCalculateIncidentID: Counter.Insert:
Error 1452 (23000): Cannot add or update a child row: a foreign key constraint fails
(`grafana_incident`.`Counters`, CONSTRAINT `Counters_orgID_fk`
 FOREIGN KEY (`orgID`) REFERENCES `Org` (`orgID`) ON DELETE CASCADE ON UPDATE RESTRICT)
```

The IRM backend has never provisioned an Org row for this stack, which one admin visit
to the IRM app fixes. Nothing in the tool list says so. Only trying does.

So the second node of the graph attempts each write for real and reads it back, and the
escalation path is chosen from what survived: IRM incident if the stack takes one,
otherwise a Grafana alert rule, otherwise annotations alone. The refusal is rendered on
the product surface verbatim. An agent that discovers it cannot open an incident here and
routes to the strongest action that works is a more honest demonstration than a happy
path that would break on your stack.

## What it refuses to guess

- **A criterion that could not be measured is never a pass.** If ffmpeg returns no true
  peak block, the ceiling was not checked, and the title is reported NOT MEASURED with
  the reason rather than marked in spec. The same holds for the loudness range and for a
  window that sits entirely below the absolute gate, where availability arithmetic would
  otherwise return a reassuring 100%. `RepairResult.landed` requires the level, the true
  peak and the absence of any unmeasured criterion, so a record cannot close on a check
  that could not fail.

- **The repair fixes the level, not the damage.** A `loudnorm` pass corrects an offset.
  Out-of-corridor spans caused by a dead reel, a blown optical track or a splice survive
  it, so they are filed as annotations at their timecodes for a person, and the residual
  work is named on the record.

- **Caption findings travel with the master rather than being re-run on the repair.** The
  repair writes audio only, so re-measuring captions on it would return zero failures on a
  file that has no captions. The findings that travel are the ones measured on the
  master's own sidecar, which a gain change cannot alter.

- **Every figure covers a stated window, not a whole feature.** Each title is measured
  over the window printed beside it. Eight features at full length is minutes of ffmpeg
  per pass, so the window is bounded and declared everywhere rather than implied.

- **The measured series is carried onto real panels as inline data read by the Infinity
  datasource, and the panel says so.** It is read back out of the stored dashboard and
  compared against ffmpeg row for row, so "the series is on the panel" is an assertion
  rather than a screenshot. Writing it into Grafana Cloud Prometheus instead needs an
  Access Policy token with `metrics:write`, which is a different credential from a stack
  service account token. Set `INTERLOCK_OTLP_USER` and `INTERLOCK_OTLP_TOKEN` and the
  same run stores the series in Mimir, and the label on the panel changes to say that.

- **IRM incident creation is refused on this stack**, for the reason above. The code path
  is exercised by the probe every run and lights up with no change once the org row is
  provisioned.

- **The web surface renders the last real run.** There is no fixture. Before any run has
  happened it says so and prints the command, rather than drawing an empty chart.

## Checks that can fail

```bash
python -m pytest tests/ -q -rs
```

Two numbers, and the second is the one that matters:

```
bare clone, nothing configured        67 passed, 11 skipped, exit 0
                                      + a red banner: grafana integration NOT exercised
GRAFANA_URL pointed at a hostname
  with no stack behind it             67 passed, 11 errors, exit 1
the real Grafana Cloud stack          78 passed, 0 skipped, exit 0
                                      + grafana: exercised for real against <stack>
                                      + gemini:  exercised for real against <model>
```

Eleven tests move from skipped to passing when a real stack is present, and the run takes
47 seconds instead of 17. Both numbers are the evidence that they were doing something.

A green tally that counts skips on the integration under judgement is worth nothing, so
`tests/conftest.py` makes a configured-but-unreachable endpoint red rather than skipped,
and prints at the end of every run whether Grafana and Gemini were touched at all. Only a
bare clone skips, and it says in words what it did not prove.

Nine behaviours were broken on purpose, confirmed red, restored, and confirmed green.
`PROOF.md` carries the transcript, `scripts/prove.sh` regenerates it.

- The SLO can come back green. `test_a_control_title_passes_the_slo` measures Niagara
  Falls and asserts 100% availability and a `clear` verdict, paired with
  `test_a_known_offender_fails_the_slo_at_the_published_figure`.
- Silence is a failed measurement, not a passing one. A synthetic silent file raises
  `MeasurementFailed`.
- An unmeasured true peak fails rather than passes, and a repair whose peaks went over
  the ceiling does not close a record.
- The repair gate is the number, not the exit code.
- ffmpeg cannot write outside the project work directory. Four hostile paths including
  two traversals raise `UnsafeOutput`.
- The alert rule threshold can come back inactive.
- A datasource uid the survey agent invented stops the run, and one of the wrong type
  stops it too.
- A single altered sample fails the series read-back, so the comparison is over values
  and not over a row count.
- Every credential, removed one at a time, stops the run before ffmpeg or MCP.

![The fleet, ranked by error budget burn](docs/img/the-fleet-under-slo.png)

![Post-conditions, and the query that established each one](docs/img/post-conditions.png)

## Run it

Requires `ffmpeg` and `ffprobe` on PATH, Python 3.13, and the official mcp-grafana binary.

```bash
go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest

uv venv && uv pip install -e .
cp .env.example .env            # then put your own values in it

python -m interlock.catalog     # fetch the fleet from archive.org, about 190 MB
python -m interlock             # measure, file, repair, close
python web/server.py            # the delivery record, on :8080
```

`.env` needs `GRAFANA_URL` and `GRAFANA_SERVICE_ACCOUNT_TOKEN` for a stack whose service
account can write dashboards, annotations and alert rules, plus `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI=true` and `GEMINI_MODEL`. No token is
ever printed, logged, serialised into the run record, or written into the container image.
The deployed service takes its token from Secret Manager at runtime.

## Build notes worth keeping

Six things cost real time and none of them are in any documentation, so they are recorded
here rather than in a commit message nobody reads.

1. **`ffmpeg -nostats` silently removes the loudness series.** The summary block still
   prints, so a measurement looks fine right up until you ask for the shape of the
   programme. On ffmpeg 9.0.1, `ebur128=framelog=verbose` does the same thing: it prints
   only the summary. The per-100 ms lines come from the default log level.
2. **`alerting_manage_rules` needs an explicit `org_id` argument.** Without it the create
   is refused with `org_id is required and must be greater than 0`, while dashboards,
   folders and annotations all succeed, so a stack can look fully writable and still
   refuse the one write that opens an alert. The tool's JSON schema does not list
   `org_id` under `required`, mentioning it only in a field description, and setting
   `GRAFANA_ORG_ID` in the environment does not help because mcp-grafana reads that for
   its own configuration while the alerting handler reads the argument.
3. **Grafana Cloud rate-limits annotation POSTs at about three in a row.** The fourth
   returns `[POST /annotations] postAnnotation (status 429): {}` and the bucket refills
   within a second. This is handled with backoff around the single throttled call rather
   than by retrying the workflow node, because retrying the node would re-post the
   annotations that already landed and file the same breach twice.
4. **`GET /api/folders/uid/interlock` returns 404 for a folder that exists** and appears
   in `GET /api/folders` two lines later, and `search_folders` is a fuzzy search that can
   miss an exact uid. A single read by uid is not evidence of absence. Interlock reads the
   folder list.
5. **`get_annotations` answers `{"Payload": [...]}`, not a bare list.** A caller that
   expects a list reads every successful query as zero rows, so a write that landed looks
   like a write that vanished. This one was found by a live test, which is the only kind
   that could have found it.
6. **Cloud Run answers `/healthz` itself and never forwards it to the container**, with
   Google's own 404 page, while `/nope` reaches the container and gets the application's
   404. The health route also answers at `/api/health`, which arrives.
7. **archive.org refuses a cold range read often enough to matter.** A fresh container
   asked for The Memphis Belle and got `curl exit 22` while the other seven titles were
   perfectly available. The fetch retries with a widening pause, deletes
   whatever a broken transfer left behind rather than measuring it, and reports an
   unavailable title by name instead of raising and leaving the whole fleet on the shelf.
8. **Cloud Run throttles CPU outside requests**, so a background run started by a POST
   sat for eight minutes before its MCP server started. `--no-cpu-throttling` is what
   makes an agent that works after the response has been sent actually work.

One more, about a sibling project rather than this one. A related repo resolves the MCP
server with `uvx --from mcp-grafana mcp-grafana` as a last resort. That branch can never
succeed: mcp-grafana is a Go binary and there is no such PyPI distribution, so the
fallback only ever raises on the far side of a 90 second MCP timeout, which reads as a
network problem rather than as a missing binary. Interlock's resolver checks real paths
and then names the one `go install` command that fixes it.

## Licence

Apache-2.0. The films are public domain, from archive.org.
