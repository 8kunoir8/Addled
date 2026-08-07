"""
Character state enumeration — maps agent internal state to visual state.

12 states total: 10 from Vox + 2 new (WORKING, DREAMING).
"""

from enum import Enum, auto


class CharacterState(Enum):
    IDLE = auto()             # Nothing happening → breathing + wander
    LISTENING = auto()        # Wake word active → glow + eyes open
    OBSERVING = auto()        # Actively watching screen → patrol
    THINKING = auto()         # Processing LLM response → pulse + progress ring
    HAS_SUGGESTION = auto()   # Has something to say → glow + approach
    ACTING = auto()           # Executing action → fly to target
    SPEAKING = auto()         # TTS playing → gentle float
    SLEEPING = auto()         # Meeting/gaming/away → corner + zzz
    BLOCKED = auto()          # Privacy guard active → retreat + dim
    ERROR = auto()            # Something went wrong → shake + red tint
    WORKING = auto()          # Background goal executing → gear particles
    DREAMING = auto()         # Memory consolidation → float + sparkle


# Map agent internal state strings to character visual states
AGENT_TO_CHARACTER: dict[str, CharacterState] = {
    "idle":               CharacterState.IDLE,
    "listening":          CharacterState.LISTENING,
    "observing":          CharacterState.OBSERVING,
    "processing_query":   CharacterState.THINKING,
    "thinking":           CharacterState.THINKING,
    "has_suggestion":     CharacterState.HAS_SUGGESTION,
    "executing_action":   CharacterState.ACTING,
    "acting":             CharacterState.ACTING,
    "speaking_tts":       CharacterState.SPEAKING,
    "speaking":           CharacterState.SPEAKING,
    "in_meeting":         CharacterState.SLEEPING,
    "in_gaming":          CharacterState.SLEEPING,
    "user_away":          CharacterState.SLEEPING,
    "sleeping":           CharacterState.SLEEPING,
    "privacy_guard":      CharacterState.BLOCKED,
    "error_state":        CharacterState.ERROR,
    "error":              CharacterState.ERROR,
    "working":            CharacterState.WORKING,
    "goal_executing":     CharacterState.WORKING,
    "dreaming":           CharacterState.DREAMING,
    "memory_consolidation": CharacterState.DREAMING,
}


class StateMachine:
    """Maps agent internal state to character visual state."""

    def __init__(self):
        self.current = CharacterState.IDLE
        self.previous = CharacterState.IDLE

    def transition(self, agent_state: str) -> CharacterState:
        new_state = AGENT_TO_CHARACTER.get(agent_state, CharacterState.IDLE)
        if new_state != self.current:
            self.previous = self.current
            self.current = new_state
        return self.current

    def set_direct(self, state: CharacterState):
        if state != self.current:
            self.previous = self.current
            self.current = state
