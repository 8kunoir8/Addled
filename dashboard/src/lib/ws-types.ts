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

// Hugging Face (Local) readiness, from localLlm.status -> .hf
// `deps_ready` covers torch/transformers (enough to chat). `vision_ready`
// additionally covers einops/timm, which Florence-2 imports at call time —
// the two can differ, and the UI must not conflate them.
export interface HfStatus {
  model_id: string;
  deps_ready: boolean;
  error: string;
  vision_ready: boolean;
  vision_error: string;
  vision_missing: string[];
  downloaded: boolean;
  root: string;
  installing: boolean;
}

export interface Provider {
  id: string;
  name: string;
  is_builtin: boolean;
  is_active: boolean;
  is_selected?: boolean;
  has_key: boolean;
  vision: boolean;
  base_url: string;
  models: string[];
  /** Local runtimes (llamafile / Hugging Face) need no API key. */
  local?: boolean;
  requires_key?: boolean;
  installed?: boolean;
  running?: boolean;
  needs_download?: boolean;
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
    smart_default?: boolean;
    builtin?: Record<string, {
      name?: string;
      base_url?: string;
      api_key?: string;
      default_model?: string;
      models?: string[];
      vision?: boolean;
      local?: boolean;
      [key: string]: unknown;
    }>;
    custom?: unknown[];
  };
  chat: {
    max_tokens: number;
    temperature: number;
    system_prompt: string;
    context_messages: number;
  };
  voice: {
    tts_engine: string;
    tts_voice: string;
    kokoro_voice: string;
    stt_engine: string;
    stt_model: string;
    wake_word: string;
    auto_tts: boolean;
    vad_enabled: boolean;
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

// Code page -----------------------------------------------------------------
// These mirror what `backend/ws_server.py` actually returns. The Code page used
// to type every one of these as `any` and read only the fields it needed, which
// is how it came to discard the `git` and `verify` results the backend was
// already computing — an untyped response cannot go visibly unused.

/** `code.git.status` — mirrors `backend/codemode/gitops.py:status`. */
export interface CodeGitStatus {
  isRepo: boolean;
  /** Whether a `git` binary was found at all. False on a machine without git. */
  available?: boolean;
  branch?: string;
  changed?: string[];
  untracked?: string[];
  clean?: boolean;
  error?: string;
}

/** `code.git.diff` — `{ diff }` around `gitops.diff`. */
export interface CodeGitDiff {
  diff: string;
  error?: string;
}

/** `code.git.revert` — note `success`, not `ok`; the ws layer renames it. */
export interface CodeGitRevert {
  success: boolean;
  /** The new inverse commit's sha, so the undo is itself revertible. */
  sha?: string;
  error?: string;
}

/**
 * `code.verify` / the `verify` field of `code.applyPlan` — mirrors
 * `backend/codemode/verify.py:run_verification` plus `verdict_line`.
 */
export interface CodeVerifyResult {
  ok: boolean;
  /** False when no check could be detected, which is not the same as passing. */
  ran: boolean;
  command?: string;
  kind?: string;
  reason?: string;
  /**
   * True when the check RAN but collected no tests.
   *
   * Different from a failure: most runners exit non-zero when they find
   * nothing, so without this the UI would tell the user their code is broken
   * when in fact there was simply nothing to run.
   */
  noTests?: boolean;
  /** A ready-to-render sentence from `verdict_line`. */
  verdict?: string;
  output?: string;
}

/** One file that `code.applyPlan` actually wrote. */
export interface CodeAppliedFile {
  index: number;
  filePath: string;
  created?: boolean;
  /** The `.bak` beside the file, when a backup was kept. */
  backup?: string;
}

/** A file that was refused, or never reached because an earlier step failed. */
export interface CodeSkippedFile {
  index: number;
  filePath: string;
  reason: string;
}

/**
 * `code.git` inside a `code.applyPlan` response: whether Addled committed the
 * change, and which commit, so the UI can offer "undo this" and name what
 * `git log` will show.
 */
export interface CodeGitCommitInfo {
  used: boolean;
  sha?: string;
  reason?: string;
}

/** `code.applyPlan` — mirrors `code_apply_plan` in `ws_server.py`. */
export interface CodeApplyPlanResult {
  applied: CodeAppliedFile[];
  skipped: CodeSkippedFile[];
  failed: { index: number; filePath: string; reason: string } | null;
  error?: string;
  count?: number;
  /** Already returned by the backend; the page must not ignore it. */
  git?: CodeGitCommitInfo;
  verify?: CodeVerifyResult;
}

/** `code.apply` — the single-file variant, which reports the same two fields. */
export interface CodeApplyResult {
  success: boolean;
  created?: boolean;
  backup?: string;
  error?: string;
  git?: CodeGitCommitInfo;
  verify?: CodeVerifyResult;
}

