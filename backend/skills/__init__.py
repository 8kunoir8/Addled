"""Skills — provider-agnostic function-calling layer with auto-forging."""
from backend.skills.registry import SkillRegistry, SkillDefinition, SkillResult, skill_registry
from backend.skills.tool_loop import chat_with_tools, execute_skill
from backend.skills.market import market  # loads installed market skills
from backend.skills.forge import SkillForge, skill_forge
