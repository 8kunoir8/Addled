'use client';

// The console — what Addled ran, and a prompt to run your own.
//
// Why this exists: asked to check for whisper, Addled said "On it - checking
// now", ran nothing, and the next turn invented "two commands are still waiting
// on your approval". Nothing had run and nothing was pending. That was only
// unknowable because the commands were invisible.
//
// Three things it must do, and the third is the one that catches that lie:
//   1. show the commands that ran, and what they printed,
//   2. show the ones that are WAITING or were DENIED — a row that simply
//      disappears is indistinguishable from a console nobody looked at,
//   3. let you type, so a question can be settled without the model in the loop.

import { useState, useEffect, useCallback, useRef } from 'react';
import { useWS } from '@/lib/useWS';
import {
  getCommands, subscribeCommands, setCommands,
  clearCommands, awaitingCount,
  type CommandEntry,
} from '@/lib/consoleStore';

/**
 * Colour and glyph per status.
 *
 * A Record keyed by the status strings, so an unknown status from a newer
 * backend degrades to a neutral row instead of crashing the panel.
 */
const STATUS_STYLE: Record<string, { dot: string; label: string; text: string }> = {
  ok: { dot: 'text-[#3fb950]', label: '✓', text: 'text-[#3fb950]' },
  failed: { dot: 'text-[#f85149]', label: '✗', text: 'text-[#f85149]' },
  timeout: { dot: 'text-[#d29922]', label: '⏱', text: 'text-[#d29922]' },
  denied: { dot: 'text-[#8b949e]', label: '⊘', text: 'text-[#8b949e]' },
  awaiting: { dot: 'text-[#d29922]', label: '⏳', text: 'text-[#d29922]' },
  running: { dot: 'text-[#58a6ff]', label: '…', text: 'text-[#58a6ff]' },
};

const NEUTRAL = { dot: 'text-[#8b949e]', label: '·', text: 'text-[#8b949e]' };

function fmtTime(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDuration(ms?: number | null): string {
  if (ms == null) return '';
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/**
 * The line a row shows when collapsed.
 *
 * `argv` is shown as an argv list, not a shell string, because that is what
 * actually ran — rendering it back into a command line would invent quoting
 * that was never there.
 */
function commandLine(entry: CommandEntry): string {
  if (entry.kind === 'argv' && entry.argv?.length) return entry.argv.join(' ');
  return entry.command || '';
}

export default function CommandConsole({
  variant = 'drawer',
}: {
  variant?: 'drawer' | 'page';
}) {
  const { send } = useWS();
  const [entries, setEntries] = useState<CommandEntry[]>(getCommands);
  const [open, setOpen] = useState(variant === 'page');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const scroller = useRef<HTMLDivElement | null>(null);
  const pinned = useRef(true);

  useEffect(() => subscribeCommands(setEntries), []);

  // Replay on connect. The broadcast only reaches a dashboard that is already
  // open, so without this a reload shows an empty console beside a conversation
  // full of work — which reads as "Addled has done nothing".
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await send('console.list', { limit: 300 });
        if (!cancelled && Array.isArray(r?.entries)) setCommands(r.entries);
      } catch {
        /* older backend, or nothing recorded yet */
      }
    })();
    return () => { cancelled = true; };
  }, [send]);

  // Follow the tail, but only while the user is already at the bottom —
  // otherwise reading back through output would be yanked away by each new
  // command, which is the behaviour that makes a console unusable.
  useEffect(() => {
    if (!open) return;
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [entries, open]);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }, []);

  const runTyped = useCallback(async () => {
    const text = typed.trim();
    if (!text || busy) return;
    setBusy(true);
    setNote('');
    setTyped('');
    // Shown immediately, so the prompt feels responsive rather than waiting on
    // a round trip; the backend's own record replaces it by id when it lands.
    try {
      const r = await send('console.run', { command: text, session: 'console' });
      if (!r?.success && r?.error) setNote(String(r.error));
      else if (r?.running) setNote('Still running — output will follow.');
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not run that.');
    } finally {
      setBusy(false);
    }
  }, [typed, busy, send]);

  const doClear = useCallback(async () => {
    try {
      await send('console.clear', {});
    } catch { /* the local clear is still correct */ }
    clearCommands();
  }, [send]);

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  }, []);

  const waiting = awaitingCount();
  const last = entries.length ? entries[entries.length - 1] : null;
  const isPage = variant === 'page';

  // A collapsed drawer is a one-line status strip, not a hidden panel — the
  // point is that you can see what just happened without opening anything.
  if (!isPage && !open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-3 right-3 z-40 flex items-center gap-2 rounded-lg border border-[#30363d] bg-[#161b22] px-3 py-1.5 text-xs shadow-lg hover:border-[#58a6ff]"
        title="Show the command console"
      >
        <span className="text-[#8b949e]">▸ console</span>
        {waiting > 0 && (
          <span className="rounded bg-[#d29922] px-1.5 text-[10px] font-semibold text-black">
            {waiting} waiting
          </span>
        )}
        {last && !waiting && (
          <span className={`${(STATUS_STYLE[last.status] || NEUTRAL).text} max-w-[220px] truncate font-mono`}>
            {(STATUS_STYLE[last.status] || NEUTRAL).label} {commandLine(last)}
          </span>
        )}
        {!last && <span className="text-[#484f58]">nothing yet</span>}
      </button>
    );
  }

  return (
    <div
      className={isPage
        ? 'flex h-full flex-col'
        : 'fixed bottom-3 right-3 z-40 flex h-[420px] w-[640px] max-w-[92vw] flex-col rounded-lg border border-[#30363d] bg-[#0d1117] shadow-2xl'}
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-[#30363d] px-3 py-2">
        <span className="text-xs font-semibold text-[#e8eaed]">Console</span>
        <span className="text-[10px] text-[#8b949e]">
          {entries.length} command{entries.length === 1 ? '' : 's'}
        </span>
        {waiting > 0 && (
          <span className="rounded bg-[#d29922] px-1.5 text-[10px] font-semibold text-black">
            {waiting} waiting on you
          </span>
        )}
        <div className="flex-1" />
        <button
          onClick={doClear}
          className="rounded px-2 py-0.5 text-[11px] text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]"
        >
          Clear
        </button>
        {!isPage && (
          <button
            onClick={() => setOpen(false)}
            className="rounded px-2 py-0.5 text-[11px] text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]"
            title="Collapse"
          >
            ▾
          </button>
        )}
      </div>

      <div
        ref={scroller}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed"
      >
        {entries.length === 0 && (
          <p className="py-6 text-center text-[#484f58]">
            Nothing has run yet. Commands Addled runs appear here — including the
            ones waiting for your approval.
          </p>
        )}
        {entries.map((e) => {
          const st = STATUS_STYLE[e.status] || NEUTRAL;
          const isOpen = isPage || expanded[e.id];
          const masked = e.redacted || [];
          return (
            <div key={e.id} className="border-b border-[#161b22] py-1.5 last:border-0">
              <button
                onClick={() => !isPage && toggle(e.id)}
                className="flex w-full items-start gap-2 text-left hover:bg-[#161b22]"
              >
                <span className={`${st.dot} shrink-0`} title={e.status}>{st.label}</span>
                <span className="shrink-0 text-[#484f58]">{fmtTime(e.ts)}</span>
                <span className="shrink-0 text-[#58a6ff]">{e.tool}</span>
                <span className="min-w-0 flex-1 break-all text-[#c9d1d9]">
                  {commandLine(e)}
                </span>
                {masked.length > 0 && (
                  <span
                    className="shrink-0 rounded bg-[#21262d] px-1 text-[9px] text-[#d29922]"
                    title={`Redacted: ${masked.join(', ')} — a credential was masked at capture`}
                  >
                    masked
                  </span>
                )}
                <span className="shrink-0 text-[#484f58]">{fmtDuration(e.duration_ms)}</span>
                {e.exit_code != null && e.exit_code !== 0 && (
                  <span className="shrink-0 text-[#f85149]">exit {e.exit_code}</span>
                )}
              </button>

              {e.status === 'awaiting' && (
                <p className="pl-5 pt-1 text-[10px] text-[#d29922]">
                  Waiting for your approval — answer it on the approval card.
                </p>
              )}
              {e.status === 'denied' && (
                <p className="pl-5 pt-1 text-[10px] text-[#8b949e]">
                  You denied this command. It did not run.
                </p>
              )}

              {isOpen && (e.stdout || e.stderr) && (
                <div className="pl-5 pt-1">
                  {e.stdout && (
                    <pre className="whitespace-pre-wrap break-all text-[#8b949e]">
                      {e.stdout}
                    </pre>
                  )}
                  {e.stderr && (
                    <pre className="whitespace-pre-wrap break-all text-[#f85149]">
                      {e.stderr}
                    </pre>
                  )}
                  {e.truncated && (
                    <p className="pt-0.5 text-[10px] text-[#484f58]">
                      (output trimmed for display)
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* The prompt. This is what makes it a terminal rather than a log. */}
      <div className="shrink-0 border-t border-[#30363d] px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="shrink-0 text-[11px] text-[#3fb950]">$</span>
          <input
            value={typed}
            onChange={(ev) => setTyped(ev.target.value)}
            onKeyDown={(ev) => {
              if (ev.key === 'Enter') { ev.preventDefault(); runTyped(); }
            }}
            placeholder={busy ? 'running…' : 'type a PowerShell command and press Enter'}
            disabled={busy}
            spellCheck={false}
            className="min-w-0 flex-1 rounded border border-[#30363d] bg-[#161b22] px-2 py-1 font-mono text-[11px] text-[#e8eaed] outline-none focus:border-[#58a6ff] disabled:opacity-50"
          />
          <button
            onClick={runTyped}
            disabled={busy || !typed.trim()}
            className="shrink-0 rounded bg-[#3380FF] px-2.5 py-1 text-[11px] font-medium text-white hover:bg-[#4d94ff] disabled:opacity-40"
          >
            Run
          </button>
        </div>
        {note && <p className="pt-1 text-[10px] text-[#d29922]">{note}</p>}
        <p className="pt-1 text-[10px] text-[#484f58]">
          Runs in the same shell Addled uses. A destructive command still waits
          for your approval.
        </p>
      </div>
    </div>
  );
}
