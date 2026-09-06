"""A turn-by-turn agent loop we drive ourselves.

`VertexGeminiLLM.prompt()` is one blocking call that runs the whole agentic
loop internally: it sends, executes any function calls against their MCP
servers, and loops until the model stops calling tools. That design assumes
tool calls are short. Ours are not — an ORFS candidate takes about an hour —
and every failure in five successive runs came from that mismatch rather than
from the agent's reasoning or the surrogate:

  * it ignores `timeout_seconds` and hung for 2h10m, twice, while a candidate
    completed underneath and went uncollected
  * a reply carrying no function call silently ends the session
  * a `ChiaTool` is pickled to reach its MCP actor, so tool state exists in two
    copies and the driver's view of in-flight work is always empty
  * killing a hung session abandons whatever it had dispatched

This module inverts the control. We send one turn, get back either text or a
function call, execute it ourselves, append the result, and send again. That
buys four things the wrapped loop cannot give:

  **A timeout that works** — we own the request.
  **Persistence** — the transcript is ours, so a crashed driver resumes instead
  of restarting. This is what `resume_session` gives the CLI backends and what
  the Vertex backend has no equivalent for.
  **Control between turns** — we can return while a build runs and come back
  when Ray says it is done, rather than holding a connection open for an hour.
  **Tools called in-process** — no MCP round trip, no pickled second copy, so
  the driver and the agent see the same objects.

The MCP surface on :class:`~chia_openroad.orfs_tools.ORFSAgentTool` still
exists and is still the deliverable; this loop simply calls the same methods
directly, which is what makes it reliable.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

#: Tool methods the agent may call, in the order they are advertised.
AGENT_TOOLS = ("list_legal_knobs", "screen_candidates", "past_failures",
               "propose_candidate", "candidate_status", "list_candidates",
               "compare_candidates", "best_candidate")


def _schema_for(fn) -> dict:
    """A Gemini function declaration derived from a tool method's signature."""
    sig = inspect.signature(fn)
    props, required = {}, []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        ann = param.annotation
        if ann is int:
            kind = "integer"
        elif ann is float:
            kind = "number"
        elif ann is dict or name == "knobs":
            kind = "object"
        else:
            kind = "string"
        props[name] = {"type": kind, "description": name}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    doc = (fn.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
    return {"name": fn.__name__, "description": doc[:900],
            "parameters": {"type": "object", "properties": props,
                           "required": required}}


class TurnAgent:
    """Drives a Gemini model over a set of tool methods, one turn at a time."""

    def __init__(self, tool, model="gemini-2.5-flash", project=None, location=None,
                 system="", request_timeout=180, transcript_path=None):
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(
            vertexai=True,
            project=project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            http_options=types.HttpOptions(timeout=request_timeout * 1000),
        )
        self.model = model
        self.tool = tool
        self.system = system
        self.transcript_path = transcript_path
        self.history: list = []
        self.declarations = [_schema_for(getattr(tool, n)) for n in AGENT_TOOLS
                             if hasattr(tool, n)]

    # -- persistence ------------------------------------------------------
    def resume(self) -> int:
        """Reload a persisted transcript. Returns how many turns were restored.

        This is the memory the wrapped backend has no equivalent for: the CLI
        backends get it from `--resume <session_id>`, the Vertex one from
        nothing. With it, a driver that dies mid-run picks up where it left off
        instead of paying for the whole exploration again.
        """
        if not self.transcript_path or not os.path.exists(self.transcript_path):
            return 0
        types = self._types
        with open(self.transcript_path) as f:
            plain = json.load(f)
        self.history = [
            types.Content(role=turn["role"],
                          parts=[types.Part(text=t) for t in turn["parts"]])
            for turn in plain if turn.get("parts")]
        logger.info("resumed %d turn(s) from %s", len(self.history),
                    self.transcript_path)
        return len(self.history)
    def save(self) -> None:
        """Persist the transcript so a crashed run resumes rather than restarts."""
        if not self.transcript_path:
            return
        plain = [{"role": h.role,
                  "parts": [p.text if getattr(p, "text", None) else str(p)
                            for p in h.parts]}
                 for h in self.history]
        with open(self.transcript_path, "w") as f:
            json.dump(plain, f, indent=1)

    # -- one turn ---------------------------------------------------------
    def _send(self):
        types = self._types
        return self.client.models.generate_content(
            model=self.model,
            contents=self.history,
            config=types.GenerateContentConfig(
                system_instruction=self.system or None,
                tools=[types.Tool(function_declarations=self.declarations)],
                temperature=0.4,
            ),
        )

    def run(self, task: str, max_turns: int = 40, deadline_s: int = 3600) -> str:
        """Run until the model stops calling tools, the turn budget is spent, or
        the deadline passes. Returns the model's final text.

        Every tool result is appended to the transcript, so what the agent
        learned survives the loop ending for any reason.
        """
        types = self._types
        if not self.history:
            self.history.append(types.Content(role="user",
                                              parts=[types.Part(text=task)]))
        else:
            self.history.append(types.Content(
                role="user",
                parts=[types.Part(text="Continue from where you left off. "
                                       "Check any candidates you had started.")]))
        started = time.monotonic()
        final = ""

        for turn in range(max_turns):
            if time.monotonic() - started > deadline_s:
                logger.warning("agent deadline reached after %d turn(s)", turn)
                break
            try:
                response = self._send()
            except Exception as exc:
                logger.error("model call failed on turn %d: %s", turn, exc)
                break

            candidate = (response.candidates or [None])[0]
            if candidate is None or not candidate.content:
                break
            self.history.append(candidate.content)
            self.save()

            calls = [p.function_call for p in (candidate.content.parts or [])
                     if getattr(p, "function_call", None)]
            texts = [p.text for p in (candidate.content.parts or [])
                     if getattr(p, "text", None)]
            if texts:
                final = "\n".join(texts)
                logger.info("agent: %s", final[:300].replace("\n", " "))

            if not calls:
                # No tool call means the model considers itself done. The
                # wrapped loop treats this the same way, which is how an agent
                # that merely narrated its plan ended a whole session.
                break

            # Every response for one call-turn goes in a SINGLE Content whose
            # part count matches the call count. Appending them as separate
            # messages fails with
            #   400 INVALID_ARGUMENT: Please ensure that the number of function
            #   response parts is equal to the number of function call parts
            # which is easy to hit, because a model routinely asks for two
            # things at once (here: list_legal_knobs and past_failures).
            parts = []
            for call in calls:
                name = call.name
                args = dict(call.args or {})
                logger.info("tool: %s(%s)", name,
                            json.dumps(args, default=str)[:160])
                try:
                    result = getattr(self.tool, name)(**args)
                except Exception as exc:
                    result = f"{type(exc).__name__}: {exc}"
                    logger.error("tool %s failed: %s", name, exc)
                parts.append(types.Part.from_function_response(
                    name=name, response={"result": str(result)[:8000]}))
            self.history.append(types.Content(role="user", parts=parts))
            self.save()

        return final
