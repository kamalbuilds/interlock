"""`python -m interlock`: one run over the fleet, against live Grafana and Gemini.

There is no dry-run flag. A run that did not talk to Grafana produced no delivery
record, and a flag that made this succeed without one would be the toggle that turns a
load-bearing integration into a decorative one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import catalog  # noqa: E402
from .agent.pipeline import run_fleet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(prog="interlock", description=__doc__)
    parser.add_argument(
        "--titles",
        nargs="*",
        metavar="TITLE_ID",
        help=f"subset of the fleet. Known: {', '.join(sorted(catalog.BY_ID))}",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        metavar="SECONDS",
        help="override the per-title scan window",
    )
    args = parser.parse_args()

    result = asyncio.run(run_fleet(args.titles, window_seconds=args.window))
    state = result["state"]
    outcome = state.get("outcome") or {}
    caps = state.get("capabilities") or {}

    print(f"escalation path      {caps.get('escalation_path')}")
    print(f"mcp calls            {result['mcp_calls']}")
    print("nodes executed       " + " -> ".join(f"{t['node']}[{t['kind']}]" for t in result["node_trace"]))
    # The ADK runner attributes a Workflow's events to the workflow rather than to the
    # node inside it, so this line says "interlock" and not a node list. It is printed
    # anyway because the per-node trace above is self-reported, and an honest report
    # says which of its two sources is the weaker one.
    print(f"runner attributed events to {' -> '.join(result['nodes_that_emitted'])}")
    for entry in result["node_trace"]:
        print(f"  {entry['node']:<14} {entry['detail']}")
    asked = [c["tool"] for c in result["agent_tool_calls"] if c["direction"] == "call"]
    print(f"grafana toolset      the model called {', '.join(asked) if asked else 'nothing'}")
    print(f"fleet board          {result['fleet_dashboard_url']}")
    print(f"run record           {result['record_path']}")
    if outcome:
        print(f"closed               {outcome.get('closed')}  {outcome.get('title_id')}")
        print(f"gate                 {outcome.get('gate')}")
    record = json.loads(Path(result["record_path"]).read_text())
    held = sum(1 for a in record["assertions"] if a["holds"])
    total = len(record["assertions"])
    print(f"post-conditions      {held} of {total} hold")
    return 0 if held == total else 1


if __name__ == "__main__":
    sys.exit(main())
