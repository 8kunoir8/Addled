// Session-level store for permission requests — survives page navigation.
//
// Why this exists rather than each page subscribing directly: `onNotification`
// keeps ONE handler per method (`notificationHandlers.set(method, handler)`),
// so the chat page and the layout cannot both listen for
// `action.approvalRequest` — the last one to mount silently wins and the other
// goes deaf. The layout is always mounted, so it owns the subscription and
// publishes answers here; the chat page and the floating prompt both read from
// this store.
//
// (In-memory only; the backend is the durable half — `approvals.list` restores
// anything raised before the dashboard was open.)

import type { ApprovalRequest } from '@/components/ApprovalCard';

export type { ApprovalRequest };

let requests: ApprovalRequest[] = [];
const listeners = new Set<(reqs: ApprovalRequest[]) => void>();
// Why an answer did not take, keyed by approval id. Kept beside the requests
// rather than inside them so a request's shape stays exactly what the backend
// sent, and so clearing one does not disturb the other.
let errors: Record<string, string> = {};
const errorListeners = new Set<(errs: Record<string, string>) => void>();

function emit() {
  const snapshot = requests;
  listeners.forEach((l) => l(snapshot));
}

function emitErrors() {
  const snapshot = errors;
  errorListeners.forEach((l) => l(snapshot));
}

export function getApprovals(): ApprovalRequest[] {
  return requests;
}

/** Why an answer to this request did not take, if it did not. */
export function getApprovalError(approvalId: string): string {
  return errors[approvalId] || '';
}

/** Record that an answer failed, so the card can say so instead of vanishing. */
export function setApprovalError(approvalId: string, message: string): void {
  if (!approvalId) return;
  if (message) {
    errors = { ...errors, [approvalId]: message };
  } else {
    const next = { ...errors };
    delete next[approvalId];
    errors = next;
  }
  emitErrors();
}

export function subscribeApprovalErrors(
  listener: (errs: Record<string, string>) => void,
) {
  errorListeners.add(listener);
  return () => {
    errorListeners.delete(listener);
  };
}

/** Add a request, ignoring one we already hold (the same id can arrive by
 *  broadcast and again from the restore call). */
export function addApproval(req: ApprovalRequest): void {
  if (!req?.approval_id) return;
  if (requests.some((r) => r.approval_id === req.approval_id)) return;
  requests = [...requests, req];
  emit();
}

export function addApprovals(next: ApprovalRequest[]): void {
  const seen = new Set(requests.map((r) => r.approval_id));
  const added = next.filter((r) => r?.approval_id && !seen.has(r.approval_id));
  if (!added.length) return;
  requests = [...requests, ...added];
  emit();
}

export function dropApproval(approvalId: string): void {
  const next = requests.filter((r) => r.approval_id !== approvalId);
  if (next.length === requests.length) {
    setApprovalError(approvalId, '');
    return;
  }
  requests = next;
  setApprovalError(approvalId, '');
  emit();
}

/** Clear everything — used when the connection is re-established with a
 *  backend that has no pending requests, so a stale card cannot linger. */
export function setApprovals(next: ApprovalRequest[]): void {
  requests = next;
  emit();
}

export function subscribeApprovals(listener: (reqs: ApprovalRequest[]) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The three answers, in one place.
 *
 * Both surfaces that show a card — the message stream and the floating prompt
 * on other pages — call this, so "Allow once" cannot mean one thing in chat and
 * another in Settings. The card is dropped only when the backend confirms;
 * otherwise the reason is recorded and shown on the card, because a request
 * that disappears without running reads as though it had been granted.
 *
 * `send` is passed in rather than imported so this module stays free of the
 * socket and can be exercised on its own.
 */
export function makeAnswers(
  send: (method: string, params?: any) => Promise<any>,
) {
  const settle = (
    approvalId: string,
    call: Promise<any>,
    fallback: string,
  ) => {
    call
      .then((r: any) => {
        if (r?.success) dropApproval(approvalId);
        else setApprovalError(approvalId, r?.error || fallback);
      })
      .catch((e: any) => setApprovalError(
        approvalId, e?.message || 'The answer could not be sent.'));
  };

  return (req: ApprovalRequest) => ({
    allow: () => settle(
      req.approval_id,
      send('action.approve', { approvalId: req.approval_id }),
      'That request is no longer waiting — ask again.'),
    always: () => settle(
      req.approval_id,
      send('approvals.alwaysAllow', {
        kind: req.kind, name: req.name, approvalId: req.approval_id,
      }),
      'That permission could not be saved.'),
    deny: () => settle(
      req.approval_id,
      send('action.deny', { approvalId: req.approval_id }),
      'That request is no longer waiting.'),
  });
}
