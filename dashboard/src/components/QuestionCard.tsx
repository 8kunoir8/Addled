'use client';

import { useState } from 'react';

import type { QuestionRequest } from '@/lib/questionsStore';

export type { QuestionRequest };

/**
 * One question waiting on the user's answer.
 *
 * Sent on `question.ask` and read back from `questions.list`, so the same shape
 * arrives whether the card was pushed live or restored after the dashboard was
 * opened late.
 *
 * The card carries either buttons or a text box, decided by whether the model
 * offered `options`. That choice belongs to the backend, not to this component:
 * a question about which of two known files is faster to press than to type,
 * and a question about what a report should be called has no sensible buttons
 * at all. Rendering both shapes from one field keeps the model's decision the
 * only thing that varies.
 *
 * A button does not clear the card by itself. The parent removes it once the
 * backend confirms, so a question that had already expired stays visible with
 * an explanation instead of vanishing as though it had been answered.
 */
export default function QuestionCard({
  question,
  onAnswer,
  onDismiss,
  busy,
  compact,
  error,
}: {
  question: QuestionRequest;
  /** The user's answer: a chosen option, or their typed text. */
  onAnswer: (answer: string) => void;
  onDismiss?: () => void;
  busy?: boolean;
  compact?: boolean;
  /** Shown when the answer did not take, so the card never lies. */
  error?: string;
}) {
  const [localBusy, setLocalBusy] = useState(false);
  const [text, setText] = useState('');
  const pending = Boolean(busy) || localBusy;

  const options = (question.options || []).filter((o) => String(o).trim());
  const hasChoices = options.length > 0;

  const send = (value: string) => {
    if (pending) return;
    const answer = String(value || '').trim();
    if (!answer) return;
    setLocalBusy(true);
    onAnswer(answer);
  };

  return (
    <div
      className={`rounded-lg border ${error ? 'border-[#f85149]' : 'border-[#1f6feb]'} bg-[#161b22] ${compact ? 'p-3' : 'p-3.5'} my-2 max-w-[520px]`}
      role="group"
      aria-label="Addled is asking a question"
    >
      <p className="text-sm font-semibold text-[#e8eaed] flex items-center gap-1.5">
        <span>❓</span>
        Addled needs to know
      </p>

      <p className="text-sm text-[#e8eaed] mt-1.5 break-words">
        {question.question}
      </p>

      {question.context && (
        <p className="text-[11px] text-[#8b949e] mt-1 break-words">
          {question.context}
        </p>
      )}

      {error && <p className="text-xs text-[#f85149] mt-2">{error}</p>}

      {hasChoices ? (
        <div className="flex flex-wrap gap-2 mt-3">
          {options.map((option) => (
            <button
              key={option}
              onClick={() => send(option)}
              disabled={pending}
              className="bg-[#21262d] hover:bg-[#30363d] disabled:opacity-50 border border-[#30363d] text-[#e8eaed] rounded-md px-3 py-1.5 text-xs font-medium max-w-[240px] truncate"
              title={option}
            >
              {option}
            </button>
          ))}
          {onDismiss && (
            <button
              onClick={() => { if (!pending) { setLocalBusy(true); onDismiss(); } }}
              disabled={pending}
              className="bg-[#3d1f1f] hover:bg-[#5a2a2a] disabled:opacity-50 text-[#f85149] rounded-md px-3 py-1.5 text-xs font-medium"
            >
              Skip
            </button>
          )}
        </div>
      ) : (
        <form
          className="mt-3 flex gap-2"
          onSubmit={(e) => { e.preventDefault(); send(text); }}
        >
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={pending}
            placeholder="Type your answer…"
            aria-label="Your answer"
            className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded-md px-3 py-1.5 text-xs text-[#e8eaed] disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={pending || !text.trim()}
            className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-md px-3 py-1.5 text-xs font-medium"
          >
            Answer
          </button>
          {onDismiss && (
            <button
              type="button"
              onClick={() => { if (!pending) { setLocalBusy(true); onDismiss(); } }}
              disabled={pending}
              className="bg-[#21262d] hover:bg-[#30363d] disabled:opacity-50 border border-[#30363d] text-[#8b949e] rounded-md px-3 py-1.5 text-xs font-medium"
            >
              Skip
            </button>
          )}
        </form>
      )}

      <p className="text-[10px] text-[#484f58] mt-2">
        {question.ttl
          ? `Waiting for ${question.ttl}. Addled carries on when you answer.`
          : 'Addled carries on when you answer.'}
      </p>
    </div>
  );
}
