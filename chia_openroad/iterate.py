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
        lines.append("## Everything built so far (id, and what it was derived from)\n")
        lines.append("| id | from | knobs | worst_slack | clock_skew | clock_wl_um | power_W |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in sorted(built, key=lambda x: x.id):
            m = c.metrics or {}
            knobs = ", ".join(f"{k}={v}" for k, v in sorted(c.knobs.items())) or "(defaults)"
            def g(k):
                v = m.get(k)
                return f"{v:.5g}" if isinstance(v, (int, float)) else "-"
            parent = f"#{c.parent_id}" if c.parent_id else "base"
            lines.append(f"| #{c.id} | {parent} | {knobs} | {g('worst_slack')} "
                         f"| {g('clock_skew_setup')} | {g('clock_wirelength_um')} "
                         f"| {g('power_total')} |")
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
        knobs, parent = {}, None
        for k, v in raw.items():
            if str(k).lower() in ("from", "parent", "parent_id"):
                try:
                    parent = int(v)
                except (TypeError, ValueError):
                    pass
                continue
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
        accepted.append({"knobs": knobs, "parent": parent})
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
                   transcript_path: str | None = None, timeout_s: int = 180,
                   consult=None, shortlist_factor: int = 3) -> dict:
    """Alternate short agent decisions with programmatic builds.

    ``build(knobs_list) -> list[(knobs, result)]`` runs candidates in parallel
    and returns once they are all done. The agent is never inside that call.

    ``consult(knob_dicts) -> str`` is optional. When given, each iteration runs
    two short model calls instead of one: the agent first names a *shortlist*
    it is considering, the surrogate prices that shortlist in milliseconds, and
    the agent then commits knowing what a fast model expects.

    Why two calls rather than a tool the agent can invoke at will: a tool loop
    reintroduces function-call round trips, which is what made an earlier
    version hang for 2h10m and recycle stale results. Two bounded calls give
    the agent the same information with no open-ended loop. The surrogate is
    milliseconds, so consulting it is free next to the ~30 min a build costs.

    Without ``consult`` the behaviour is exactly the single-call loop, which is
    the control arm for measuring whether consultation is worth anything.
    """
    transcript = []
    for i in range(iterations):
        state = render_state(store, screen, failures)
        prompt = (
            f"{state}\n\n## Your task, iteration {i + 1} of {iterations}\n\n"
            f"Propose exactly {per_iteration} configurations to build next. They "
            f"run in parallel, so make them genuinely different from each other "
            f"and from everything already built.\n\n"
            f"Optimise {objective} ({better} is better), without inflating area "
            f"or power.\n\n"
            f"**Every knob below is yours to set** — floorplan, placement and "
            f"clock tree alike, not only the clock-tree ones. A knob you leave "
            f"out takes the design's own default.\n\n"
            f"**Each configuration is a full specification, and each costs about "
            f"an hour** whichever knob you change: routing dominates this design "
            f"and almost any change forces a full re-route. So the question is "
            f"not how to make an experiment cheap, it is which experiments are "
            f"worth an hour. You have {iterations - i} iteration(s) left.\n\n"
            f"You may derive a configuration from an earlier one by adding "
            f'`"from": <id>` to its JSON. That records the lineage so the search '
            f"reads as a tree; it does not change the cost.\n\n"
            f"Legal knobs and ranges:\n{policy.describe_for_agent()}\n\n"
            f"Reply with one JSON object per line and nothing else, e.g.\n"
            f'{{"CORE_UTILIZATION": 40, "CTS_CLUSTER_SIZE": 18, "from": 14}}\n'
            f"State your reasoning briefly first, then the JSON lines.")

        started = time.monotonic()
        consulted = None
        if consult is not None:
            # Round 1: what are you thinking about? Deliberately wider than the
            # build budget — the point is to price ideas before committing.
            want = max(per_iteration + 1, per_iteration * shortlist_factor)
            try:
                draft = ask(client, model, system, prompt + (
                    f"\n\nFIRST, before committing: list up to {want} "
                    f"configurations you are CONSIDERING. A fast model will "
                    f"price them for you and you will then choose which to "
                    f"build. These are candidates for evaluation, not your "
                    f"final answer, so spread them out.\n"
                    f"Reply with one JSON object per line and nothing else."),
                    timeout_s)
                ideas, _ = parse_proposals(draft, policy, want)
            except Exception as exc:
                logger.warning("iteration %d: shortlist call failed (%s); "
                               "proceeding without consultation", i + 1, exc)
                ideas = []
            if ideas:
                try:
                    consulted = consult([d["knobs"] for d in ideas])
                except Exception as exc:
                    logger.warning("iteration %d: surrogate declined (%s)", i + 1, exc)
                if consulted:
                    logger.info("iteration %d: surrogate priced %d shortlisted "
                                "configuration(s)", i + 1, len(ideas))
                    prompt += (
                        f"\n\n## A fast model priced the shortlist you were "
                        f"considering\n\n{consulted}\n\n"
                        f"These are predictions, not measurements, and the model "
                        f"is silent on knobs it does not observe. Use them to "
                        f"choose — do not treat a tie as a decision.")

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
                           "proposed": proposals, "rejected": rejections,
                           "consulted": consulted})
        if transcript_path:
            with open(transcript_path, "w") as f:
                json.dump(transcript, f, indent=1)

        if not proposals:
            logger.warning("iteration %d produced no legal proposal; stopping", i + 1)
            break

        logger.info("iteration %d: building %d candidate(s)", i + 1, len(proposals))
        build(proposals)          # programmatic, parallel, agent not involved

    return {"iterations": len(transcript), "transcript": transcript}
