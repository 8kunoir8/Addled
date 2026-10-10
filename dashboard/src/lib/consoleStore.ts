// The console's shared state — every command Addled ran, and the ones it did not.
//
// Why a store rather than component state: `onNotification` keeps ONE handler
// per method (`notificationHandlers.set(method, handler)`), so the chat page and
// the layout cannot both subscribe to `chat.command` — the last to mount
// silently wins and the other goes deaf. The layout is always mounted, so it
// owns the subscription and publishes here; the drawer, the `/console` page and
// anything else read from this store.
//
// The same shape as `approvalsStore`, deliberately: the two solve the same
// problem (one always-mounted subscriber, many readers) and the next reader
// should recognise the pattern rather than learn a second one.
//
// (In-memory only; the backend is the durable half — `console.list` restores
// everything recorded before the dashboard was open.)

export type CommandEntry = {
  id: string;
  approval_id?: string | null;
  kind: 'shell' | 'argv' | 'session' | string;
  tool: string;
  command: string;
  argv?: string[] | null;
  cwd?: string | null;
  status: 'running' | 'ok' | 'failed' | 'timeout' | 'denied' | 'awaiting' | string;
  exit_code?: number | null;
  stdout: string;
  stderr: string;
  truncated?: boolean;
  duration_ms?: number | null;
  conversation?: string | null;
  session?: string;
  /** Fields whose value the backend masked. Present only when something was. */
  redacted?: string[];
  ts?: number;
  ts_updated?: number;
};

// Bounded, matching the backend's ring. A long session must not grow the
// renderer without limit, and the two caps agreeing means a reload cannot show
// more than the live view did.
const MAX_ENTRIES = 500;

let entries: CommandEntry[] = [];
const listeners = new Set<(entries: CommandEntry[]) => void>();

function emit() {
  listeners.forEach((l) => l(entries));
}

export function getCommands(): CommandEntry[] {
  return entries;
}

/**
 * Add or update one entry.
 *
 * Matched by `id`, which is the whole point: the gate records a command as
 * "awaiting", and the terminal later records the same command with its output.
 * Both carry the same id, so this UPDATES one row rather than adding a second —
 * which is what stops a command that was approved from appearing twice, once
 * stuck on "waiting".
 */
export function upsertCommand(entry: CommandEntry): void {
  if (!entry?.id) return;
  const at = entries.findIndex((e) => e.id === entry.id);
  if (at === -1) {
    entries = [...entries, entry].slice(-MAX_ENTRIES);
  } else {
    // Merged rather than replaced: a later write carries the output but not
    // necessarily the command, exactly as the backend merges.
    entries = entries.map((e, i) => (i === at ? { ...e, ...entry } : e));
  }
  emit();
}

/** Replace the whole list — used by the replay on connect. */
export function setCommands(next: CommandEntry[]): void {
  entries = Array.isArray(next) ? next.slice(-MAX_ENTRIES) : [];
  emit();
}

export function clearCommands(): void {
  entries = [];
  emit();
}

export function subscribeCommands(listener: (entries: CommandEntry[]) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The newest entry, for the collapsed bar's one-line summary.
 *
 * A separate function rather than a hook so the collapsed bar and the expanded
 * list agree on what "latest" means without either recomputing it.
 */
export function latestCommand(): CommandEntry | null {
  return entries.length ? entries[entries.length - 1] : null;
}

/** How many are waiting on the user. Drives the amber "needs you" badge. */
export function awaitingCount(): number {
  return entries.filter((e) => e.status === 'awaiting').length;
}
