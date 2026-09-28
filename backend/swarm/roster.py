"""
The roster — swarm agents that persist, and know how to do their job.

Why this exists
---------------
Every swarm agent used to be ephemeral. `spawn()` put a `SwarmAgent` in a dict,
the dict died with the process, and the only things an agent carried were a
one-line type prompt and a tool list. So "my Reviewer agent" could not exist:
there was nothing to name, nothing to reuse, and nothing that remembered how you
like a review done.

This module keeps three things that make an agent *an agent* rather than a
one-shot call:

- **An identity that survives a restart.** Definitions live in
  `memory/swarm/roster.json`; a spawn can come from the file instead of from
  scratch.
- **A brief.** A few standing sentences — tone, red lines, who to escalate to.
  Read before every task, the way a person briefs a colleague once rather than
  every time.
- **Learned rules.** Corrections the user made ("proposals are one page") that
  are read before every future task. This is the part that compounds.

And two things that make it affordable to run:

- **A model per agent.** A grunt desk can run on the local model while the
  reasoning desk runs on a cloud one.
- **Skills per agent**, naming guideline packs already in the repo rather than a
  second skill format invented here.

Everything is plain JSON and plain Markdown in the user's own memory folder, so
it can be edited by hand and is never overwritten by an update.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.swarm.roster")

_DIR = Path(__file__).resolve().parent.parent / "memory" / "swarm"
ROSTER_PATH = _DIR / "roster.json"
FEEDBACK_DIR = _DIR / "feedback"

# Deliberately small: a brief is a few sentences, not a document. Past this it
# stops being read as "standing instruction" and starts eating the task.
MAX_BRIEF_CHARS = 2000
# The most recent standing rules carried per agent. Newest last, the way a
# correction added yesterday matters more than one from last month.
MAX_RULES = 15

# What each agent type is, and what it should be offered by default. The skills
# name *guideline packs that already exist* (backend/guidelines/packs.py) and
# *tool skills* (backend/skills/), so this adds no new format — it just says
# which of the things Addled already has belong to which kind of desk.
DEFAULT_TYPES: dict[str, dict] = {
    "general": {
        "role": "General Assistant",
        "does": "Takes any task that does not fit a specialised desk.",
        "prompt": ("You are a capable general assistant. Answer clearly and "
                   "practically, and use your tools rather than guessing."),
        # The two habits that apply to any work at all: find the cause before
        # fixing, and verify before claiming done.
        "skills": ["systematic-debugging", "verification-before-completion",
                   "email_list", "email_search", "email_send",
                   "transcribe_audio"],
    },
    "coder": {
        "role": "Software Engineer",
        "does": "Writes, reads and fixes code; runs the project's own checks.",
        "prompt": ("You are an expert software engineer. Write clean, "
                   "efficient, well-documented code; prefer the smallest "
                   "change that solves the problem. Read before you write, and "
                   "verify with the project's own test command when one exists."),
        "skills": ["code_read", "search_in_files", "verify_code", "run_command",
                   "guidelines:karpathy", "guidelines:ponytail",
                   "test-driven-development", "systematic-debugging",
                   "executing-plans", "domain-modeling", "docx", "pdf"],
    },
    "reviewer": {
        "role": "Code Reviewer",
        "does": "Reviews changes for correctness, risk and missed cases.",
        "prompt": ("You are a rigorous code reviewer. Look for what is wrong, "
                   "not for what to praise. Name the file and the line, say "
                   "why it matters, and separate must-fix from nice-to-have. If "
                   "the change is sound, say so plainly rather than inventing a "
                   "problem."),
        "skills": ["code_read", "search_in_files", "file_info",
                   "guidelines:karpathy", "requesting-code-review",
                   "receiving-code-review",
                   "verification-before-completion"],
    },
    "analyst": {
        "role": "Data Analyst",
        "does": "Breaks data down into findings a decision can be made from.",
        "prompt": ("You are a data analyst. Break complex material into the "
                   "findings that matter, show the reasoning that got you "
                   "there, and say what is uncertain. Never invent a number."),
        # xlsx for real spreadsheets and pdf for source documents, plus the
        # evidence habit: state nothing as settled without checking it.
        "skills": ["read_file", "search_in_files", "xlsx", "pdf", "query",
                   "read-file", "systematic-debugging",
                   "verification-before-completion"],
    },
    "researcher": {
        "role": "Researcher",
        "does": "Finds accurate information and summarises it with sources.",
        "prompt": ("You are a researcher. Find accurate information with your "
                   "tools, prefer primary sources, and cite where each claim "
                   "came from. Say plainly when something could not be "
                   "confirmed."),
        "skills": ["web_search", "read_file", "wiki_search", "wiki_read",
                   "research", "brainstorming", "pdf", "query"],
    },
    "writer": {
        "role": "Writer",
        "does": "Writes and reshapes prose for a given audience.",
        "prompt": ("You are a writer. Match the register the audience expects, "
                   "lead with the point, and cut anything that does not carry "
                   "weight. Prefer concrete over abstract."),
        # writing-guidelines is the craft; docx/pptx/pdf are the delivery
        # formats a writer is actually asked to produce.
        "skills": ["read_file", "wiki_search", "sop_lookup",
                   "writing-guidelines", "docx", "pptx", "pdf",
                   "doc-coauthoring", "internal-comms", "email_send"],
    },
    "planner": {
        "role": "Planner",
        "does": "Turns a goal into ordered, actionable steps with dependencies.",
        "prompt": ("You are a planner. Turn the goal into steps that can be "
                   "executed in order, name the dependencies and the risks, and "
                   "say what you could not determine rather than inventing it. "
                   "A step that cannot be acted on alone is not a step."),
        "skills": ["sop_lookup", "sop_list", "wiki_search",
                   "writing-plans", "brainstorming"],
    },
    "devops": {
        "role": "DevOps Engineer",
        "does": "Deployment, CI/CD, infrastructure and incident work.",
        "prompt": ("You are a DevOps engineer. Prefer the smallest change that "
                   "fixes the problem, make the rollback obvious, and never run "
                   "something destructive without saying what it will do first."),
        "skills": ["run_command", "read_file", "session_open", "session_send",
                   "guidelines:ponytail", "systematic-debugging",
                   "finishing-a-development-branch"],
    },
    "qa": {
        "role": "Quality Assurance",
        "does": "Checks that the result actually holds up before it ships.",
        "prompt": ("You are a QA checker. Try to break the thing. Check the "
                   "claims against the evidence, the edge cases nobody "
                   "mentioned, and the numbers. Report what failed; do not "
                   "approve something you could not test."),
        "skills": ["verify_code", "read_file", "search_in_files",
                   "verification-before-completion",
                   "test-driven-development", "webapp-testing",
                   "diagnosing-bugs"],
    },
}

def _ensure_dirs() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.debug("could not create the swarm memory folder: %s", e)

def load() -> dict:
    """The saved roster. Never raises — a corrupt file reads as empty."""
    _ensure_dirs()
    try:
        if ROSTER_PATH.is_file():
            data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("agents"), list):
                return data
    except (OSError, json.JSONDecodeError) as e:
        log.debug("roster unreadable, starting empty: %s", e)
    return {"agents": [], "version": 1}

def save(data: dict) -> None:
    _ensure_dirs()
    try:
        ROSTER_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    except OSError as e:
        log.warning("could not write the roster: %s", e)

def normalise(entry: dict) -> dict:
    """Shape one roster entry, filling what a caller left out.

    The unknown-type fallback is deliberate: a typo in `type` costs you the
    default prompt, not the agent.
    """
    spec = DEFAULT_TYPES.get(str(entry.get("type") or "general"),
                             DEFAULT_TYPES["general"])
    agent_type = str(entry.get("type") or "general")
    if agent_type not in DEFAULT_TYPES:
        agent_type = "general"
    tools = entry.get("tools")
    if not isinstance(tools, list) or not tools:
        # None means "every enabled skill", which is the honest default for an
        # agent the user did not narrow by hand.
        tools = tools if isinstance(tools, list) else None
    return {
        "id": str(entry.get("id") or f"agent_{int(time.time() * 1000) % 100000}"),
        "name": str(entry.get("name") or spec["role"]),
        "type": agent_type,
        "role": str(entry.get("role") or spec["role"]),
        "does": str(entry.get("does") or spec["does"]),
        "prompt": str(entry.get("prompt") or spec["prompt"]),
        "brief": str(entry.get("brief") or ""),
        "tools": tools,
        "skills": list(entry.get("skills") or spec.get("skills") or []),
        "model": str(entry.get("model") or ""),
        "provider": str(entry.get("provider") or ""),
        "isLead": bool(entry.get("isLead") or entry.get("lead") or False),
        "created": float(entry.get("created") or time.time()),
    }

def get(agent_id: str) -> dict | None:
    for entry in load().get("agents", []):
        if str(entry.get("id")) == str(agent_id):
            return normalise(entry)
    return None

def upsert(entry: dict) -> dict:
    """Add or replace an agent definition, returning the stored shape."""
    data = load()
    shaped = normalise(entry)
    agents = [a for a in data.get("agents", [])
              if str(a.get("id")) != shaped["id"]]
    agents.append(shaped)
    data["agents"] = agents
    save(data)
    return shaped

def remove(agent_id: str) -> bool:
    data = load()
    before = len(data.get("agents", []))
    data["agents"] = [a for a in data.get("agents", [])
                      if str(a.get("id")) != str(agent_id)]
    save(data)
    return len(data["agents"]) < before

def definitions() -> list[dict]:
    return [normalise(a) for a in load().get("agents", [])]

def type_catalogue() -> list[dict]:
    """The built-in types and their default skills, for the UI to offer."""
    return [{"type": name, **{k: v for k, v in spec.items()}}
            for name, spec in DEFAULT_TYPES.items()]

# ── learned rules ─────────────────────────────────────────────────────────

def _feedback_path(agent_id: str) -> Path:
    safe = "".join(c for c in str(agent_id) if c.isalnum() or c in "-_") or "agent"
    return FEEDBACK_DIR / f"{safe}.md"

def rules_for(agent_id: str, limit: int = MAX_RULES) -> list[str]:
    """The most recent standing rules for an agent, newest last."""
    path = _feedback_path(agent_id)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rules: list[str] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # The file has two sections; only "Standing rules" is carried into
            # every future task. A one-off belongs to the task that earned it.
            section = stripped.lstrip("#").strip().lower()
            continue
        if section.startswith("standing") and stripped.startswith(("-", "*")):
            rules.append(stripped.lstrip("-* ").strip())
    return rules[-limit:]

def add_rule(agent_id: str, rule: str) -> dict:
    """Record a standing rule for an agent."""
    rule = str(rule or "").strip()
    if not rule:
        return {"success": False, "error": "The rule was empty."}
    _ensure_dirs()
    path = _feedback_path(agent_id)
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    if not existing.strip():
        existing = ("# Feedback\n\n## Standing rules\n\n"
                    "## One-offs\n\n")
    lines = existing.splitlines()
    out: list[str] = []
    inserted = False
    for line in lines:
        if (not inserted and line.strip().lower().startswith("## one-off")):
            out.append(f"- {rule}")
            inserted = True
        out.append(line)
    if not inserted:
        out.append(f"- {rule}")
    try:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "agentId": agent_id, "rule": rule}

def add_one_off(agent_id: str, note: str) -> dict:
    """Record a correction that applies only to the task it came from."""
    note = str(note or "").strip()
    if not note:
        return {"success": False, "error": "The note was empty."}
    _ensure_dirs()
    path = _feedback_path(agent_id)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- {note}\n")
    except OSError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "agentId": agent_id, "oneOff": note}

def build_persona(agent, base_prompt: str = "") -> str:
    """The full instructions for one task: who it is, its brief, its rules.

    Order matters and mirrors how a person is briefed: identity, then the
    standing brief, then the corrections most recently learned, then the skills
    they own. The task itself is appended by the caller.
    """
    parts: list[str] = []
    identity = str(getattr(agent, "role", "") or "").strip()
    does = str(getattr(agent, "does", "") or "").strip()
    name = str(getattr(agent, "name", "") or "").strip()
    head = base_prompt or str(getattr(agent, "system_prompt", "") or "")
    if name and identity:
        parts.append(f"You are {name} — {identity}. {does}".strip())
    if head:
        parts.append(head)

    brief = str(getattr(agent, "brief", "") or "").strip()
    if brief:
        parts.append("Standing brief (always applies):\n" + brief[:MAX_BRIEF_CHARS])

    rules = rules_for(str(getattr(agent, "id", "")))
    if rules:
        parts.append("How this user wants it done, learned from their "
                     "corrections (newest last):\n"
                     + "\n".join(f"- {r}" for r in rules))

    skills = getattr(agent, "skills", None) or []
    pack_ids = [s.split(":", 1)[1] for s in skills
                if isinstance(s, str) and s.startswith("guidelines:")]
    if pack_ids:
        try:
            from backend.guidelines import packs, store
            for pack_id in pack_ids:
                if pack_id not in packs.PACKS:
                    continue
                body = store.text(pack_id)
                if body.strip():
                    title = packs.PACKS[pack_id].get("title", pack_id)
                    parts.append(f"### {title}\n{body.strip()}")
        except Exception as e:  # noqa: BLE001
            log.debug("could not inject guideline packs: %s", e)
    return "\n\n".join(parts)

def skill_names(entry: dict) -> list[str]:
    """The tool skills an entry names (guideline packs excluded).

    Used to narrow the catalogue an agent is offered, so a desk does not carry
    fifty tools it will never call.
    """
    skills = entry.get("skills") or []
    names = [s for s in skills
             if isinstance(s, str) and not s.startswith("guidelines:")]
    return names

# The desks seeded on a fresh install, one per built-in type. Only names that
# make sense as a standing team: a user who has never opened the Agents page
# should still find a usable office rather than an empty room.
SEED_AGENTS = [
    ("Planner", "planner"),
    ("Researcher", "researcher"),
    ("Coder", "coder"),
    ("Reviewer", "reviewer"),
    ("QA", "qa"),
]

def seed_if_empty() -> int:
    """Create the default desks when the roster has never been filled.

    Deliberately only when it is EMPTY: a user who deleted a desk meant to
    delete it, and re-adding it on every start would be a fight. Returns how
    many were created.
    """
    data = load()
    if data.get("agents"):
        return 0
    created = 0
    for name, agent_type in SEED_AGENTS:
        spec = DEFAULT_TYPES.get(agent_type, DEFAULT_TYPES["general"])
        upsert({
            "id": f"agent_{agent_type}",
            "name": name,
            "type": agent_type,
            "role": spec["role"],
            "does": spec["does"],
            "prompt": spec["prompt"],
            "skills": spec.get("skills") or [],
        })
        created += 1
    log.info("Seeded %d default swarm agent(s)", created)
    return created
