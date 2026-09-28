// Session-level chat message store — survives page navigation.
// (In-memory only; persists for the lifetime of the dashboard session.)

export type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  streaming?: boolean;
  attachments?: { name: string; kind: string; preview?: string }[];
  /** Where a message from another surface came from (a bot, a task, the
   *  character). Absent on anything typed in this app. */
  source?: string;
  sourceLabel?: string;
  sourceIcon?: string;
};

let messages: ChatMessage[] = [];
const listeners = new Set<(msgs: ChatMessage[]) => void>();

export function getChatMessages(): ChatMessage[] {
  return messages;
}

export function setChatMessages(next: ChatMessage[]): void {
  messages = next;
  listeners.forEach((l) => l(next));
}

/** Subscribe to store updates (e.g. a reply that arrived after remount). */
export function subscribeChatMessages(listener: (msgs: ChatMessage[]) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
