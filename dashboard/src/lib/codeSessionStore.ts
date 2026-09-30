// Code sessions — the sidebar list on the Code page.
//
// The Code page had one implicit session: whatever was on screen. Closing the
// tab, navigating away, or switching workspace lost the conversation entirely,
// and there was no way to keep two lines of work apart. Claude Code's panel is
// built around the opposite: every piece of work is a named session you can
// return to.
//
// State lives in localStorage rather than in memory, unlike `chatStore`. That
// is deliberate: a code session is a unit of work that spans days, and losing
// it on a reload would defeat the point of naming it. It is still per-browser —
// the backend is not involved, because a session here is a UI grouping of
// prompts and their results, not a server-side conversation.

export type CodeRole = 'user' | 'assistant';

/** One prompt or one reply in a code session. */
export type CodeMessage = {
  id: string;
  role: CodeRole;
  content: string;
  timestamp: number;
  /** Set while a plan/edit for this message is still in flight. */
  pending?: boolean;
  /** Files the planner named for this turn, for display under the reply. */
  files?: string[];
  /** A short note about what happened (a plan was made, a diff was applied). */
  note?: string;
  /** Attachments the user sent with this message. */
  attachments?: CodeAttachment[];
};

/** An attachment in the composer or on a message. */
export type CodeAttachment = {
  name: string;
  kind: 'image' | 'text' | 'file';
  /** base64 for images, the file's text for text, absent for binary files. */
  data?: string;
  /** An object URL for the thumbnail. Revoked when the attachment is removed. */
  preview?: string;
  size?: number;
};

/**
 * One session: a named line of work in a workspace.
 *
 * `workspaceId` is recorded because a session belongs to the folder it was
 * started in — reopening a session for a different workspace would show a
 * conversation about files that are not the ones in front of you.
 */
export type CodeSession = {
  id: string;
  title: string;
  workspaceId: string;
  createdAt: number;
  updatedAt: number;
  /** True once the user pins it, so it sorts above the recents. */
  pinned?: boolean;
  messages: CodeMessage[];
};

const STORAGE_KEY = 'addled.code.sessions.v1';
const MAX_SESSIONS = 60;
/** Titles are derived from the first prompt, so they stay short. */
const TITLE_CHARS = 48;

function newId(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch { /* fall through to the time-based id */ }
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export function messageId(): string {
  return newId();
}

/**
 * A session title taken from the first thing the user asked.
 *
 * Derived rather than asked for: a prompt for a title before any work has
 * happened is friction, and the first request is almost always the best
 * description of what the session is for. Empty prompts get a neutral name
 * rather than an empty row in the list.
 */
export function titleFromPrompt(prompt: string): string {
  const clean = String(prompt || '').replace(/\s+/g, ' ').trim();
  if (!clean) return 'Untitled session';
  if (clean.length <= TITLE_CHARS) return clean;
  return clean.slice(0, TITLE_CHARS - 1).trimEnd() + '…';
}

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

let sessions: CodeSession[] = [];
let activeId = '';
const listeners = new Set<() => void>();

function load(): void {
  if (typeof window === 'undefined') return;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed?.sessions)) return;
    // Validate just enough that one malformed record cannot break the page.
    sessions = parsed.sessions.filter(
      (s: any) => s && typeof s.id === 'string' && Array.isArray(s.messages),
    );
    activeId = typeof parsed.activeId === 'string' ? parsed.activeId : '';
  } catch {
    // A corrupt store is treated as empty; it is a convenience, not data of
    // record, and refusing to render the page over it would be far worse.
    sessions = [];
    activeId = '';
  }
}

function persist(): void {
  if (typeof window === 'undefined') return;
  try {
    // Keep the newest MAX_SESSIONS, so a long-lived install cannot grow the
    // store without bound. Pinned sessions are kept first.
    const kept = [...sessions]
      .sort((a, b) => (Number(b.pinned) - Number(a.pinned))
        || (b.updatedAt - a.updatedAt))
      .slice(0, MAX_SESSIONS);
    window.localStorage.setItem(STORAGE_KEY,
      JSON.stringify({ sessions: kept, activeId }));
  } catch {
    // Quota or a disabled store. The page still works for this visit.
  }
}

function notify(): void {
  listeners.forEach(l => l());
}

/** Subscribe to any change in the session list. */
export function subscribeCodeSessions(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export function getSessions(): CodeSession[] {
  return sessions;
}

export function getActiveId(): string {
  return activeId;
}

export function getActiveSession(): CodeSession | null {
  return sessions.find(s => s.id === activeId) || null;
}

/** Newest first, pinned first — the order the sidebar shows. */
export function listSessions(): { pinned: CodeSession[]; recent: CodeSession[] } {
  const sorted = [...sessions].sort((a, b) => b.updatedAt - a.updatedAt);
  return {
    pinned: sorted.filter(s => s.pinned),
    recent: sorted.filter(s => !s.pinned),
  };
}

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

export function ensureLoaded(): void {
  if (!loadedOnce) { load(); loadedOnce = true; }
}
let loadedOnce = false;

/** Create a session for a workspace and make it active. */
export function createSession(workspaceId: string, title = 'New session'): CodeSession {
  ensureLoaded();
  const now = Date.now();
  const session: CodeSession = {
    id: newId(), title, workspaceId,
    createdAt: now, updatedAt: now, messages: [],
  };
  sessions = [session, ...sessions];
  activeId = session.id;
  persist(); notify();
  return session;
}

export function setActiveSession(id: string): void {
  ensureLoaded();
  if (!sessions.some(s => s.id === id)) return;
  activeId = id;
  persist(); notify();
}

/**
 * The session for a workspace, creating one if there is none.
 *
 * Used on bind: arriving at the Code page with a workspace already configured
 * should land in the work you were last doing there, not in an empty composer
 * beside a list you have to pick from manually.
 */
export function sessionForWorkspace(workspaceId: string): CodeSession {
  ensureLoaded();
  const existing = sessions
    .filter(s => s.workspaceId === workspaceId)
    .sort((a, b) => b.updatedAt - a.updatedAt)[0];
  if (existing) {
    activeId = existing.id;
    persist(); notify();
    return existing;
  }
  return createSession(workspaceId);
}

export function appendMessage(sessionId: string, message: CodeMessage): void {
  ensureLoaded();
  const session = sessions.find(s => s.id === sessionId);
  if (!session) return;
  session.messages = [...session.messages, message];
  session.updatedAt = message.timestamp || Date.now();
  persist(); notify();
}

export function updateMessage(
  sessionId: string,
  messageIdToEdit: string,
  patch: Partial<CodeMessage>,
): void {
  ensureLoaded();
  const session = sessions.find(s => s.id === sessionId);
  if (!session) return;
  session.messages = session.messages.map(m =>
    m.id === messageIdToEdit ? { ...m, ...patch } : m);
  session.updatedAt = Date.now();
  persist(); notify();
}

export function renameSession(id: string, title: string): void {
  ensureLoaded();
  const session = sessions.find(s => s.id === id);
  if (!session) return;
  const clean = String(title || '').trim();
  if (!clean) return;
  session.title = clean.slice(0, 120);
  session.updatedAt = Date.now();
  persist(); notify();
}

export function togglePinned(id: string): void {
  ensureLoaded();
  const session = sessions.find(s => s.id === id);
  if (!session) return;
  session.pinned = !session.pinned;
  persist(); notify();
}

export function deleteSession(id: string): void {
  ensureLoaded();
  sessions = sessions.filter(s => s.id !== id);
  if (activeId === id) {
    const next = [...sessions].sort((a, b) => b.updatedAt - a.updatedAt)[0];
    activeId = next ? next.id : '';
  }
  persist(); notify();
}

/** Clear the messages of a session but keep it, for "start over". */
export function clearSessionMessages(id: string): void {
  ensureLoaded();
  const session = sessions.find(s => s.id === id);
  if (!session) return;
  session.messages = [];
  session.updatedAt = Date.now();
  persist(); notify();
}
