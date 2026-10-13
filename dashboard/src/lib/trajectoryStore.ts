// The trajectory's shared state — what the agent DID, step by step.
//
// Why a store rather than component state: `onNotification` keeps ONE handler
// per method (`notificationHandlers.set(method, handler)`), so two components
// cannot both subscribe to `chat.trajectory` — the last to mount silently wins
// and the other goes deaf. The layout is always mounted, so it owns the
// subscription and publishes here; the page reads from this store.
//
// The same shape as `consoleStore` and `approvalsStore`, deliberately: the
// three solve the same problem (one always-mounted subscriber, many readers)
// and the next reader should recognise the pattern rather than learn a third
// one.
//
// Where the console shows one row per COMMAND, this shows one row per TOOL
// CALL, and groups them by turn. A turn that searched, read twice and wrote has
// no single command to show — which is the whole reason this is not the console.
//
// (In-memory only; the backend is the durable half — `trajectory.list` restores
// everything recorded before the dashboard was open.)

export type TrajectoryStep = {
  id: string;
  turn_id: string;
  /** Position within the turn. Makes a dropped step visible. */
  seq: number;
  tool: string;
  arguments?: string | Record<string, unknown> | null;
  status: 'running' | 'ok' | 'failed' | string;
  success?: boolean | null;
  result?: string | Record<string, unknown> | null;
  error?: string | null;
  duration_ms?: number | null;
  truncated?: boolean;
  conversation?: string | null;
  ts?: number;
  ts_done?: number;
};

export type TrajectoryTurn = {
  turn_id: string;
  steps: TrajectoryStep[];
  count?: number;
  /** Summed from the steps the backend kept — never a wall-clock guess. */
  duration_ms?: number;
  failed?: number;
  ts?: number;
};

// Bounded, matching the backend's ring. A long session must not grow the
// renderer without limit, and the two caps agreeing means a reload cannot show
// more than the live view did.
const MAX_ENTRIES = 500;

let steps: TrajectoryStep[] = [];
const listeners = new Set<(steps: TrajectoryStep[]) => void>();

function emit() {
  listeners.forEach((l) => l(steps));
}

export function getSteps(): TrajectoryStep[] {
  return steps;
}

/**
 * Add or update one step.
 *
 * Matched by `id` for the same reason the console matches by id: the store
 * records a step as "running" before the call, then the same step again with
 * its result and duration. Both carry the same id, so this UPDATES one row
 * rather than adding a second — which is what stops every tool call appearing
 * twice, once stuck on "running".
 */
export function upsertStep(step: TrajectoryStep): void {
  if (!step?.id) return;
  const at = steps.findIndex((s) => s.id === step.id);
  if (at === -1) {
    steps = [...steps, step].slice(-MAX_ENTRIES);
  } else {
    // Merged rather than replaced: the finishing write carries the result but
    // not necessarily the tool name or arguments, exactly as the backend merges.
    steps = steps.map((s, i) => (i === at ? { ...s, ...step } : s));
  }
  emit();
}

/** Replace the whole list — used by the replay on connect. */
export function setSteps(next: TrajectoryStep[]): void {
  steps = Array.isArray(next) ? next.slice(-MAX_ENTRIES) : [];
  emit();
}

export function clearSteps(): void {
  steps = [];
  emit();
}

export function subscribeSteps(listener: (steps: TrajectoryStep[]) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The steps grouped into turns, oldest turn first.
 *
 * Derived here rather than in the component so the page and the badge count
 * agree on what a turn contains. The backend groups too, but the live
 * subscription delivers individual steps, so this is the one place both the
 * replay and the live path are reduced to the same shape.
 */
export function getTurns(filter?: { conversation?: string | null }): TrajectoryTurn[] {
  const wanted = filter?.conversation || null;
  const turns: TrajectoryTurn[] = [];
  const index = new Map<string, TrajectoryTurn>();
  for (const step of steps) {
    if (wanted && step.conversation !== wanted) continue;
    const id = step.turn_id || 'unknown';
    let turn = index.get(id);
    if (!turn) {
      turn = { turn_id: id, steps: [], ts: step.ts };
      index.set(id, turn);
      turns.push(turn);
    }
    turn.steps.push(step);
  }
  for (const turn of turns) {
    turn.steps.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    turn.count = turn.steps.length;
    turn.duration_ms = turn.steps.reduce(
      (sum, s) => sum + (typeof s.duration_ms === 'number' ? s.duration_ms : 0),
      0,
    );
    turn.failed = turn.steps.filter((s) => s.success === false).length;
  }
  return turns;
}

/**
 * How many steps are still running.
 *
 * Drives the "working now" indicator. A step left in `running` is not an error
 * — the turn may still be going — but a stale one is worth surfacing, which is
 * why an unfinished step stays visible rather than being hidden until it ends.
 */
export function runningCount(): number {
  return steps.filter((s) => s.status === 'running').length;
}

/** How many steps failed. Drives the red badge. */
export function failedCount(): number {
  return steps.filter((s) => s.success === false).length;
}
