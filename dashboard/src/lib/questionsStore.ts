// Session-level store for clarification questions — survives page navigation.
//
// The same reasoning as `approvalsStore`: `onNotification` keeps ONE handler
// per method, so the chat page and the layout cannot both listen for
// `question.ask` — the last to mount wins and the other goes deaf. The layout
// owns the subscription and publishes here; the chat page reads from it.
//
// Questions are per-conversation in a way approvals are not. An approval is
// answered from wherever the user is; a question belongs to the chat that asked
// it, and a page showing another conversation's question would offer a card the
// user cannot act on. The store keeps the origin so a reader can filter, and
// `questionsFor` is the one place that decides what "belongs to this page"
// means.

export interface QuestionRequest {
  question_id: string;
  question: string;
  /** Concrete choices, shown as buttons. Empty for an open-ended question. */
  options?: string[];
  /** One line on why the model is asking, when it chose to say. */
  context?: string;
  /** Where it came from, so a page can show only its own. */
  source?: string;
  conversation?: string;
  /** Human phrase like "10 minutes", for the card's expiry note. */
  ttl?: string;
  created?: number;
}

let questions: QuestionRequest[] = [];
const listeners = new Set<(qs: QuestionRequest[]) => void>();
// Why an answer did not take, keyed by question id — kept beside the questions
// for the same reason as approvals: a question's shape stays exactly what the
// backend sent.
let errors: Record<string, string> = {};
const errorListeners = new Set<(errs: Record<string, string>) => void>();

function emit() {
  const snapshot = questions;
  listeners.forEach((l) => l(snapshot));
}

function emitErrors() {
  const snapshot = errors;
  errorListeners.forEach((l) => l(snapshot));
}

export function getQuestions(): QuestionRequest[] {
  return questions;
}

/** Questions waiting for one conversation, or all when no origin is given. */
export function questionsFor(
  source?: string,
  conversation?: string,
): QuestionRequest[] {
  if (!source && !conversation) return questions;
  return questions.filter((q) => {
    if (source && (q.source || '') !== source) return false;
    if (conversation && (q.conversation || '') !== conversation) return false;
    return true;
  });
}

export function addQuestion(question: QuestionRequest): void {
  if (!question?.question_id) return;
  if (questions.some((q) => q.question_id === question.question_id)) return;
  questions = [...questions, question];
  emit();
}

/**
 * Replace the whole list, after re-reading `questions.list`.
 *
 * Used on connect: a question raised before the dashboard opened is not
 * broadcast again, so the list is the only way to learn about it. Replacing
 * rather than merging means a question the backend has already expired
 * disappears instead of lingering as a card whose only answer is "too late".
 */
export function setQuestions(next: QuestionRequest[]): void {
  questions = Array.isArray(next) ? next : [];
  emit();
  // Prune errors for questions that are no longer open. A restore replaces the
  // whole list, so anything left in `errors` refers to a question the backend
  // has already closed and will never be displayed again.
  const live = new Set(questions.map((q) => q.question_id));
  const stale = Object.keys(errors).filter((id) => !live.has(id));
  if (stale.length) {
    const rest = { ...errors };
    stale.forEach((id) => delete rest[id]);
    errors = rest;
    emitErrors();
  }
}

/** Drop one, once the backend confirms it was answered. */
export function removeQuestion(questionId: string): void {
  const next = questions.filter((q) => q.question_id !== questionId);
  if (next.length !== questions.length) {
    questions = next;
    emit();
  }
  // The error goes with it. Ids are unique so a stale entry could not be shown
  // against a different question, but the map would grow for the life of the
  // session — one entry per failed answer — with nothing to reclaim it.
  if (errors[questionId]) {
    const rest = { ...errors };
    delete rest[questionId];
    errors = rest;
    emitErrors();
  }
}

export function getQuestionError(questionId: string): string {
  return errors[questionId] || '';
}

/** Record that an answer failed, so the card can say so instead of vanishing. */
export function setQuestionError(questionId: string, message: string): void {
  if (!questionId) return;
  if (message) {
    errors = { ...errors, [questionId]: message };
  } else {
    const next = { ...errors };
    delete next[questionId];
    errors = next;
  }
  emitErrors();
}

export function subscribeQuestions(
  listener: (qs: QuestionRequest[]) => void,
): () => void {
  listeners.add(listener);
  listener(questions);
  return () => listeners.delete(listener);
}

export function subscribeQuestionErrors(
  listener: (errs: Record<string, string>) => void,
): () => void {
  errorListeners.add(listener);
  listener(errors);
  return () => errorListeners.delete(listener);
}

/**
 * Send the user's answer, and resume the work it was blocking.
 *
 * Two calls, in this order, and the order matters. The answer settles the
 * question; the chat turn then carries it back to the model. Doing it the other
 * way round would send a turn whose answer the backend had not recorded yet,
 * and the model would be told it had asked something nobody had answered.
 *
 * The resume is a normal `chat.send` rather than a special "continue" RPC, so
 * it inherits everything a typed message gets — the same pipeline, the same
 * history, the same tools. The prompt is written to read as the user answering
 * rather than as a new instruction, so the model continues its own thread of
 * work instead of starting a fresh one.
 */
export function makeQuestionAnswers(
  send: (method: string, params?: any) => Promise<any>,
) {
  const settle = (
    questionId: string,
    call: Promise<any>,
    fallback: string,
  ) => {
    call
      .then((r: any) => {
        if (r?.success) removeQuestion(questionId);
        else setQuestionError(questionId, r?.error || fallback);
      })
      .catch((e: any) => setQuestionError(
        questionId, e?.message || 'The answer could not be sent.'));
  };

  return (q: QuestionRequest) => {
    const resume = (answer: string) => {
      const asked = q.question ? `"${q.question}"` : 'a question';
      send('chat.send', {
        message:
          `Answering your question ${asked}: ${answer}\n\n` +
          `This is my answer to the question you asked. Continue the task ` +
          `you were working on when you asked it, using this.`,
        source: q.source || 'dashboard',
        conversation: q.conversation || undefined,
      }).catch(() => { /* the settle error already told the user */ });
    };

    return {
      answer: (answer: string) => {
        const value = String(answer || '').trim();
        if (!value) return;
        // The origin is REQUIRED, not optional: the backend refuses an answer
        // that does not say which conversation it belongs to, because otherwise
        // an id alone settles any question — consent moving between chats.
        // `conversation` is sent as an explicit empty string when the question
        // has none (the dashboard's own chat raises them that way), which is a
        // claim about THIS conversation rather than a missing field.
        const origin = {
          source: q.source || 'dashboard',
          conversation: q.conversation || '',
        };
        settle(
          q.question_id,
          send('question.answer', {
            question_id: q.question_id, answer: value, ...origin,
          })
            .then((r: any) => { if (r?.success) resume(value); return r; }),
          'That question is no longer open — it expired, or another surface '
          + 'answered it.',
        );
      },
      dismiss: () => settle(
        q.question_id,
        send('question.dismiss', {
          question_id: q.question_id,
          source: q.source || 'dashboard',
          conversation: q.conversation || '',
        }),
        'That question is no longer open.'),
    };
  };
}
