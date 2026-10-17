'use client';

// The console, full page.
//
// The drawer answers "what just happened"; this answers "what has happened" —
// a whole session's commands, long output read comfortably, and the same prompt
// the drawer offers. Both read the one store the layout subscribes to, so the
// two never disagree.

import CommandConsole from '@/components/CommandConsole';

export default function ConsolePage() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b border-[var(--border)] px-4 py-3">
        <h1 className="text-sm font-semibold text-[var(--text-primary)]">Console</h1>
        <p className="pt-0.5 text-[11px] text-[var(--text-secondary)]">
          Every command Addled ran — and the ones waiting for your approval.
          Type below to run one yourself in the same shell.
        </p>
      </header>
      <div className="min-h-0 flex-1 overflow-hidden bg-[var(--bg-primary)]">
        <CommandConsole variant="page" />
      </div>
    </div>
  );
}
