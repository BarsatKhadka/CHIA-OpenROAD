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



def knob_evidence(built, objective="worst_slack", better="higher",
                  min_obs=2, max_knobs=8) -> list:
    """Mean objective per knob value, across everything built so far.

    The agent sees individual candidates, each varying six to eight knobs at
    once, and is expected to infer which knob did what. At four candidates a
    turn it cannot. Meanwhile the aggregate signal is strong and sitting in the
    ledger unused: pooling 69 cb_sha256 builds shows CORE_UTILIZATION=45
    averaging -0.675 against 30 averaging -0.864, and SETUP_SLACK_MARGIN=0.0
    averaging -0.682 against 0.1 averaging -0.778.

    That matters because the right move is design-specific and the agent
    carries priors from elsewhere. Lowering utilisation helps cb_aes and
    cb_picorv32, so it proposes that on cb_sha256 too, where the opposite is
    true. Showing the evidence lets it notice.
    """
    import statistics
    from collections import defaultdict
    seen = defaultdict(lambda: defaultdict(list))
    for c in built:
        v = (c.metrics or {}).get(objective)
        if not isinstance(v, (int, float)):
            continue
        for name, val in (c.knobs or {}).items():
            seen[name][str(val)].append(v)
    out = []
    for name, by_val in seen.items():
        groups = {v: statistics.mean(xs) for v, xs in by_val.items()
                  if len(xs) >= min_obs}
        if len(groups) < 2:
            continue
        ranked = sorted(groups.items(), key=lambda t: t[1],
                        reverse=(better == "higher"))
        best, worst = ranked[0], ranked[-1]
        out.append((abs(best[1] - worst[1]), name, best, worst,
                    sum(len(x) for x in by_val.values())))
    out.sort(reverse=True)
    return out[:max_knobs]


def render_state(store, screen, failures, baseline=None, top_n: int = 12) -> str:
    """The database as prompt text — the agent's whole memory of the run.

    ``baseline`` is the design's own default configuration, measured. Without
    it the agent cannot tell a win from a loss: it sees only what it built, so
    a run where every candidate is worse than doing nothing looks identical to
    one where every candidate is better. Measured on cb_picorv32, where 12 of
    12 candidates came in below the default and nothing in the prompt said so.
    """
    lines = []
    if baseline:
        def base(k):
            v = baseline.get(k)
            return f"{v:.5g}" if isinstance(v, (int, float)) else "-"
        lines.append("## The default configuration — this is what you must beat")
        lines.append("")
        lines.append(f"worst_slack {base('worst_slack')}, clock_skew "
                     f"{base('clock_skew_setup')}, power_W {base('power_total')}, "
                     f"area {base('instance_area')}")
        lines.append("")
        lines.append("It sets no knobs at all. A configuration that does not "
                     "beat these numbers is worse than doing nothing.")
        lines.append("")
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
            delta = ""
            bs = (baseline or {}).get("worst_slack")
            if isinstance(m.get("worst_slack"), (int, float)) and isinstance(bs, (int, float)):
                delta = f" ({m['worst_slack'] - bs:+.4f} vs default)"
            lines.append(f"| #{c.id} | {parent} | {knobs} | {g('worst_slack')}{delta} "
                         f"| {g('clock_skew_setup')} | {g('clock_wirelength_um')} "
                         f"| {g('power_total')} |")
    else:
        lines.append("## Nothing has been built yet.\n")

    ev = knob_evidence(built) if built else []
    if ev:
        lines.append("")
        lines.append("## What the runs so far say about each knob")
        lines.append("")
        lines.append("Mean worst_slack across every build that set the knob to "
                     "that value. Higher is better. This is evidence from THIS "
                     "design, which may disagree with what usually works.")
        lines.append("")
        lines.append("| knob | best value seen | worst value seen | builds |")
        lines.append("|---|---|---|---|")
        for _, name, best, worst, n in ev:
            lines.append(f"| {name} | {best[0]} -> {best[1]:.4g} | "
                         f"{worst[0]} -> {worst[1]:.4g} | {n} |")
        lines.append("")

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


def _retrying(call, what: str, attempts: int = 5, base_delay: float = 20.0):
    """Retry a model call through transient quota and server errors.

    A turn that loses its model call ends the whole run: run_iterations breaks
    out of the loop, and the ledger is archived with however many turns it had.
    Measured on cb_sha256, where three loops running concurrently exhausted the
    Vertex quota and turn 3 died with 429 RESOURCE_EXHAUSTED, leaving a
    two-turn run that looked like an agent that had stopped improving.
    """
    import random
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            text = str(exc)
            transient = any(code in text for code in
                            ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
                             "500", "INTERNAL", "DEADLINE_EXCEEDED"))
            if not transient or attempt == attempts - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 5)
            logger.warning("%s failed (%s); retrying in %.0fs (attempt %d/%d)",
                           what, text[:80], delay, attempt + 1, attempts)
            time.sleep(delay)


def ask(client, model: str, system: str, prompt: str, timeout_s: int = 180) -> str:
    """One short, bounded model call. No tools, so nothing can hang on a build."""
    from google.genai import types

    def once():
        response = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(system_instruction=system or None,
                                               temperature=0.5),
        )
        return "".join(p.text for p in (response.candidates[0].content.parts or [])
                       if getattr(p, "text", None))

    return _retrying(once, "model call")



#: Tools the agent may call *inside* a turn. Read-only and fast — SQLite
#: queries and a fitted model, all milliseconds.
#:
#: propose_candidate and candidate_status are deliberately absent. They are the
#: only slow ones: candidate_status blocks for ~30 min waiting on a build, and
#: holding a model session open across that is what made five successive runs
#: fail (hung 2h10m twice, recycled stale results, declared the build system
#: broken). The distinction that matters is not "tools vs no tools" — it is
#: fast tools inside the turn, the hour-long one outside it. Building stays
#: with the driver, which is also what keeps the trust boundary trivial to
#: state: the agent reads freely and commits by proposing, never by executing.
READ_ONLY_TOOLS = ("list_legal_knobs", "past_failures", "list_candidates",
                   "compare_candidates", "best_candidate", "screen_candidates",
                   "describe_surrogates", "predict_knobs")


def ask_with_tools(client, model: str, system: str, prompt: str, tool,
                   timeout_s: int = 180, max_calls: int = 12) -> tuple[str, list]:
    """One turn in which the agent may consult read-only tools before answering.

    Returns (final_text, calls_made). Falls back to a plain ask() if the tool
    exposes none of READ_ONLY_TOOLS, so a caller without a tool still works.
    """
    from google.genai import types
    from chia_openroad.agent import _schema_for

    fns = {n: getattr(tool, n) for n in READ_ONLY_TOOLS if hasattr(tool, n)}
    if not fns:
        return ask(client, model, system, prompt, timeout_s), []
    decls = [_schema_for(f) for f in fns.values()]

    history = [types.Content(role="user", parts=[types.Part(text=prompt)])]
    made, final = [], ""
    for _ in range(max_calls):
        resp = _retrying(lambda: client.models.generate_content(
            model=model, contents=history,
            config=types.GenerateContentConfig(
                system_instruction=system or None,
                tools=[types.Tool(function_declarations=decls)],
                temperature=0.4,
                http_options=types.HttpOptions(timeout=timeout_s * 1000))),
            "tool-calling model call")
        cand = (resp.candidates or [None])[0]
        if cand is None or not cand.content:
            break
        history.append(cand.content)
        parts = cand.content.parts or []
        texts = [p.text for p in parts if getattr(p, "text", None)]
        if texts:
            final = "\n".join(texts)
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        if not calls:
            break
        # One Content carrying exactly as many responses as there were calls;
        # separate messages fail with "number of function response parts must
        # equal number of function call parts".
        out = []
        for call in calls:
            args = dict(call.args or {})
            made.append(f"{call.name}({json.dumps(args, default=str)[:100]})")
            try:
                result = fns[call.name](**args)
            except Exception as exc:
                result = f"{type(exc).__name__}: {exc}"
            out.append(types.Part.from_function_response(
                name=call.name, response={"result": str(result)[:8000]}))
        history.append(types.Content(role="user", parts=out))
    return final, made



def _n_refine(per_iteration: int, ahead: bool) -> int:
    """How many of this turn's candidates should refine the leader.

    Fixed at half, this helped where the default was already near-optimal and
    hurt where the space was rich. Measured across four designs: cb_picorv32
    (default -0.2352, best reachable about -0.2351, so no headroom) went from a
    worsening search to an improving one, while cb_ethmac (default +0.1106, a
    previous run reaching +0.3703, so ample headroom) lost 0.134 ns of median
    and half its hit rate, because refining one or two knobs off the leader
    cannot travel far enough to find what exploration was finding.

    So make it conditional on whether the search is already beating the
    default. Behind it, converge. Ahead of it, keep most of the budget on
    exploration and spend one candidate consolidating.
    """
    return 1 if ahead else max(1, (per_iteration + 1) // 2)


def run_iterations(*, client, model, system, store, failures, screen, policy,
                   build, iterations: int, per_iteration: int,
                   objective: str = "worst_slack", better: str = "higher",
                   transcript_path: str | None = None, timeout_s: int = 180,
                   consult=None, shortlist_factor: int = 3, tool=None,
                   baseline=None) -> dict:
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
    # Turn structure: explore on turn 1, then split each later turn between
    # refining the leader and exploring elsewhere. An earlier prompt demanded
    # every candidate be "genuinely different from everything already built",
    # which forbids refinement and makes every turn pure exploration. Measured
    # over three turns on four designs, the per-turn best was flat or
    # oscillating in seven of eight runs, with turn 3 frequently worse than
    # turn 2.
    transcript = []
    for i in range(iterations):
        # Is the best result so far ahead of the default? That decides how
        # much of this turn goes to refinement versus exploration.
        ahead = False
        if baseline and isinstance(baseline.get(objective), (int, float)):
            vals = [ (c.metrics or {}).get(objective)
                     for c in store.list(status="built", limit=200) ]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                best_so_far = max(vals) if better == "higher" else min(vals)
                ahead = (best_so_far > baseline[objective]) if better == "higher" \
                    else (best_so_far < baseline[objective])
        state = render_state(store, screen, failures, baseline=baseline)
        prompt = (
            f"{state}\n\n## Your task, iteration {i + 1} of {iterations}\n\n"
            f"Propose exactly {per_iteration} configurations to build next. "
            f"They run in parallel, so make them different from each other.\n\n"
            + (f"Spend this turn as follows. {_n_refine(per_iteration, ahead)} "
               f"of your {per_iteration} configurations must REFINE the best "
               f"result so far, each changing only one or two knobs from it "
               f"so you can tell which change was responsible. The rest must "
               f"explore elsewhere. Do not re-propose a configuration "
               f"already built.\n\n" if i > 0 else
               f"This is the first turn and nothing has been built yet, so "
               f"spread these configurations widely across the knobs you "
               f"think matter.\n\n")
            + f"Optimise {objective} ({better} is better) and beat the "
            f"default shown above, without inflating area or power.\n\n"
            f"**Every knob below is yours to set** — floorplan, placement and "
            f"clock tree alike, not only the clock-tree ones. A knob you leave "
            f"out takes the design's own default.\n\n"
            f"**Each configuration is a full specification, and each costs about "
            f"an hour** whichever knob you change: routing dominates this design "
            f"and almost any change forces a full re-route. So the question is "
            f"not how to make an experiment cheap, it is which experiments are "
            f"worth an hour. You have {iterations - i} iteration(s) left.\n\n"
            f'Add `"from": <id>` to derive from an earlier configuration. '
            f"That candidate's knobs are inherited and anything you list "
            f"overrides them, so "
            f'`{{"from": 6, "CTS_CLUSTER_SIZE": 30}}` means #6 with that one '
            f"knob changed. Without a `from`, your JSON is the whole "
            f"configuration and every knob you omit takes the design's "
            f"default.\n\n"
            f"Legal knobs and ranges:\n{policy.describe_for_agent()}\n\n"
            + (f"You may call the read-only tools available to you first — "
               f"they cost milliseconds, and predict_knobs will price any "
               f"configuration you are weighing before you spend an hour on "
               f"it. When you are done looking, answer.\n\n" if tool is not None else "")
            + f"Reply with one JSON object per line and nothing else, e.g.\n"
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

        tool_calls = []
        try:
            if tool is not None:
                reply, tool_calls = ask_with_tools(client, model, system, prompt,
                                                   tool, timeout_s)
                if tool_calls:
                    logger.info("iteration %d: agent called %d tool(s): %s",
                                i + 1, len(tool_calls), ", ".join(tool_calls[:6]))
            else:
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
                           "consulted": consulted, "tool_calls": tool_calls})
        if transcript_path:
            with open(transcript_path, "w") as f:
                json.dump(transcript, f, indent=1)

        if not proposals:
            logger.warning("iteration %d produced no legal proposal; stopping", i + 1)
            break

        logger.info("iteration %d: building %d candidate(s)", i + 1, len(proposals))
        build(proposals)          # programmatic, parallel, agent not involved

    return {"iterations": len(transcript), "transcript": transcript}
