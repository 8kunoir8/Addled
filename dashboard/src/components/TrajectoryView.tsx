'use client';

// The trajectory — what the agent DID, step by step.
//
// The chat page shows what the model SAID. The console shows what Addled RAN,
// and can only ever see the terminal. Neither shows the middle: the search, the
// two file reads, the write, and what each one cost. Asked "why did that take so
// long?" or "did it actually check?", this is the only surface that answers.
//
// Read from the store the layout writes to, for the same reason the console is:
// `onNotification` keeps ONE handler per method, so a page-level subscriber
// would go deaf the moment another page mounted.

import { useState, useEffect } from 'react';
import { useWS } from '@/lib/useWS';
import {
  getTurns, subscribeSteps, setSteps,
  type TrajectoryStep, type TrajectoryTurn,
} from '@/lib/trajectoryStore';

function fmtTime(ts?: number): string {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleTimeString(
    [], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDuration(ms?: number | null): string {
  if (ms == null) return '';
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Colour and glyph per status.
 *
 * A Record keyed by the status strings, so an unknown status from a newer
 * backend degrades to a neutral row instead of crashing the panel.
 */
const STATUS_STYLE: Record<string, { dot: string; text: string }> = {
  running: { dot: 'bg-[var(--warning)] animate-pulse', text: 'text-[var(--warning)]' },
  ok: { dot: 'bg-[var(--success)]', text: 'text-[var(--success)]' },
  failed: { dot: 'bg-[var(--danger)]', text: 'text-[var(--danger)]' },
};

function statusStyle(status?: string) {
  return STATUS_STYLE[status || ''] || { dot: 'bg-[var(--text-secondary)]', text: 'text-[var(--text-secondary)]' };
}

/** A field is shown as the JSON the backend sent, trimmed to one readable line. */
function renderField(value: TrajectoryStep['result']): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function StepRow({ step }: { step: TrajectoryStep }) {
  const [open, setOpen] = useState(false);
  const style = statusStyle(step.status);
  const result = renderField(step.result);
  const args = renderField(step.arguments);
  const detail = step.error ? step.error : result;

  return (
    <li className="border-b border-[var(--bg-tertiary)] last:border-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-[var(--bg-secondary)]"
      >
        <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} />
        <span className="shrink-0 font-mono text-[12px] text-[var(--text-primary)]">{step.tool}</span>
        {step.truncated ? (
          <span
            className="shrink-0 rounded bg-[var(--border)] px-1 text-[10px] text-[var(--text-secondary)]"
            title="The backend capped this field; it is not the whole value."
          >
            trimmed
          </span>
        ) : null}
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-[#6e7681]">
          {args}
        </span>
        <span className={`shrink-0 font-mono text-[11px] ${style.text}`}>
          {fmtDuration(step.duration_ms)}
        </span>
      </button>
      {open ? (
        <div className="px-3 pb-2 pl-7">
          {args ? (
            <pre className="mb-1 overflow-x-auto whitespace-pre-wrap break-all rounded bg-[var(--bg-primary)] p-2 font-mono text-[11px] text-[var(--text-secondary)]">
              {args}
            </pre>
          ) : null}
          {detail ? (
            <pre
              className={`overflow-x-auto whitespace-pre-wrap break-all rounded bg-[var(--bg-primary)] p-2 font-mono text-[11px] ${
                step.error ? 'text-[var(--danger)]' : 'text-[var(--text-primary)]'
              }`}
            >
              {detail}
            </pre>
          ) : (
            <p className="text-[11px] italic text-[#6e7681]">No output recorded.</p>
          )}
        </div>
      ) : null}
    </li>
  );
}

function TurnBlock({ turn }: { turn: TrajectoryTurn }) {
  return (
    <section className="mb-3 overflow-hidden rounded border border-[var(--border)] bg-[var(--bg-secondary)]">
      <header className="flex items-center gap-2 border-b border-[var(--border)] px-3 py-1.5">
        <span className="font-mono text-[11px] text-[var(--text-secondary)]">{fmtTime(turn.ts)}</span>
        <span className="text-[11px] text-[var(--text-secondary)]">
          {turn.count} step{turn.count === 1 ? '' : 's'}
        </span>
        {turn.failed ? (
          <span className="rounded bg-[var(--danger)]/15 px-1.5 text-[10px] text-[var(--danger)]">
            {turn.failed} failed
          </span>
        ) : null}
        <span className="ml-auto font-mono text-[11px] text-[var(--text-secondary)]">
          {fmtDuration(turn.duration_ms)}
        </span>
      </header>
      <ul>
        {turn.steps.map((step) => (
          <StepRow key={step.id} step={step} />
        ))}
      </ul>
    </section>
  );
}

export default function TrajectoryView() {
  const { send } = useWS();
  const [turns, setTurns] = useState<TrajectoryTurn[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const sync = () => setTurns(getTurns());
    sync();
    return subscribeSteps(sync);
  }, []);

  // A broadcast reaches only a tab that is ALREADY open, so without this a
  // reload would show an empty trajectory beside a conversation full of work —
  // the same false silence `console.list` exists to prevent.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await send('trajectory.list', { limit: 500 });
        if (cancelled) return;
        if (r?.success === false) {
          setError(r?.error || 'Could not load the trajectory.');
          return;
        }
        if (Array.isArray(r?.entries)) setSteps(r.entries);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [send]);

  if (error) {
    return <p className="p-4 text-[12px] text-[var(--danger)]">{error}</p>;
  }

  if (!turns.length) {
    return (
      <p className="p-4 text-[12px] text-[#6e7681]">
        Nothing recorded yet. Run a turn and its steps will appear here.
      </p>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      {turns.map((turn) => (
        <TurnBlock key={turn.turn_id} turn={turn} />
      ))}
    </div>
  );
}
