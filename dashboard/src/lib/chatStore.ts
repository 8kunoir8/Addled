// Session-level chat message store — survives page navigation.
// (In-memory only; persists for the lifetime of the dashboard session.)

export type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  streaming?: boolean;
};

let messages: ChatMessage[] = [];

export function getChatMessages(): ChatMessage[] {
  return messages;
}

export function setChatMessages(next: ChatMessage[]): void {
  messages = next;
}
