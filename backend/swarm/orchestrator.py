"""
Agent swarm orchestrator — spawn, manage, and coordinate sub-agents.
"""

from __future__ import annotations

import asyncio
import logging
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
        """Execute a task using this agent's system prompt + tools."""
        self.status = "running"
        self.current_task = task
        self._provider = provider

        try:
            if provider:
                from backend.providers import router
                messages = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": task},
                ]
                result = await provider.chat(
                    messages,
                    model=router.for_provider(provider, "reasoning"),
                    max_tokens=2000)
                if result.ok:
                    self.results.append({"task": task, "response": result.response, "timestamp": __import__('time').time()})
                    self.status = "ready"
                    self.current_task = None
                    return {"success": True, "response": result.response, "agent": self.name}
        except Exception as e:
            log.error("Agent %s task failed: %s", self.name, e)

        self.status = "error"
        return {"success": False, "error": "Agent execution failed", "agent": self.name}


class SwarmOrchestrator:
    """Manages multiple swarm agents."""

    def __init__(self):
        self._agents: dict[str, SwarmAgent] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def spawn(self, name: str, agent_type: str = "general", system_prompt: str = "",
              tools: list[str] | None = None) -> SwarmAgent:
        """Create a new agent."""
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        if not system_prompt:
            system_prompt = _default_prompt(agent_type)
        agent = SwarmAgent(agent_id, name, agent_type, system_prompt, tools or ["chat"])
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
                "tools": a.tools, "results": len(a.results),
            }
            for a in self._agents.values()
        ]

    def get_agent(self, agent_id: str) -> SwarmAgent | None:
        return self._agents.get(agent_id)

    async def run_agent(self, agent_id: str, task: str, provider=None) -> dict:
        """Run a task on a specific agent."""
        agent = self._agents.get(agent_id)
        if not agent:
            return {"success": False, "error": f"Agent not found: {agent_id}"}
        return await agent.run_task(task, provider)

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
