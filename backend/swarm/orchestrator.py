"""
Agent swarm orchestrator — spawn, manage, and coordinate sub-agents.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

log = logging.getLogger("addled.swarm")


class SwarmAgent:
    """A single swarm agent with its own context and tools."""

    def __init__(self, agent_id: str, name: str, agent_type: str, system_prompt: str, tools: list[str]):
        self.id = agent_id
        self.name = name
        self.type = agent_type
        self.system_prompt = system_prompt
        self.tools = tools
        self.status = "ready"
        self.current_task: str | None = None
        self.results: list[dict] = []
        self._provider = None

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
                persona=self.system_prompt,
                # None means every enabled skill. A list is an agent that was
                # deliberately given a narrower set.
                tools=self.tools,
                # A task is not a conversation: no chat history, no journal,
                # no long-term memory writes, no mood events.
                record=False,
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

    def spawn(self, name: str, agent_type: str = "general", system_prompt: str = "",
              tools: list[str] | None = None) -> SwarmAgent:
        """Create a new agent. tools=None means every enabled skill.

        This used to default to ["chat"], a skill that does not exist, so the
        dashboard badge read "chat" and the agent quietly had no tools at all.
        """
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        if not system_prompt:
            system_prompt = _default_prompt(agent_type)
        agent = SwarmAgent(agent_id, name, agent_type, system_prompt, tools)
        self._agents[agent_id] = agent
        log.info("Spawned agent: %s (%s)", name, agent_id)
        return agent

    def stop(self, agent_id: str) -> bool:
        """Stop and remove an agent."""
        if agent_id in self._tasks:
            self._tasks[agent_id].cancel()
            del self._tasks[agent_id]
        if agent_id in self._agents:
            del self._agents[agent_id]
            return True
        return False

    def list_agents(self) -> list[dict]:
        """List all agents with status."""
        return [
            {
                "id": a.id, "name": a.name, "type": a.type,
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
            return await running
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
