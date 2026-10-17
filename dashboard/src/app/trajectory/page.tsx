'use client';

// The trajectory, full page.
//
// Beside the console, not on the chat page. The chat page is the busiest
// surface in the dashboard and a turn's worth of steps would compete with the
// conversation for the reader's attention; the console already established the
// pattern of "what Addled did" living on its own page, and this is the second
// half of that answer — the console shows what it RAN, this shows what it DID.

import TrajectoryView from '@/components/TrajectoryView';

export default function TrajectoryPage() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b border-[var(--border)] px-4 py-3">
        <h1 className="text-sm font-semibold text-[var(--text-primary)]">Trajectory</h1>
        <p className="pt-0.5 text-[11px] text-[var(--text-secondary)]">
          Every tool call the agent made, grouped by turn — what it ran, what
          came back, and how long each one took. Click a step to see its
          arguments and its result.
        </p>
      </header>
      <div className="min-h-0 flex-1 overflow-hidden bg-[var(--bg-primary)]">
        <TrajectoryView />
      </div>
    </div>
  );
}
