"""Programmatic loop, short agent calls, memory in the database.

This is the shape CHIA's own long-running loops use. Its gem5 alignment study
(paper §5.1, Fig. 3) ran 202 iterations over 10.5 days without ever holding an
agent open across the expensive work:

    choose parent -> agent assembles a change -> build & run programmatically
          ^                                                  |
          +--------  compare, persist to SQLite  <------------+

Each iteration invokes a **fresh** agent. Its memory is the database rendered
into the next prompt, not a conversation held open.

We arrived here the long way. Five runs failed trying to keep one agent session
alive across hour-long ORFS builds: the model hung past its own timeout twice,
gave up and declared the build system broken, ended a session by replying
without a tool call, and left candidates orphaned because tool state did not
survive the pickle into an MCP actor. None of those were reasoning failures.
They were all consequences of asking an agent to wait.

Here the agent is asked one bounded question per iteration — *given everything
tried so far, what should we build next?* — and answers in seconds. The builds
happen in Python, in parallel, with nothing waiting on a model.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)


def render_state(store, screen, failures, top_n: int = 12) -> str:
    """The database as prompt text — the agent's whole memory of the run."""
    lines = []
    built = [c for c in store.list(status="built", limit=200)]
    if built:
        lines.append("## What has been built, and what it measured\n")
        lines.append("| knobs | worst_slack | clock_skew | clock_wl_um | power_W |")
        lines.append("|---|---|---|---|---|")
        for c in built:
            m = c.metrics or {}
            knobs = ", ".join(f"{k}={v}" for k, v in sorted(c.knobs.items())) or "(defaults)"
            def g(k):
                v = m.get(k)
                return f"{v:.5g}" if isinstance(v, (int, float)) else "-"
            lines.append(f"| {knobs} | {g('worst_slack')} | {g('clock_skew_setup')} "
                         f"| {g('clock_wirelength_um')} | {g('power_total')} |")
    else:
        lines.append("## Nothing has been built yet.\n")

    bad = failures.hints(limit=6)
    if bad:
        lines.append("\n## Configurations that failed to build\n")
        lines += [f"- {h}" for h in bad]

    if screen and screen.get("ranked"):
        lines.append(f"\n## A fast surrogate's ranking by predicted "
                     f"{screen['metric']} ({screen['better']} is better)\n")
        lines.append("These are predictions, not measurements. Entries often tie, "
                     "because the model ignores knobs that do not move its objective — "
                     "a tie is not a choice.\n")
        for knobs, value in screen["ranked"][:top_n]:
            lines.append(f"- {value:.6g}  "
                         + ", ".join(f"{k}={v}" for k, v in sorted(knobs.items())))
    return "\n".join(lines)


PROPOSAL_RE = re.compile(r"\{[^{}]*\}")


def parse_proposals(text: str, policy, want: int) -> tuple[list[dict], list[str]]:
    """Pull knob dicts out of the model's reply, keeping only legal ones.

    Returns (accepted, rejections). Parsing rather than tool-calling keeps the
    agent's turn to a single short request — no function-call round trips, and
    nothing for a long build to hang.
    """
    accepted, rejected, seen = [], [], set()
    for blob in PROPOSAL_RE.findall(text.replace("'", '"')):
        try:
            raw = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or not raw:
            continue
        knobs = {}
        for k, v in raw.items():
            if isinstance(v, (int, float)):
                knobs[str(k)] = v
            elif isinstance(v, str):
                try:
                    knobs[str(k)] = float(v)
                except ValueError:
                    pass
        if not knobs:
            continue
        ok, why = policy.check(knobs)
        key = json.dumps(knobs, sort_keys=True)
        if not ok:
            rejected.append(f"{knobs} -> {why}")
            continue
        if key in seen:
            continue
        seen.add(key)
        accepted.append(knobs)
        if len(accepted) >= want:
            break
    return accepted, rejected


def ask(client, model: str, system: str, prompt: str, timeout_s: int = 180) -> str:
    """One short, bounded model call. No tools, so nothing can hang on a build."""
    from google.genai import types
    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(system_instruction=system or None,
                                           temperature=0.5),
    )
    return "".join(p.text for p in (response.candidates[0].content.parts or [])
                   if getattr(p, "text", None))


def run_iterations(*, client, model, system, store, failures, screen, policy,
                   build, iterations: int, per_iteration: int,
                   objective: str = "worst_slack", better: str = "higher",
                   transcript_path: str | None = None, timeout_s: int = 180) -> dict:
    """Alternate short agent decisions with programmatic builds.

    ``build(knobs_list) -> list[(knobs, result)]`` runs candidates in parallel
    and returns once they are all done. The agent is never inside that call.
    """
    transcript = []
    for i in range(iterations):
        state = render_state(store, screen, failures)
        prompt = (
            f"{state}\n\n## Your task, iteration {i + 1} of {iterations}\n\n"
            f"Propose exactly {per_iteration} clock-tree configurations to build "
            f"next. They run in parallel, so make them genuinely different from "
            f"each other and from what has already been built.\n\n"
            f"Optimise {objective} ({better} is better), without inflating area "
            f"or power.\n\n"
            f"Legal knobs and ranges:\n{policy.describe_for_agent()}\n\n"
            f"Reply with one JSON object per line and nothing else, e.g.\n"
            f'{{"CTS_CLUSTER_SIZE": 18, "CTS_CLUSTER_DIAMETER": 45, "CTS_BUF_DISTANCE": 90}}\n'
            f"Briefly state your reasoning first, then the JSON lines.")

        started = time.monotonic()
        try:
            reply = ask(client, model, system, prompt, timeout_s)
        except Exception as exc:
            logger.error("iteration %d: model call failed: %s", i + 1, exc)
            break
        logger.info("iteration %d: agent replied in %.1fs", i + 1,
                    time.monotonic() - started)

        proposals, rejections = parse_proposals(reply, policy, per_iteration)
        for r in rejections:
            logger.warning("iteration %d rejected %s", i + 1, r)
        transcript.append({"iteration": i + 1, "reply": reply,
                           "proposed": proposals, "rejected": rejections})
        if transcript_path:
            with open(transcript_path, "w") as f:
                json.dump(transcript, f, indent=1)

        if not proposals:
            logger.warning("iteration %d produced no legal proposal; stopping", i + 1)
            break

        logger.info("iteration %d: building %d candidate(s)", i + 1, len(proposals))
        build(proposals)          # programmatic, parallel, agent not involved

    return {"iterations": len(transcript), "transcript": transcript}
