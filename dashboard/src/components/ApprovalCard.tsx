'use client';

import { useState } from 'react';

/**
 * One thing waiting on the user's decision, as the backend announces it.
 *
 * Sent on `action.approvalRequest` and read back from `approvals.list`, so the
 * same shape arrives whether the card was pushed live or restored after the
 * dashboard was opened late.
 */
export interface ApprovalRequest {
  approval_id: string;
  action_type: string;
  /** `skill` | `tool` | `action` — decides the wording and the icon. */
  kind: string;
  name: string;
  /** May "Always allow" be offered? The policy decides, not this component. */
  grantable?: boolean;
  /** May "Allow for session" be offered? True for anything that can ask. */
  session_grantable?: boolean;
  /** The command, when the thing being approved is a shell command. */
  command?: string;
}

const KIND_LABEL: Record<string, string> = {
  skill: 'skill',
  tool: 'tool',
  action: 'action',
};

const KIND_ICON: Record<string, string> = {
  skill: '🧩',
  tool: '🧰',
  action: '⚡',
};

function describe(request: ApprovalRequest): string {
  const what = KIND_LABEL[request.kind] || 'action';
  if (request.command) {
    return `The ${what} "${request.name}" wants to run a command:`;
  }
  return `The ${what} "${request.name}" needs your permission before it can run:`;
}

/**
 * A pending approval, rendered as up to four answers.
 *
 * **Allow once** answers this request only; the thing asks again next time.
 * **Always allow** writes a standing permission, so it does not ask again until
 * it is revoked from the skill or tool card.
 * **Allow for session** writes one that lasts until Addled restarts.
 *
 * Which of the two grants is offered is the backend's decision, not this
 * component's, and the distinction is not cosmetic. An action that is
 * dangerous whatever you pass it — `delete_file` — can be remembered
 * permanently, because the answer is about the action. One that is dangerous
 * because of its *arguments* — `run_command`, whose gate judges the command
 * text against a list of unrecoverable ones (`format`, `diskpart`, `rm -rf`) —
 * cannot: a grant is keyed by name, so remembering it would allow every
 * command the skill will ever carry, including the ones the gate exists to
 * stop. That one gets the session answer instead, and it expires at restart.
 *
 * A button does not clear the card by itself. The parent removes it once the
 * backend confirms, so a request that had already expired (its window
 * passed, or another surface answered it) stays visible with an explanation
 * instead of vanishing as though it had been granted.
 */
export default function ApprovalCard({
  request,
  onAllow,
  onAlwaysAllow,
  onAllowForSession,
  onDeny,
  busy,
  compact,
  error,
}: {
  request: ApprovalRequest;
  onAllow: () => void;
  onAlwaysAllow?: () => void;
  onAllowForSession?: () => void;
  onDeny: () => void;
  busy?: boolean;
  compact?: boolean;
  /** Shown when the answer did not take, so the card never lies. */
  error?: string;
}) {
  const [localBusy, setLocalBusy] = useState(false);
  const pending = Boolean(busy) || localBusy;
  const grantable = Boolean(request.grantable) && Boolean(onAlwaysAllow);
  // Offered whenever a permanent grant is not, so the card always has a real
  // answer: "ask me every single time" is not one the user will keep reading.
  const sessionGrantable =
    Boolean(request.session_grantable ?? true) && Boolean(onAllowForSession);

  const run = (fn: () => void) => () => {
    if (pending) return;
    setLocalBusy(true);
    fn();
  };

  return (
    <div className={`rounded-lg border ${error ? 'border-[#f85149]' : 'border-[#d29922]'} bg-[#161b22] ${compact ? 'p-3' : 'p-3.5'} my-2 max-w-[520px]`}>
      <p className="text-sm font-semibold text-[#e8eaed] flex items-center gap-1.5">
        <span>{KIND_ICON[request.kind] || '⚡'}</span>
        Permission needed
      </p>
      <p className="text-xs text-[#8b949e] mt-1 break-words">{describe(request)}</p>
      {request.command && (
        <pre className="mt-2 max-h-32 overflow-auto rounded bg-[#0d1117] border border-[#30363d] px-2 py-1.5 text-[11px] text-[#e8eaed] font-mono whitespace-pre-wrap break-all">
          {request.command}
        </pre>
      )}
      {error && <p className="text-xs text-[#f85149] mt-2">{error}</p>}
      <div className="flex flex-wrap gap-2 mt-3">
        <button
          onClick={run(onAllow)}
          disabled={pending}
          className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-md px-3 py-1.5 text-xs font-medium"
        >
          Allow once
        </button>
        {sessionGrantable && (
          <button
            onClick={run(onAllowForSession!)}
            disabled={pending}
            title="Do not ask again for this run of Addled; the prompt returns when it restarts"
            className="bg-[#21262d] hover:bg-[#30363d] disabled:opacity-50 border border-[#30363d] text-[#e8eaed] rounded-md px-3 py-1.5 text-xs font-medium"
          >
            Allow for session
          </button>
        )}
        {grantable && (
          <button
            onClick={run(onAlwaysAllow!)}
            disabled={pending}
            title="Do not ask again until you turn this off on the skill or tool card"
            className="bg-[#21262d] hover:bg-[#30363d] disabled:opacity-50 border border-[#30363d] text-[#e8eaed] rounded-md px-3 py-1.5 text-xs font-medium"
          >
            Always allow
          </button>
        )}
        <button
          onClick={run(onDeny)}
          disabled={pending}
          className="bg-[#3d1f1f] hover:bg-[#5a2a2a] disabled:opacity-50 text-[#f85149] rounded-md px-3 py-1.5 text-xs font-medium"
        >
          Deny
        </button>
      </div>
      {!grantable && sessionGrantable && (
        <p className="text-[10px] text-[#484f58] mt-2">
          This one judges each request on what it carries, so it can only be
          allowed for this session.
        </p>
      )}
    </div>
  );
}
