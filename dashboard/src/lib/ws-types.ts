// WebSocket message types for the Addled dashboard

export interface WSMessage {
  jsonrpc: '2.0';
  id?: string;
  method?: string;
  result?: any;
  error?: { code: number; message: string };
  params?: any;
}

// System
export interface SystemStatus {
  engineState: string;
  provider: string;
  agentName: string;
  characterState: string;
  uptime: number;
}

export interface Provider {
  id: string;
  name: string;
  is_builtin: boolean;
  is_active: boolean;
  has_key: boolean;
  vision: boolean;
  base_url: string;
  models: string[];
}

// Chat
export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: number;
  tokens?: { in: number; out: number };
}

// Goals
export interface GoalStep {
  index: number;
  description: string;
  action_type: string;
  params: Record<string, any>;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  result?: any;
  dependencies?: number[];
}

export interface Goal {
  id: string;
  title: string;
  description: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  priority: 'low' | 'normal' | 'high' | 'urgent';
  created_at: number;
  plan: {
    steps: GoalStep[];
  };
}

// Agents
export interface AgentInfo {
  id: string;
  name: string;
  emoji: string;
  type: string;
  status: 'offline' | 'ready' | 'running' | 'error';
  currentTask?: string;
  tools: string[];
}

// Character
export interface CharacterConfig {
  shape: string;
  color: string;
  glow_intensity: number;
  eyes: boolean;
  size: number;
  movement_speed: string;
  idle_wander_range: number;
  preferred_corner: string;
}

// Calendar
export interface CalendarEvent {
  id: string;
  title: string;
  start: string;
  end: string;
  location?: string;
  description?: string;
  source: string;
}

// Settings
export interface AppSettings {
  agent_name: string;
  character: CharacterConfig;
  providers: {
    active: string;
    priority: string[];
  };
  chat: {
    max_tokens: number;
    temperature: number;
    system_prompt: string;
    context_messages: number;
  };
  voice: {
    tts_engine: string;
    wake_word: string;
    auto_tts: boolean;
    language: string;
  };
  safety: {
    file_access_mode: string;
    kill_switch_hotkey: string;
    quiet_hours_start: string;
    quiet_hours_end: string;
  };
  notifications: {
    bubble_duration_s: number;
    show_character_state: boolean;
  };
}
