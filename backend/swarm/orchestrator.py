"""
Agent swarm orchestrator — spawn, manage, and coordinate sub-agents.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

log = logging.getLogger("addled.swarm")

# How much of the flow's shared transcript rides along with each step. Bounded
# because it is prepended to every later prompt: unbounded, a long flow would
# crowd out the step's own instructions on a small local model.
FLOW_TRANSCRIPT_CHARS = 6000

# How many finished steps the swarm remembers across flows. A flow's own
# transcript is enough for one run, but a second flow — "now review what we
# just built" — needs the first one to still be in scope.
MAX_MEMORY_ENTRIES = 40

# How many mid-flight notes are kept. Notes are for coordination inside one
# wave, so this only has to outlast a run, not the session.
MAX_NOTES = 40

# Hard ceiling on how many times a step's `until` gate may send work back. A
# condition that never passes would otherwise burn model calls indefinitely —
# and on the local 8k model each attempt is also a context risk.
MAX_GATE_ATTEMPTS = 5

# A branch graph can loop, and unlike `dependsOn` a jump is not covered by
# `_find_cycle`. This budget is the only thing guaranteeing a flow terminates.
MAX_BRANCH_JUMPS = 10
# The words a branch target may use instead of a step number.
BRANCH_KEYWORDS = {"done", "stop"}


class SwarmAgent:
    """A single swarm agent with its own context and tools.

    `brief`, `skills` and the roster id are what make it a named desk rather
    than a one-shot call: they are read before every task (see
    `roster.build_persona`), and `model`/`provider` let a cheap desk run on the
    local model while the reasoning desk runs on a cloud one.
    """

    def __init__(self, agent_id: str, name: str, agent_type: str,
                 system_prompt: str, tools: list[str],
                 role: str = "", does: str = "", brief: str = "",
                 skills: list[str] | None = None, model: str = "",
                 provider: str = "", is_lead: bool = False):
        self.id = agent_id
        self.name = name
        self.type = agent_type
        self.system_prompt = system_prompt
        self.tools = tools
        self.role = role
        self.does = does
        self.brief = brief
        self.skills = skills or []
        self.model = model
        self.provider = provider
        self.is_lead = is_lead
        self.status = "ready"
        self.current_task: str | None = None
        self.results: list[dict] = []
        self._provider = None

    def persona(self) -> str:
        """The full instruction block for this agent's next task."""
        try:
            from backend.swarm import roster
            return roster.build_persona(self, self.system_prompt)
        except Exception as e:  # noqa: BLE001
            log.debug("could not compose the persona for %s: %s", self.name, e)
            return self.system_prompt

    def _note_progress(self, task: str, output: str, flow: str = "") -> None:
        """Save what this agent just did to its own notebook. Never raises.

        The notebook is durable and per-agent, so it is what lets a desk
        remember its own work across a restart rather than being a stranger to
        its own plan.
        """
        try:
            from backend.swarm import notebook
            notebook.record(self.id, name=self.name, task=task, output=output,
                            flow=flow)
        except Exception as e:  # noqa: BLE001
            log.debug("could not write the notebook for %s: %s", self.name, e)

    def remember(self) -> str:
        """What this agent has done before, for its next task.

        Its own history only. An agent being handed every other agent's work is
        what made the old shared blackboard hard to reason about; a desk that
        knows its own record is the thing that was missing.
        """
        try:
            from backend.swarm import notebook
            return notebook.digest(self.id, self.name)
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the notebook for %s: %s", self.name, e)
            return ""

    def _resolve_provider(self, handed):
        """The provider this agent should run on.

        A roster entry may name its own provider, so one desk can run on the
        local model while another uses a cloud one. Best-effort: anything that
        cannot be resolved falls back to the provider the caller handed in,
        because a misconfigured desk should still do the work.
        """
        if not self.provider:
            return handed
        try:
            from backend.providers.registry import get_provider
            picked = get_provider(self.provider)
            if picked is not None:
                return picked
        except Exception as e:  # noqa: BLE001
            log.debug("provider '%s' for %s unavailable: %s",
                      self.provider, self.name, e)
        return handed

    async def run_task(self, task: str, provider=None) -> dict:
        """Execute a task through the same pipeline the Chat page uses.

        This used to call provider.chat() directly, which meant a swarm agent
        had no tools, no memory and no model routing — it could only talk. It
        asked for tools in the dashboard and was never given any.

        Going through run_chat_pipeline is what makes an agent's capabilities
        the same as chat's, and it is the only way they stay the same: a skill
        added to the pipeline reaches agents, the character and the bots at
        once, instead of three of the four falling behind.
        """
        self.status = "running"
        self.current_task = task
        self._provider = provider

        # A roster agent may name its own model, so a grunt desk can run on the
        # local model while the reasoning desk runs on a cloud one. Resolved
        # here, best-effort: a provider that cannot be built falls back to the
        # one this agent was handed rather than failing the task.
        provider = self._resolve_provider(provider)

        if not provider:
            self.status = "error"
            self.current_task = None
            return {"success": False, "error": "No AI provider configured",
                    "agent": self.name}

        try:
            # Imported here, not at module load: ws_server imports the swarm.
            from backend.ws_server import run_chat_pipeline

            result = await run_chat_pipeline(
                task,
                # Who it is, its brief, its learned rules, its bound skills —
                # composed from the roster, not just the one-line type prompt.
                persona=self.persona(),
                # `model` rides in params: the pipeline's router reads it, so a
                # roster agent can pin its own model without a new kwarg on a
                # function every other surface also calls.
                params=({"model": self.model} if self.model else None),
                # None means every enabled skill. A list is an agent that was
                # deliberately given a narrower set.
                tools=self.tools,
                # Agent tasks go to memory and the journal so all surfaces can
                # recall what each agent did and why — but they do not go to
                # chat history, which would fill with five-agent chatter the
                # user never asked to read.
                record={"memory", "journal"},
                # And the character should not show "thinking" for work the
                # user is not watching it do.
                announce=False,
                # An agent is reasoning work whatever the wording, so do not
                # let a one-line task get classified as a chat question.
                force_role="reasoning",
                max_tool_rounds=5,
                # The provider this agent was handed, so run_task's existing
                # contract is kept rather than quietly resolved again.
                provider=provider,
            )
            text = (result or {}).get("response", "") or ""
            if text and not text.startswith(("[Provider:", "[Not connected:")):
                self.results.append({
                    "task": task,
                    "response": text,
                    "tools": (result or {}).get("toolResults", 0),
                    "timestamp": time.time(),
                })
                # Write to this agent's own notebook the moment the work is
                # done, not at the end of the flow. A run that is killed
                # mid-way is exactly the case the notebook exists for, and one
                # that only saved on completion would lose the very steps that
                # had already succeeded.
                self._note_progress(task, text)
                self.status = "ready"
                self.current_task = None
                return {"success": True, "response": text, "agent": self.name,
                        "toolResults": (result or {}).get("toolResults", 0)}
            self.status = "error"
            self.current_task = None
            return {"success": False,
                    "error": text or "Agent execution failed",
                    "agent": self.name}
        except asyncio.CancelledError:
            self.status = "ready"
            self.current_task = None
            raise
        except Exception as e:
            log.error("Agent %s task failed: %s", self.name, e)

        self.status = "error"
        self.current_task = None
        return {"success": False, "error": "Agent execution failed",
                "agent": self.name}


class SwarmOrchestrator:
    """Manages multiple swarm agents."""

    def __init__(self):
        self._agents: dict[str, SwarmAgent] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Finished steps across every flow this session. Without this, work
        # from a previous flow was invisible to the next one.
        self._memory: list[dict] = []
        # Mid-flight notes between agents working the same wave, and which of
        # them each agent has already been shown.
        self._notes: list[dict] = []
        self._notes_seen: dict[str, set[int]] = {}

    def spawn(self, name: str, agent_type: str = "general", system_prompt: str = "",
              tools: list[str] | None = None, *, brief: str = "",
              skills: list[str] | None = None, model: str = "",
              provider: str = "", role: str = "", does: str = "",
              is_lead: bool = False, persist: bool = False) -> SwarmAgent:
        """Create a new agent. tools=None means every enabled skill.

        This used to default to ["chat"], a skill that does not exist, so the
        dashboard badge read "chat" and the agent quietly had no tools at all.

        `persist=True` writes the definition to the roster so it survives a
        restart — that is what turns a spawned agent into a named desk the user
        can reuse. The default is False, because a flow that spawns help for one
        run should not litter the roster with it.
        """
        from backend.swarm import roster
        spec = roster.DEFAULT_TYPES.get(agent_type,
                                        roster.DEFAULT_TYPES["general"])
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        if not system_prompt:
            system_prompt = _default_prompt(agent_type)
        if tools is None and skills:
            # A roster entry that named tool skills gets exactly those, so the
            # desk is not offered fifty tools it will never call.
            hinted = roster.skill_names({"skills": skills})
            if hinted:
                tools = hinted
        agent = SwarmAgent(
            agent_id, name, agent_type, system_prompt, tools,
            role=role or spec.get("role", ""),
            does=does or spec.get("does", ""),
            brief=brief,
            skills=list(skills or spec.get("skills") or []),
            model=model, provider=provider, is_lead=is_lead,
        )
        self._agents[agent_id] = agent
        if persist:
            roster.upsert({
                "id": agent_id, "name": name, "type": agent_type,
                "role": agent.role, "does": agent.does, "prompt": system_prompt,
                "brief": brief, "tools": tools, "skills": agent.skills,
                "model": model, "provider": provider, "isLead": is_lead,
            })
        log.info("Spawned agent: %s (%s)%s", name, agent_id,
                 " [persisted]" if persist else "")
        return agent

    def spawn_from_roster(self, agent_id: str) -> SwarmAgent | None:
        """Start an agent from its saved definition.

        This is what makes a restart survivable: the roster is the source of
        truth, and this brings one back without the caller having to remember
        its name, brief, skills or model.
        """
        from backend.swarm import roster
        entry = roster.get(agent_id)
        if not entry:
            return None
        agent = SwarmAgent(
            entry["id"], entry["name"], entry["type"], entry["prompt"],
            entry.get("tools"),
            role=entry.get("role", ""), does=entry.get("does", ""),
            brief=entry.get("brief", ""), skills=entry.get("skills") or [],
            model=entry.get("model", ""), provider=entry.get("provider", ""),
            is_lead=bool(entry.get("isLead")),
        )
        self._agents[agent.id] = agent
        return agent

    def load_roster(self) -> int:
        """Bring every saved agent into this session. Returns how many.

        Called at startup so the Agents page shows the user's desks, not an
        empty room, the first time it is opened after a restart.
        """
        from backend.swarm import roster
        count = 0
        for entry in roster.definitions():
            if entry["id"] in self._agents:
                continue
            try:
                if self.spawn_from_roster(entry["id"]) is not None:
                    count += 1
            except Exception as e:  # noqa: BLE001
                log.debug("could not restore agent %s: %s", entry["id"], e)
        if count:
            log.info("Restored %d roster agent(s)", count)
        return count

    def save_agent(self, agent_id: str) -> dict:
        """Persist a live agent's definition to the roster."""
        from backend.swarm import roster
        agent = self._agents.get(agent_id)
        if agent is None:
            return {"success": False, "error": f"No agent {agent_id}."}
        entry = roster.upsert({
            "id": agent.id, "name": agent.name, "type": agent.type,
            "role": agent.role, "does": agent.does,
            "prompt": agent.system_prompt, "brief": agent.brief,
            "tools": agent.tools, "skills": agent.skills,
            "model": agent.model, "provider": agent.provider,
            "isLead": agent.is_lead,
        })
        return {"success": True, "agent": entry}

    def stop(self, agent_id: str) -> bool:
        """Stop and remove an agent from this session.

        The roster entry is deliberately kept: stopping an agent you defined
        should not delete the definition, or the user would have to retype it.
        Use `forget` for that.
        """
        if agent_id in self._tasks:
            self._tasks[agent_id].cancel()
            del self._tasks[agent_id]
        if agent_id in self._agents:
            del self._agents[agent_id]
            return True
        return False

    def forget(self, agent_id: str) -> bool:
        """Remove an agent AND its saved definition."""
        from backend.swarm import roster
        self.stop(agent_id)
        return roster.remove(agent_id)

    def list_agents(self) -> list[dict]:
        """List all agents with status."""
        return [
            {
                "id": a.id, "name": a.name, "type": a.type,
                "role": a.role, "does": a.does, "brief": a.brief,
                "skills": a.skills, "model": a.model, "provider": a.provider,
                "isLead": a.is_lead,
                "status": a.status, "currentTask": a.current_task,
                # None reads as "all tools" on the Agents page. An empty list is
                # an agent deliberately given none.
                "tools": a.tools, "allTools": a.tools is None,
                "results": len(a.results),
            }
            for a in self._agents.values()
        ]

    def get_agent(self, agent_id: str) -> SwarmAgent | None:
        return self._agents.get(agent_id)

    async def run_agent(self, agent_id: str, task: str, provider=None) -> dict:
        """Run a task on a specific agent.

        The task is registered in _tasks so stop() can actually cancel it. It
        was never registered before, so the Stop button cancelled nothing and
        the agent kept working in the background.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            return {"success": False, "error": f"Agent not found: {agent_id}"}

        running = asyncio.create_task(agent.run_task(task, provider))
        self._tasks[agent_id] = running
        try:
            result = await running
            if result.get("success"):
                self._remember_step(agent, task, result.get("response") or "")
            return result
        except asyncio.CancelledError:
            if not running.cancelled():
                # Not our cancellation — someone is cancelling the caller.
                raise
            agent.status = "ready"
            agent.current_task = None
            log.info("Agent %s stopped by request", agent.name)
            return {"success": False, "error": "Stopped", "agent": agent.name}
        finally:
            self._tasks.pop(agent_id, None)

    async def run_parallel(self, tasks: list[tuple[str, str]], provider=None) -> list[dict]:
        """Run multiple agent tasks in parallel. tasks: [(agent_id, task), ...]."""
        coroutines = [self.run_agent(aid, task, provider) for aid, task in tasks]
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        return [
            {"success": False, "error": str(r)} if isinstance(r, Exception)
            else r for r in results
        ]

    # -- shared run memory ----------------------------------------------------
    #
    # A flow's transcript lived only inside `run_flow`, so an agent could not
    # remember a *previous* flow — run `planner → coder`, then a second flow to
    # review the code, and the reviewer had never seen the plan. This is the
    # same blackboard, kept per swarm for the session, bounded the same way.

    def _remember_step(self, agent: SwarmAgent, task: str, output: str) -> None:
        """Append one finished step to the swarm-wide memory."""
        if not str(output or "").strip():
            return
        self._memory.append({
            "agent": agent.name,
            "agentId": agent.id,
            "task": " ".join(str(task or "").split())[:200],
            "output": str(output),
            "timestamp": time.time(),
        })
        del self._memory[:-MAX_MEMORY_ENTRIES]

    def memory_text(self, limit: int = 8) -> str:
        """Recent finished work across every flow, newest last, bounded."""
        if not self._memory:
            return ""
        blocks = []
        for entry in self._memory[-limit:]:
            blocks.append(f"[{entry['agent']} — {entry['task']}]\n"
                          f"{entry['output']}")
        text = "\n\n".join(blocks)
        if len(text) > FLOW_TRANSCRIPT_CHARS:
            text = text[-FLOW_TRANSCRIPT_CHARS:]
        return text

    def clear_memory(self) -> int:
        """Forget the shared memory. Returns how many entries were dropped."""
        count = len(self._memory)
        self._memory.clear()
        return count

    # -- flow checkpoints ----------------------------------------------------
    #
    # A flow is the longest-running thing in the app and had no durability: an
    # interrupted one lost every finished step, because progress lived in local
    # variables. These three write it down instead, and `resume_flow` picks it
    # up. Each is best-effort — a checkpoint that cannot be written must not
    # stop the flow it is describing.

    @staticmethod
    def _checkpoint_start(flow_id: str, goal: str, steps: list[dict]) -> None:
        try:
            from backend.swarm import checkpoints
            checkpoints.start(flow_id, goal, steps)
        except Exception as e:  # noqa: BLE001
            log.debug("could not open a checkpoint for %s: %s", flow_id, e)

    @staticmethod
    def _checkpoint_save(flow_id: str, finished, skipped, done) -> None:
        try:
            from backend.swarm import checkpoints
            checkpoints.progress(flow_id,
                                 finished=list(finished),
                                 skipped=list(skipped),
                                 done=list(done))
        except Exception as e:  # noqa: BLE001
            log.debug("could not update the checkpoint for %s: %s", flow_id, e)

    @staticmethod
    def _checkpoint_done(flow_id: str) -> None:
        try:
            from backend.swarm import checkpoints
            checkpoints.complete(flow_id)
        except Exception as e:  # noqa: BLE001
            log.debug("could not close the checkpoint for %s: %s", flow_id, e)

    def resumable_flows(self) -> list[dict]:
        """Flows that were interrupted and still have steps to run."""
        try:
            from backend.swarm import checkpoints
            return checkpoints.resumable()
        except Exception as e:  # noqa: BLE001
            log.debug("could not list resumable flows: %s", e)
            return []

    async def resume_flow(self, flow_id: str, provider=None) -> dict:
        """Carry on an interrupted flow from where it stopped.

        The finished steps are replayed from the checkpoint rather than rerun —
        that work cost model calls and its results are recorded — and only the
        unfinished ones execute. A flow with nothing left to do is reported as
        already complete rather than started again.
        """
        try:
            from backend.swarm import checkpoints
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"checkpoints unavailable: {e}"}

        data = checkpoints.load(flow_id)
        if data is None:
            return {"success": False,
                    "error": f"No checkpoint for {flow_id}."}

        steps = data.get("steps") or []
        finished = sorted(int(s) for s in data.get("finished") or [])
        skipped = sorted(int(s) for s in data.get("skipped") or [])
        prior_done = data.get("done") or []
        goal = str(data.get("goal") or "")

        remaining = [p for p in range(1, len(steps) + 1)
                     if p not in finished and p not in skipped]
        if not remaining:
            checkpoints.complete(flow_id)
            return {"success": True, "flowId": flow_id, "resumed": False,
                    "steps": prior_done,
                    "message": "That flow had already finished."}

        # The agents a step names have to exist before it can run. A resumed
        # flow whose desks were removed is reported plainly rather than failing
        # on the first step with an unclear error.
        missing = [s.get("agentId") for s in steps
                   if str(s.get("agentId") or "") not in self._agents]
        if missing:
            return {"success": False, "flowId": flow_id,
                    "error": ("These agents no longer exist, so the flow "
                              "cannot continue: "
                              + ", ".join(sorted({str(m) for m in missing})))}

        log.info("Resuming flow %s: %d of %d step(s) already done",
                 flow_id, len(finished), len(steps))
        result = await self.run_flow(steps, provider=provider, goal=goal)
        if isinstance(result, dict):
            # `run_flow` opens a fresh checkpoint under a new id, so the old one
            # is closed here or it would be offered to resume forever.
            checkpoints.complete(flow_id)
            result["resumed"] = True
            result["resumedFrom"] = flow_id
            # The replayed steps belong in the answer: a caller reading it
            # should see the whole flow, not only the part that just ran.
            if prior_done:
                result["steps"] = prior_done + list(result.get("steps") or [])
        return result

    # -- mid-flight notes ----------------------------------------------------
    #
    # The transcript above only carries FINISHED steps. Two agents working the
    # same wave at the same time were invisible to each other until both were
    # done, so a teammate could not warn another one — "the two hook lines
    # clash", "I already renamed that helper" — while the warning could still
    # change the outcome. This is that channel, scoped to the wave.

    def note(self, from_agent: str, text: str, to: str = "") -> dict:
        """Leave a note for the other agents in the current wave."""
        text = " ".join(str(text or "").split())
        if not text:
            return {"success": False, "error": "The note was empty."}
        entry = {
            "from": str(from_agent or "?"),
            "to": str(to or "").strip(),
            "text": text[:600],
            "timestamp": time.time(),
        }
        self._notes.append(entry)
        del self._notes[:-MAX_NOTES]
        log.info("Note from %s to %s: %s", entry["from"],
                 entry["to"] or "the wave", entry["text"][:120])
        return {"success": True, **entry}

    def notes_text(self, exclude: str = "", limit: int = 10) -> str:
        """Notes the given agent has not yet seen, newest last.

        A note addressed to someone else is still shown, because overhearing a
        warning is useful — but it is labelled, so the reader knows it was not
        addressed to them.
        """
        seen = self._notes_seen.setdefault(exclude, set())
        fresh = []
        for i, entry in enumerate(self._notes):
            if i in seen:
                continue
            seen.add(i)
            if entry["from"] == exclude:
                continue
            if entry["to"] and entry["to"] != exclude:
                fresh.append(f"[{entry['from']} → {entry['to']}] {entry['text']}")
            else:
                fresh.append(f"[{entry['from']}] {entry['text']}")
        return "\n".join(fresh[-limit:])

    def clear_notes(self) -> int:
        count = len(self._notes)
        self._notes.clear()
        self._notes_seen.clear()
        return count

    # -- flows: ordered and parallel collaboration ----------------------------
    #
    # Until now agents could not collaborate at all. Each `run_agent` call was
    # an island: it went through `run_chat_pipeline(record=False)`, so no
    # history, no shared memory, and `agent.results` — although recorded — was
    # never read by anything, so even a hand-off had no channel. A flow is the
    # missing middle: the orchestrator owns the transcript and hands each agent
    # what the ones before it produced.

    def _flow_transcript(self, steps: list[dict]) -> str:
        """The shared blackboard, as the next agent reads it.

        Bounded on purpose. Every step appends, and this text is prepended to
        each later prompt, so on a long flow an unbounded transcript would grow
        until it crowded out the agent's own instructions — the same failure
        the context budgeting work fixed for chat.
        """
        blocks = []
        for step in steps:
            output = str(step.get("output") or "").strip()
            if not output:
                continue
            blocks.append(f"[{step['agent']} — {step['task']}]\n{output}")
        if not blocks:
            return ""
        text = "\n\n".join(blocks)
        if len(text) > FLOW_TRANSCRIPT_CHARS:
            text = text[-FLOW_TRANSCRIPT_CHARS:]
        return text

    def _flow_prompt(self, step: dict, steps: list[dict],
                     goal: str, index: int, total: int,
                     extra_context: str = "") -> str:
        """One step's task text, carrying the goal and what came before."""
        parts = []
        if goal:
            parts.append(f"Overall goal: {goal}")
        parts.append(f"Your step ({index} of {total}): {step['task']}")
        # This desk's own history first. It is what the agent was missing when
        # every agent shared one blackboard: an agent would be told what the
        # others did and nothing about what it had done itself.
        try:
            own = self._agents[step["agentId"]].remember()
        except Exception:  # noqa: BLE001
            own = ""
        if own:
            parts.append("Your own record from previous work. This is what you "
                         "did, not what the others did — carry it forward:\n\n"
                         + own)
        if extra_context:
            # Another agent is working this same step in parallel. Saying so is
            # what stops two agents writing the same thing twice.
            parts.append(extra_context)
        shared = self._flow_transcript(steps)
        if shared:
            parts.append("Work already done by the other agents in this flow. "
                         "Build on it and do not repeat it:\n\n" + shared)
        else:
            earlier = self.memory_text()
            if earlier:
                # Nothing from this flow yet, so the only thing to build on is
                # work from an earlier flow in this session.
                parts.append("Work from earlier in this session. Build on it "
                             "if it is relevant:\n\n" + earlier)
        return "\n\n".join(parts)

    def _normalise_steps(self, steps) -> tuple[list[dict], str]:
        """Validate a step list. Returns (steps, error)."""
        if not steps or not isinstance(steps, list):
            return [], "A flow needs at least one step"
        normalised: list[dict] = []
        for raw in steps:
            if not isinstance(raw, dict):
                return [], "Every step must be an object with agentId and task"
            agent_id = str(raw.get("agentId") or raw.get("agent_id") or "").strip()
            task = str(raw.get("task") or "").strip()
            if not agent_id or not task:
                return [], "Every step needs an agentId and a task"
            if agent_id not in self._agents:
                return [], f"Agent not found: {agent_id}"
            until, until_error = self._normalise_until(raw.get("until"))
            if until_error:
                return [], until_error
            normalised.append({
                "agentId": agent_id,
                "task": task,
                "parallel": [str(a).strip() for a in (raw.get("parallel") or [])
                             if str(a).strip()],
                "merge": str(raw.get("merge") or "").strip(),
                "until": until,
                "retry": str(raw.get("retry") or "").strip(),
                "dependsOn": [int(d) for d in (raw.get("dependsOn")
                                               or raw.get("depends_on")
                                               or [])],
                # A step that fails without being optional stops the flow, as
                # every step always has. Optional exists so the skip path has
                # something to mean — "this one may fail, carry on".
                "optional": bool(raw.get("optional")),
                # Where to go after this step's verdict, instead of simply on
                # to the next one. A step number (1-based) or "done"/"stop".
                "onPass": self._normalise_branch(raw.get("onPass"),
                                                 raw.get("on_pass")),
                "onFail": self._normalise_branch(raw.get("onFail"),
                                                 raw.get("on_fail")),
            })
        total = len(normalised)
        for position, step in enumerate(normalised, 1):
            for peer in step["parallel"]:
                if peer not in self._agents:
                    return [], f"Agent not found: {peer}"
            if step["retry"] and step["retry"] not in self._agents:
                return [], f"Retry agent not found: {step['retry']}"
            for dependency in step["dependsOn"]:
                # 1-based, matching the step numbers the dashboard shows and
                # the order the user typed them in.
                if dependency < 1 or dependency > total:
                    return [], (f"Step {position} depends on step "
                                f"{dependency}, which does not exist")
                if dependency == position:
                    return [], f"Step {position} cannot depend on itself"
            gate_agent = (step["until"] or {}).get("agent")
            if gate_agent and gate_agent not in self._agents:
                return [], f"Gate agent not found: {gate_agent}"
            judge_agent = (step["until"] or {}).get("judge")
            if judge_agent and judge_agent not in self._agents:
                return [], f"Gate judge not found: {judge_agent}"
            if step["until"] is not None:
                if step["parallel"] or step["merge"]:
                    # Which entry is the gate when several agents answered?
                    # Refused rather than guessed at.
                    return [], ("A step cannot have both 'until' and "
                                "'parallel'/'merge' — it is ambiguous which "
                                "answer the condition reads.")
                if not step["retry"]:
                    return [], ("A step with 'until' also needs 'retry' naming "
                                "the agent to run again when it fails.")
            if step["onPass"] or step["onFail"]:
                if step["parallel"] or step["merge"]:
                    # The same ambiguity as `until`: whose answer decides is
                    # not defined for a fan-out, so it is refused not guessed.
                    return [], ("A step cannot have both 'onPass'/'onFail' and "
                                "'parallel'/'merge' — it is ambiguous which "
                                "answer decides the branch.")
                for label, target in (("onPass", step["onPass"]),
                                      ("onFail", step["onFail"])):
                    if target is None:
                        continue
                    if isinstance(target, tuple):
                        return [], f"Step {position} {label}: {target[1]}"
                    if isinstance(target, str) and target in BRANCH_KEYWORDS:
                        continue
                    if target == position:
                        return [], (f"Step {position} cannot branch to itself "
                                    f"with '{label}' — use 'until' with "
                                    f"'retry' to repeat a step.")
                    if not isinstance(target, int) or target < 1 \
                            or target > total:
                        return [], (f"Step {position} {label} targets step "
                                    f"{target}, which does not exist")
        cycle = self._find_cycle(normalised)
        if cycle:
            return [], f"Steps form a dependency cycle: {cycle}"
        return normalised, ""

    @staticmethod
    def _normalise_branch(*values):
        """A branch target: a 1-based step number, a keyword, or None.

        Returns the raw value for range-checking later (the step count is not
        known until the whole list is read), or ``("invalid", reason)``.
        """
        raw = None
        for value in values:
            if value is not None and value != "":
                raw = value
                break
        if raw is None:
            return None
        if isinstance(raw, bool):
            return ("invalid", "a branch target cannot be true/false")
        if isinstance(raw, int):
            return raw
        text = str(raw).strip()
        if text.lower() in BRANCH_KEYWORDS:
            return text.lower()
        try:
            return int(text)
        except ValueError:
            return ("invalid", f"'{text}' is not a step number, 'done' or 'stop'")

    @staticmethod
    def _find_cycle(steps: list[dict]) -> str:
        """A readable path through the first dependency cycle, or ''.

        Checked up front because a cycle would otherwise show as a step that
        never becomes ready — which reads as a hang rather than a mistake the
        user can fix.
        """
        edges = {i: set(step["dependsOn"])
                 for i, step in enumerate(steps, 1)}
        state: dict[int, int] = {}
        path: list[int] = []

        def walk(node: int) -> str:
            state[node] = 1
            path.append(node)
            for nxt in sorted(edges.get(node, ())):
                colour = state.get(nxt, 0)
                if colour == 1:
                    start = path.index(nxt)
                    return " -> ".join(str(n) for n in path[start:] + [nxt])
                if colour == 0:
                    found = walk(nxt)
                    if found:
                        return found
            path.pop()
            state[node] = 2
            return ""

        for node in sorted(edges):
            if state.get(node, 0) == 0:
                found = walk(node)
                if found:
                    return found
        return ""

    @staticmethod
    def _normalise_until(raw) -> tuple[dict | None, str]:
        """Validate one step's `until` condition. Returns (condition, error).

        Substring matching is the default because it is cheap and predictable,
        but it is not a parser: `contains: "PASS"` also matches inside "NOT
        PASSED". A `judge` turns the decision over to a model for the cases
        where that matters — at the cost of one extra call per evaluation.
        """
        if raw is None:
            return None, ""
        if not isinstance(raw, dict):
            return None, "'until' must be an object"

        judge = str(raw.get("judge") or "").strip()
        criterion = str(raw.get("criterion") or raw.get("criteria") or "").strip()
        contains = str(raw.get("contains") or "")
        not_contains = str(raw.get("notContains") or raw.get("not_contains") or "")

        if judge:
            if not criterion:
                return None, ("'until' with a 'judge' needs a 'criterion' "
                              "describing what counts as passing")
            kinds = 1
        else:
            if bool(contains) == bool(not_contains):
                return None, ("'until' needs exactly one of 'contains', "
                              "'notContains', or a 'judge' with a "
                              "'criterion'")
            kinds = 1
        if kinds != 1:
            return None, "'until' must name exactly one condition"

        try:
            attempts = int(raw.get("maxAttempts",
                                   raw.get("max_attempts", 3)))
        except (TypeError, ValueError):
            attempts = 3
        return {
            "contains": contains,
            "notContains": not_contains,
            "judge": judge,
            "criterion": criterion,
            "agent": str(raw.get("agent") or "").strip(),
            # Clamped, not trusted: a gate that never passes would otherwise
            # burn model calls indefinitely.
            "maxAttempts": max(1, min(attempts, MAX_GATE_ATTEMPTS)),
        }, ""

    def _until_text(self, condition: dict, entries: list[dict]) -> str:
        """The output a condition is evaluated against."""
        if not entries:
            return ""
        chosen = None
        wanted = condition.get("agent") or ""
        if wanted:
            # Matched on agentId, and on the display name because that is what
            # the dashboard sends back from its picker.
            for entry in reversed(entries):
                if entry.get("agentId") == wanted or entry.get("agent") == wanted:
                    chosen = entry
                    break
        if chosen is None:
            chosen = entries[-1]
        return str(chosen.get("output") or "")

    @staticmethod
    def _verdict_is_pass(text: str) -> bool:
        """Read a judge's plain-text verdict.

        A judge is asked for PASS or FAIL, but models embellish. FAIL is
        checked first on purpose: "FAIL, does not pass" contains PASS, and
        reading that as a pass inverts the gate.
        """
        upper = str(text or "").upper()
        if "FAIL" in upper:
            return False
        return "PASS" in upper

    async def _until_met(self, condition: dict, entries: list[dict],
                         provider=None) -> bool:
        """Whether a step's entries satisfy its `until` condition.

        Reads the entry named by `condition['agent']` when one is given, so the
        gate can be a separate reviewer step rather than the producer marking
        its own work. A `judge` asks a model instead of matching text; anything
        else is a case-insensitive substring test on that entry's output.
        """
        if not condition or not entries:
            return True
        text = self._until_text(condition, entries)

        judge = condition.get("judge") or ""
        if judge:
            verdict = await self._ask_judge(judge, condition.get("criterion", ""),
                                            text, provider)
            if verdict is None:
                # The judge could not be reached. Falling back to "pass" would
                # silently accept unreviewed work, so fail and let the attempt
                # budget report it.
                log.warning("Gate judge '%s' produced no verdict", judge)
                return False
            return verdict

        lowered = text.lower()
        needle = str(condition.get("contains") or "").lower()
        if needle:
            return needle in lowered
        banned = str(condition.get("notContains") or "").lower()
        return banned not in lowered

    async def _ask_judge(self, judge_agent: str, criterion: str, output: str,
                         provider) -> bool | None:
        """Ask an agent to judge one output. None when it could not answer."""
        if judge_agent not in self._agents:
            return None
        prompt = (
            "You are judging whether work meets a criterion. Answer with "
            "exactly one word: PASS or FAIL.\n\n"
            f"Criterion: {criterion}\n\n"
            f"Work to judge:\n{output[:4000]}"
        )
        try:
            result = await self.run_agent(judge_agent, prompt, provider)
        except Exception as e:  # noqa: BLE001
            log.warning("Gate judge '%s' raised: %s", judge_agent, e)
            return None
        if not result.get("success"):
            return None
        return self._verdict_is_pass(result.get("response") or "")

    async def run_flow(self, steps: list[dict], provider=None,
                       goal: str = "") -> dict:
        """Run a flow, respecting each step's dependencies.

        Steps with no `dependsOn` run in the order given, which is what every
        flow did before dependencies existed. A step that names dependencies
        waits for exactly those.

        Independent steps in the same wave run **at the same time** when the
        provider can serve more than one request, so two unrelated branches no
        longer cost their sum. A single-generation provider (local llamafile)
        still runs them one at a time — concurrency there would only queue.
        """
        normalised, problem = self._normalise_steps(steps)
        if problem:
            return {"success": False, "error": problem}

        flow_id = f"flow_{uuid.uuid4().hex[:8]}"
        goal = str(goal or "").strip()
        total = len(normalised)
        concurrent = not self._single_generation_provider(provider)
        # A flow is the longest-running thing in the app — minutes of model
        # calls across several agents — and its progress lived only in these
        # local variables. Opening a checkpoint means an interrupted run leaves
        # something behind to carry on from.
        self._checkpoint_start(flow_id, goal, normalised)
        # Notes are for coordination inside one flow. Starting clean means a
        # note from a previous run cannot arrive as if it were about this one.
        self.clear_notes()
        log.info("Flow %s: %d step(s), %s", flow_id, total,
                 "concurrent waves" if concurrent else "sequential (single-"
                 "generation provider)")

        done: list[dict] = []
        finished: set[int] = set()
        skipped: list[int] = []
        jumps: list[dict] = []
        # A jump target that has not run yet in this pass. Set when a branch
        # sends control backwards or forwards; cleared once it is consumed.
        jump_to: int | None = None
        budget_left = MAX_BRANCH_JUMPS
        # Set when a branch targets "done"; `break` alone only leaves the inner
        # loop, so the outer `while` needs a flag to stop too.
        flow_done = False

        while (len(finished) + len(skipped) < total or jump_to is not None) \
                and not flow_done:
            if jump_to is not None:
                # A jump moves execution straight to its target. Steps in
                # between are deliberately not run — that is what makes this a
                # branch rather than an ordering. Its dependencies are treated
                # as satisfied, because the branch decision already decided it
                # should run.
                #
                # `finished` is cleared here, before the wave, so a step that
                # already ran can run again. Doing it after the wave left the
                # target permanently unfinished, and the normal ready set then
                # picked it up a second time.
                finished.discard(jump_to)
                skipped = [s for s in skipped if s != jump_to]
                ready = [jump_to]
            else:
                ready = [
                    position for position, step in enumerate(normalised, 1)
                    if position not in finished and position not in skipped
                    and all(dep in finished for dep in step["dependsOn"])
                ]
            if not ready:
                cascade = [
                    position for position, step in enumerate(normalised, 1)
                    if position not in finished and position not in skipped
                    and any(dep in skipped for dep in step["dependsOn"])
                ]
                if cascade:
                    for position in cascade:
                        blocked_by = [d for d in normalised[position - 1]
                                      ["dependsOn"] if d in skipped]
                        skipped.append(position)
                        done.append({
                            "agent": self._agents[
                                normalised[position - 1]["agentId"]].name,
                            "agentId": normalised[position - 1]["agentId"],
                            "task": normalised[position - 1]["task"],
                            "success": False,
                            "output": "", "skipped": True,
                            "error": ("Skipped: step(s) "
                                      + ", ".join(str(b) for b in blocked_by)
                                      + " did not complete"),
                        })
                    continue
                blocked = [p for p in range(1, total + 1)
                           if p not in finished and p not in skipped]
                return {
                    "success": False,
                    "flowId": flow_id,
                    "goal": goal,
                    "steps": done,
                    "blocked": blocked,
                    "error": ("These steps can never run: their dependencies "
                              "did not complete: "
                              + ", ".join(str(b) for b in blocked)),
                }

            outcome = await self._run_wave(
                self._wave_members(ready, normalised),
                normalised, done, goal, total, provider, concurrent)
            done.extend(outcome["entries"])
            finished.update(outcome["finished"])
            skipped.extend(outcome["skipped"])
            # After the wave, not during it: a wave is the unit of progress a
            # resume can act on, and writing mid-wave would record steps whose
            # peers had not finished.
            self._checkpoint_save(flow_id, finished, skipped, done)
            jump_to = None

            if outcome.get("failure"):
                # A failure is only fatal if the step did not say where to go
                # instead. That is the point of onFail.
                step = normalised[(outcome.get("stoppedAt") or 1) - 1]
                target = step["onFail"]
                if target and budget_left > 0:
                    budget_left -= 1
                    jumps.append({"from": outcome.get("stoppedAt"),
                                  "to": target, "reason": "failed"})
                    log.info("Flow %s: step %s failed -> %s",
                             flow_id, outcome.get("stoppedAt"), target)
                    if isinstance(target, str):
                        if target == "stop":
                            return {
                                "success": False, "flowId": flow_id,
                                "goal": goal, "steps": done,
                                "skipped": skipped, "jumps": jumps,
                                "stoppedAt": outcome.get("stoppedAt"),
                                "error": outcome["failure"],
                            }
                        flow_done = True   # "done" — finish successfully
                        break
                    jump_to = target
                    continue
                if target and budget_left <= 0:
                    return {
                        "success": False, "flowId": flow_id, "goal": goal,
                        "steps": done, "skipped": skipped, "jumps": jumps,
                        "error": (f"Branch budget of {MAX_BRANCH_JUMPS} jumps "
                                  f"exhausted — the flow is looping."),
                    }
                return {
                    "success": False,
                    "flowId": flow_id,
                    "goal": goal,
                    "stoppedAt": outcome.get("stoppedAt"),
                    "steps": done,
                    "skipped": skipped,
                    "jumps": jumps,
                    "error": outcome["failure"],
                }

            # A completed step may still branch on its verdict.
            for position in outcome["finished"]:
                step = normalised[position - 1]
                if not (step["onPass"] or step["onFail"]):
                    continue
                # No `position in outcome["finished"]` re-check here: this
                # loop iterates `outcome["finished"]`, so it was always true.
                # The guard that matters is the one below — a step whose branch
                # target is None passes control on rather than deciding it.
                entries = [e for e in outcome["entries"]
                           if e.get("agentId") == step["agentId"]
                           or e.get("attempt") is not None
                           or e.get("gate")]
                passed = await self._branch_condition(step, entries, provider)
                target = step["onPass"] if passed else step["onFail"]
                if target is None:
                    continue
                if budget_left <= 0:
                    return {
                        "success": False, "flowId": flow_id, "goal": goal,
                        "steps": done, "skipped": skipped, "jumps": jumps,
                        "error": (f"Branch budget of {MAX_BRANCH_JUMPS} jumps "
                                  f"exhausted — the flow is looping."),
                    }
                budget_left -= 1
                jumps.append({"from": position, "to": target,
                              "reason": "passed" if passed else "failed"})
                log.info("Flow %s: step %d %s -> %s", flow_id, position,
                         "passed" if passed else "failed", target)
                if isinstance(target, str):
                    if target == "stop":
                        return {
                            "success": False, "flowId": flow_id, "goal": goal,
                            "steps": done, "skipped": skipped, "jumps": jumps,
                            "stoppedAt": position,
                            "error": (f"Step {position} branched to 'stop'."),
                        }
                    flow_done = True   # "done" — finish successfully
                    break
                jump_to = target
                break

        # Reached the end, so there is nothing left to resume.
        self._checkpoint_done(flow_id)
        return {
            "success": True,
            "flowId": flow_id,
            "goal": goal,
            "steps": done,
            "skipped": skipped,
            "jumps": jumps,
            "response": done[-1]["output"] if done else "",
        }

    async def _branch_condition(self, step: dict, entries: list[dict],
                                provider) -> bool:
        """Did this step pass, for the purpose of choosing a branch?

        Reuses the gate evaluation so a branch and a `retry` can never disagree
        about what "passed" means. A step with no gate branches on whether it
        succeeded at all, which is what `onPass`/`onFail` mean for a plain step.
        """
        condition = step["until"]
        if condition:
            if not entries:
                return False
            return await self._until_met(condition, entries, provider)
        return bool(entries) and all(e.get("success") for e in entries)

    @staticmethod
    def _wave_members(ready: list[int], normalised: list[dict]) -> list[int]:
        """Which ready steps may run together.

        Only steps that *declared* their independence share a wave. A step
        with no `dependsOn` is implicitly ordered after the steps listed above
        it — that is the behaviour every flow had before dependencies existed,
        and running such steps concurrently would silently remove the hand-off
        between them ("step 2 builds on step 1" would stop being true).

        So a plain step runs alone; steps whose dependencies were all declared
        explicitly are free to meet each other. Within a wave the outputs are
        collected by step number, so the reported order is still the list
        order.
        """
        if len(ready) <= 1:
            return list(ready)
        explicit = [p for p in ready if normalised[p - 1]["dependsOn"]]
        if len(explicit) <= 1:
            # At most one step is safe to overlap with anything, so run the
            # earliest alone and let the next wave decide again.
            return [min(ready)]
        return sorted(explicit)

    async def _run_wave(self, positions: list[int], normalised: list[dict],
                        done: list[dict], goal: str, total: int, provider,
                        concurrent: bool) -> dict:
        """Run one dependency wave. Returns entries / finished / skipped.

        Only one step at a time may be chosen first when the steps share the
        provider's attention, so a lone ready step and a wave of one are the
        same path. Several ready steps run together when the provider allows
        it, and the wave's results are ordered by step number so the reported
        order does not depend on which agent happened to finish first.
        """
        if len(positions) == 1 or not concurrent:
            entries: list[dict] = []
            finished: list[int] = []
            skipped: list[int] = []
            for position in positions:
                step = normalised[position - 1]
                outcome = await self._run_step_with_gate(
                    step, done + entries, goal, position, total, provider)
                step_entries, failure = outcome
                if failure and step["until"] is not None:
                    entries.extend(step_entries)
                    return {"entries": entries, "finished": finished,
                            "skipped": skipped, "failure": failure,
                            "stoppedAt": position}
                if failure and step.get("optional"):
                    skipped.append(position)
                    entries.append({
                        "agent": self._agents[step["agentId"]].name,
                        "agentId": step["agentId"],
                        "task": step["task"],
                        "success": False, "output": "",
                        "error": failure, "skipped": True,
                    })
                    continue
                if failure:
                    entries.extend(step_entries)
                    return {"entries": entries, "finished": finished,
                            "skipped": skipped, "failure": failure,
                            "stoppedAt": position}
                entries.extend(step_entries)
                finished.append(position)
            return {"entries": entries, "finished": finished,
                    "skipped": skipped, "failure": ""}

        # Concurrent wave. A step sees the outputs of the steps it depends on
        # that are also in this wave, so a declared dependency's output still
        # reaches it. Steps with no relation to each other share nothing and
        # wait for nothing, which is what lets independent branches overlap.
        #
        # Adjacency is deliberately NOT a wait: two plain steps (no dependsOn)
        # start together. They are still ordered by *position* when their
        # outputs are collected, so the reported order matches the list.
        wave_outputs: dict[int, list[dict]] = {}
        finished_gate = {p: asyncio.Event() for p in positions}

        async def one(position: int) -> tuple[int, list[dict], str]:
            step = normalised[position - 1]
            prefix: list[dict] = []
            for dep in step["dependsOn"]:
                if dep not in finished_gate:
                    continue          # finished in an earlier wave
                try:
                    await asyncio.wait_for(finished_gate[dep].wait(),
                                           timeout=600)
                except asyncio.TimeoutError:
                    log.warning("Wave step %d waited too long on step %d",
                                position, dep)
                prefix.extend(wave_outputs.get(dep, []))
            entries, failure = await self._run_step_with_gate(
                step, done + prefix, goal, position, total, provider)
            wave_outputs[position] = entries
            finished_gate[position].set()
            return position, entries, failure

        results = await asyncio.gather(*(one(p) for p in positions),
                                       return_exceptions=True)

        entries: list[dict] = []
        finished: list[int] = []
        skipped: list[int] = []
        failure = ""
        stopped_at = None
        for position, outcome in zip(positions, results):
            step = normalised[position - 1]
            if isinstance(outcome, Exception):
                entries.append({
                    "agent": self._agents[step["agentId"]].name,
                    "agentId": step["agentId"],
                    "task": step["task"], "success": False, "output": "",
                    "error": f"{type(outcome).__name__}: {outcome}",
                })
                if not failure:
                    failure = (f"Step {position} "
                               f"({self._agents[step['agentId']].name}) "
                               f"failed: {outcome}")
                    stopped_at = position
                continue
            _, step_entries, step_failure = outcome
            entries.extend(step_entries)
            if not step_failure:
                finished.append(position)
                continue
            if step["until"] is not None or not step.get("optional"):
                if not failure:
                    failure = step_failure
                    stopped_at = position
                continue
            skipped.append(position)
            entries.append({
                "agent": self._agents[step["agentId"]].name,
                "agentId": step["agentId"],
                "task": step["task"], "success": False, "output": "",
                "error": step_failure, "skipped": True,
            })
        if failure:
            return {"entries": entries, "finished": finished,
                    "skipped": skipped, "failure": failure,
                    "stoppedAt": stopped_at or positions[0]}
        return {"entries": entries, "finished": finished,
                "skipped": skipped, "failure": ""}

    async def _run_step(self, step: dict, done: list[dict], goal: str,
                        index: int, total: int,
                        provider) -> tuple[list[dict], str]:
        """Run one step, or one step plus its parallel peers.

        Returns (entries, error). The peers run concurrently and are given the
        same inputs but told about each other, then a merge agent reconciles
        their answers — otherwise "parallel" would just be two monologues.

        Concurrency is a choice, not a default: a local llamafile server serves
        one generation at a time, so two agents at once there would queue and
        look slow rather than fast. The peers still run in order under a local
        provider, and genuinely concurrently otherwise.
        """
        # Deduped against the step's own agent: naming it in `parallel` too
        # would otherwise run it twice and report the same answer as a "peer".
        peers = []
        for candidate in step["parallel"]:
            if candidate in self._agents and candidate != step["agentId"] \
                    and candidate not in peers:
                peers.append(candidate)
        local_only = self._single_generation_provider(provider)
        # One peer is enough to fan out — requiring two meant a step with a
        # single partner quietly fell through to the sequential branch, so the
        # two agents never learned about each other.
        if peers and not local_only:
            entries, error = await self._run_parallel_step(
                step, peers, done, goal, index, total, provider)
            if error:
                return entries, error
        else:
            entries = []
            # Peers still run under a single-generation provider — just in
            # order rather than at the same time. Skipping them entirely would
            # make "parallel" silently mean "the first agent only".
            all_ids = [step["agentId"]] + peers
            names = [self._agents[a].name for a in all_ids]
            for agent_id in all_ids:
                others = ", ".join(n for n in names
                                   if n != self._agents[agent_id].name)
                extra = (f"You are working alongside: {others}. They are "
                         f"answering the same step. Do your own part well "
                         f"rather than repeating theirs — their answers are "
                         f"merged afterwards.") if others else ""
                entries.append(await self._run_one(agent_id, step, done, goal,
                                                   index, total, provider, extra))
                if not entries[-1]["success"]:
                    return entries, (f"Step {index} ({entries[-1]['agent']}) "
                                     f"failed: {entries[-1]['error'] or 'unknown error'}")
            if peers and local_only:
                log.info("Parallel step %d run in order: %s serves one "
                         "generation at a time",
                         index, getattr(provider, "provider_id", "the provider"))

        failed = [e for e in entries if not e["success"]]
        if failed:
            return entries, (f"Step {index} ({failed[0]['agent']}) failed: "
                             f"{failed[0]['error'] or 'unknown error'}")

        # Only merge when a merge agent was named. Defaulting to the step's own
        # agent looked harmless but was not: the lead would then reconcile its
        # own answer with the peer's, ran a second time (reported as a
        # duplicate step), and its summary silently replaced both answers.
        model = step["merge"]
        if model and len(entries) > 1:
            merged = await self._merge_step(model, entries, done, goal, index,
                                            total, provider)
            entries.append(merged)
            if not merged["success"]:
                return entries, (f"Step {index} merge ({merged['agent']}) "
                                 f"failed: {merged['error'] or 'unknown error'}")
        return entries, ""

    async def _run_step_with_gate(self, step: dict, done: list[dict],
                                  goal: str, index: int, total: int,
                                  provider) -> tuple[list[dict], str]:
        """Run a step, honouring its `until` gate when it has one.

        A step with no `until` behaves exactly as before, which is what keeps
        existing flows unchanged. With one, the producer runs, the condition is
        evaluated, and a failed condition sends the work back to `retry` with
        the rejection attached — a review loop, bounded by `maxAttempts`.
        """
        condition = step["until"]
        if not condition:
            return await self._run_step(step, done, goal, index, total,
                                        provider)

        attempts = int(condition.get("maxAttempts") or 1)
        collected: list[dict] = []
        feedback = ""
        for attempt in range(1, attempts + 1):
            working = dict(step)
            if feedback:
                working["task"] = (f"{step['task']}\n\nYour previous attempt "
                                   f"was rejected. Fix this and try again:\n"
                                   f"{feedback}")
            entries, error = await self._run_step(working, done, goal, index,
                                                  total, provider)
            for entry in entries:
                entry["attempt"] = attempt
            collected.extend(entries)
            if error:
                return collected, error

            # The gate is a *separate* agent unless it is the producer itself.
            # Without running it there was nothing to evaluate: the condition
            # was compared against the producer's own output, so "contains
            # PASS" could never be satisfied and every gated step burned its
            # full attempt budget.
            gate_agent = condition.get("agent") or ""
            gate_entries = entries
            if gate_agent and gate_agent != step["agentId"]:
                verdict_text = self._gate_verdict(condition, entries)
                gate_task = (
                    f"Review this work and reply with your verdict.\n\n"
                    f"Original task: {step['task']}\n\n"
                    f"Work to review:\n{str((entries[-1] or {}).get('output') or '')}"
                )
                if verdict_text:
                    gate_task += f"\n\nPrevious review said:\n{verdict_text}"
                gate_task += ("\n\nIf it is acceptable, include the word PASS. "
                              "If not, say exactly what is wrong.")
                gate_result = await self.run_agent(gate_agent, gate_task,
                                                   provider)
                gate_entry = {
                    "agent": self._agents[gate_agent].name,
                    "agentId": gate_agent,
                    "task": f"gate: {step['task']}",
                    "success": bool(gate_result.get("success")),
                    "output": (gate_result.get("response") or "")
                              if gate_result.get("success") else "",
                    "error": gate_result.get("error"),
                    "gate": True,
                    "attempt": attempt,
                }
                collected.append(gate_entry)
                gate_entries = [gate_entry]
                if not gate_entry["success"]:
                    return collected, (
                        f"Step {index} gate ({gate_entry['agent']}) failed: "
                        f"{gate_entry['error'] or 'unknown error'}")

            if await self._until_met(condition, gate_entries, provider):
                return collected, ""

            feedback = self._gate_verdict(condition, gate_entries)
            log.info("Flow step %d failed its gate on attempt %d/%d",
                     index, attempt, attempts)

        return collected, (
            f"Step {index} did not satisfy its gate after {attempts} "
            f"attempt(s). Last verdict: {feedback or 'no output'}"
        )

    @staticmethod
    def _gate_verdict(condition: dict, entries: list[dict]) -> str:
        """The text handed back to the producer when the gate rejects it."""
        wanted = condition.get("agent") or ""
        chosen = None
        if wanted:
            for entry in reversed(entries):
                if entry.get("agentId") == wanted or entry.get("agent") == wanted:
                    chosen = entry
                    break
        if chosen is None:
            chosen = entries[-1] if entries else {}
        verdict = str(chosen.get("output") or chosen.get("error") or "").strip()
        return verdict[:600]

    @staticmethod
    def _single_generation_provider(provider) -> bool:
        """Whether this provider serves one request at a time.

        Delegates to the shared list in `providers.base` so the orchestrator and
        the Code page cannot disagree about which providers queue — two copies
        would drift, and here that means firing parallel requests at a server
        that can only hold them.
        """
        from backend.providers.base import concurrency_width
        return concurrency_width(provider, wanted=2) == 1

    async def _run_one(self, agent_id: str, step: dict, done: list[dict],
                       goal: str, index: int, total: int,
                       provider, extra: str) -> dict:
        """Run a single agent for a step and shape its entry."""
        agent = self._agents[agent_id]
        prompt = self._flow_prompt(step, done, goal, index, total, extra)
        # Notes another agent left during this wave. Read here, immediately
        # before the agent runs, so a warning that arrived seconds ago is in
        # front of it — which is the whole point of a mid-flight channel.
        try:
            fresh = self.notes_text(exclude=agent_id)
            if fresh:
                prompt += ("\n\nNotes from the other agents working right now "
                           "(they may affect your work):\n" + fresh)
        except Exception as e:  # noqa: BLE001
            log.debug("could not attach notes for %s: %s", agent.name, e)
        result = await self.run_agent(agent_id, prompt, provider)
        return {
            "agent": agent.name,
            "agentId": agent.id,
            "task": step["task"],
            "success": bool(result.get("success")),
            "output": (result.get("response") or "")
                      if result.get("success") else "",
            "error": result.get("error"),
            "parallel": agent.id != step["agentId"],
        }

    async def _run_parallel_step(self, step: dict, peers: list[str],
                                 done: list[dict], goal: str, index: int,
                                 total: int, provider) -> tuple[list[dict], str]:
        """Run the step and its peers at the same time."""
        agent_ids = [step["agentId"]] + [p for p in peers
                                         if p != step["agentId"]]
        names = [self._agents[a].name for a in agent_ids]
        runs = []
        for agent_id in agent_ids:
            others = ", ".join(n for n in names
                               if n != self._agents[agent_id].name)
            extra = (f"You are working in parallel with: {others}. They are "
                     f"answering the same step. Do your own part well rather "
                     f"than repeating theirs — their answers are merged "
                     f"afterwards.") if others else ""
            runs.append(self._run_one(agent_id, step, done, goal, index,
                                      total, provider, extra))
        results = await asyncio.gather(*runs, return_exceptions=True)
        entries: list[dict] = []
        for agent_id, outcome in zip(agent_ids, results):
            if isinstance(outcome, Exception):
                entries.append({
                    "agent": self._agents[agent_id].name,
                    "agentId": agent_id,
                    "task": step["task"],
                    "success": False,
                    "output": "",
                    "error": f"{type(outcome).__name__}: {outcome}",
                    "parallel": agent_id != step["agentId"],
                })
            else:
                entries.append(outcome)
        failed = [e for e in entries if not e["success"]]
        if failed:
            return entries, (f"Step {index} ({failed[0]['agent']}) failed: "
                             f"{failed[0]['error'] or 'unknown error'}")
        return entries, ""

    async def _merge_step(self, agent_id: str, entries: list[dict],
                          done: list[dict], goal: str, index: int, total: int,
                          provider) -> dict:
        """Have one agent reconcile several parallel answers into one."""
        agent = self._agents[agent_id]
        rendered = "\n\n".join(
            f"--- {e['agent']} ---\n{e['output']}" for e in entries if e["output"])
        parts = []
        if goal:
            parts.append(f"Overall goal: {goal}")
        parts.append(f"You are reconciling step {index} of {total}: "
                     f"{entries[0]['task']}")
        parts.append("Several agents answered this step in parallel. Merge "
                     "them into one answer: keep what is correct, resolve "
                     "disagreements, and drop duplication. Produce the single "
                     "result the rest of the flow should build on.")
        parts.append(rendered)
        shared = self._flow_transcript(done)
        if shared:
            parts.append("Work from earlier steps:\n\n" + shared)
        result = await self.run_agent(agent_id, "\n\n".join(parts), provider)
        return {
            "agent": agent.name,
            "agentId": agent.id,
            "task": f"merge: {entries[0]['task']}",
            "success": bool(result.get("success")),
            "output": (result.get("response") or "")
                      if result.get("success") else "",
            "error": result.get("error"),
            "merged": True,
        }

def _default_prompt(agent_type: str) -> str:
    prompts = {
        "coder": "You are an expert software engineer. Write clean, efficient, well-documented code. Prioritize best practices and security.",
        "writer": "You are a creative writer. Adapt your style to audience and purpose. Prioritize clarity and engagement.",
        "analyst": "You are a data analyst. Break down complex data into actionable insights. Use statistical rigor.",
        "planner": "You are a strategic planner. Create detailed, actionable plans. Consider dependencies, risks, and resources.",
        "researcher": "You are a web researcher. Find accurate information and summarize it clearly. Cite sources.",
        "devops": "You are a DevOps engineer. Provide practical solutions for deployment, CI/CD, and infrastructure.",
        "general": "You are a helpful AI assistant. Provide clear, practical answers to any question.",
    }
    return prompts.get(agent_type, prompts["general"])


# Singleton
swarm = SwarmOrchestrator()
