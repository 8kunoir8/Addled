'use client';

import { useState, useRef, useEffect } from 'react';
import { useWS } from '@/lib/useWS';
import { getChatMessages, setChatMessages, subscribeChatMessages } from '@/lib/chatStore';
import {
  subscribeApprovals, subscribeApprovalErrors, makeAnswers,
  type ApprovalRequest,
} from '@/lib/approvalsStore';
import ApprovalCard from '@/components/ApprovalCard';
import QuestionCard from '@/components/QuestionCard';
import {
  subscribeQuestions, subscribeQuestionErrors, makeQuestionAnswers, questionsFor,
  type QuestionRequest,
} from '@/lib/questionsStore';
import {
  filesFromPaste, filesToAttachments, releaseAttachment,
  MAX_ATTACHMENTS, type Attachment,
} from '@/lib/attachments';

interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  streaming?: boolean;
  attachments?: { name: string; kind: string; preview?: string }[];
  /** Where the turn came from, for a message this page did not send itself.
   *  Absent on everything the user typed here, which needs no badge. */
  source?: string;
  sourceLabel?: string;
  sourceIcon?: string;
}

const GREETING: Message = {
  role: 'assistant',
  content: 'Hello! I\'m your AI desktop companion. How can I help you today?',
  // Stamped on mount, not here. This module is evaluated when the page is
  // prerendered at build time, so a Date.now() here is the *build* time: the
  // client then renders a different clock and React reports a hydration
  // mismatch (error #418), and the greeting shows a stale build timestamp.
  // 0 means "no time yet" and is not rendered.
  timestamp: 0,
};

// Fill an in-flight "thinking" placeholder with the reply (or append if none).
function fillReply(messages: Message[], reply: string): Message[] {
  const idx = messages.findIndex(
    (m) => m.role === 'assistant' && m.streaming && !m.content
  );
  if (idx >= 0) {
    return messages.map((m, i) =>
      i === idx ? { ...m, content: reply, streaming: false } : m
    );
  }
  const last = messages[messages.length - 1];
  if (last && last.role === 'assistant' && last.content === reply) {
    return messages;
  }
  return [...messages, { role: 'assistant', content: reply, timestamp: Date.now() }];
}

export default function ChatPage() {
  const { state: wsState, send, onNotification } = useWS();
  // Restore the session's messages on mount so navigation doesn't wipe them
  const [messages, setMessages] = useState<Message[]>(() => {
    const cached = getChatMessages();
    return cached.length ? cached : [GREETING];
  });
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  // The current slow step ("Looking at photo.png…"), or '' when idle.
  const [activity, setActivity] = useState('');
  // Questions waiting on this page. The list is kept so the component re-renders
  // when one arrives; what is RENDERED is filtered by `questionsFor` below, so
  // a question raised in another conversation never reaches this page's cards.
  const [, setQuestions] = useState<QuestionRequest[]>([]);
  const [questionErrors, setQuestionErrors] =
    useState<Record<string, string>>({});
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [processingFiles, setProcessingFiles] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [anchors, setAnchors] = useState<{type: string; text: string}[]>([]);
  const [workScope, setWorkScope] = useState('');
  // What the turn can use and what it used, sent by the backend before and
  // after answering. Not cleared on a timer like the anchors: this describes
  // capability, so it stays until the next turn replaces it.
  const [usage, setUsage] = useState<any>(null);
  // Requests waiting on the user's permission, newest last.
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  // Why an answer did not take, so a card can say so rather than vanish.
  const [approvalErrors, setApprovalErrors] =
    useState<Record<string, string>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const dragDepth = useRef(0);

  useEffect(() => {
    setChatMessages(messages);
  }, [messages]);

  // Give the greeting its clock once we are on the client. Doing it here rather
  // than at module scope keeps the prerendered HTML and the first client render
  // identical — see GREETING.
  useEffect(() => {
    setMessages((prev) => prev.map((m) =>
      m.timestamp > 0 ? m : { ...m, timestamp: Date.now() }));
  }, []);

  // The backend owns "the current conversation", so adopt its view once we are
  // connected. Without this the two disagreed: this page restored its own cached
  // messages (or just the greeting) while the pipeline was handing the model a
  // different thread — which looked like the agent answering a question nobody
  // had asked.
  useEffect(() => {
    if (wsState !== 'connected') return;
    const cached = getChatMessages();
    const last = cached[cached.length - 1];
    if (last && last.role === 'assistant' && last.streaming) {
      return;  // a request is in flight — the recovery effect below owns this
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await send('chat.history', { max: 60 });
        if (cancelled) return;
        const msgs: Message[] = (r?.messages || [])
          .filter((m: any) => m && m.content)
          .map((m: any) => ({
            role: m.role === 'user' ? 'user' : 'assistant',
            content: String(m.content ?? ''),
            timestamp: Number(m.timestamp || 0) * 1000,
          }));
        setMessages(msgs.length ? msgs : [GREETING]);
      } catch {
        /* keep what we already have */
      }
    })();
    return () => { cancelled = true; };
  }, [wsState, send]);

  // Apply replies that landed in the store after this component (re)mounted —
  // e.g. the user switched to another tab mid-request and came back.
  useEffect(() => subscribeChatMessages((msgs) => {
    setMessages((prev) => (prev === msgs ? prev : msgs));
  }), []);

  // If a previous session left a stuck "thinking" placeholder behind
  // (connection dropped mid-reply), recover the real answer from the backend.
  useEffect(() => {
    if (wsState !== 'connected') return;
    const cached = getChatMessages();
    const stuck = cached[cached.length - 1];
    if (!stuck || stuck.role !== 'assistant' || !stuck.streaming || stuck.content) {
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await send('chat.history', { max: 60 });
        const backendMsgs: any[] = r?.messages || [];
        const lastB = backendMsgs[backendMsgs.length - 1];
        if (cancelled) return;
        const placeholderAt = stuck.timestamp;
        if (lastB && lastB.role === 'assistant' && lastB.content &&
            (lastB.timestamp || 0) * 1000 >= placeholderAt - 5000) {
          setMessages((prev) => fillReply(prev, lastB.content));
        } else if (lastB && lastB.role === 'user' &&
                   (lastB.timestamp || 0) * 1000 >= placeholderAt - 5000) {
          // Backend is still processing this prompt — keep the placeholder.
        } else {
          // No matching turn in history — the placeholder is orphaned.
          setMessages((prev) => prev.filter(
            (m) => !(m.role === 'assistant' && m.streaming && !m.content)
          ));
        }
      } catch {
        /* keep the placeholder */
      }
    })();
    return () => { cancelled = true; };
  }, [wsState, send]);

  // A message from another surface: the floating character, the voice
  // listener, a bot bridge (Telegram / Discord / WhatsApp) or a scheduled
  // task. It carries `source` so the bubble can say where it came from — a
  // message typed on a phone is otherwise indistinguishable from one typed
  // here, and the user needs to know which conversation they are reading.
  //
  // A turn sent from this page is never pushed back: it is declared
  // `source: 'dashboard'`, which the backend does not announce, because this
  // page has already drawn both bubbles itself.
  useEffect(() => onNotification('chat.push', (params: any) => {
    if (!params?.role || !params?.content) return;
    setMessages(prev => [...prev, {
      role: params.role,
      content: params.content,
      timestamp: params.timestamp ? params.timestamp * 1000 : Date.now(),
      source: params.source,
      sourceLabel: params.source_label,
      sourceIcon: params.source_icon,
    }]);
  }), [onNotification]);

  useEffect(() => {
    if (wsState !== 'connected') return;
    send('workspace.status', {})
      .then((r: any) => setWorkScope(r?.active_workspace || r?.root || ''))
      .catch(() => {});
  }, [wsState, send]);

  // Memory anchors: which memories the backend injected into this turn
  useEffect(() => onNotification('memory.anchors', (params: any) => {
    if (params?.anchors) setAnchors(params.anchors);
    const t = setTimeout(() => setAnchors([]), 12000);
    return () => clearTimeout(t);
  }), [onNotification]);

  // Tools, skills and MCP servers this chat has, and the ones it just used
  useEffect(() => onNotification('chat.tools', (params: any) => {
    if (params) setUsage(params);
  }), [onNotification]);

  // What a slow attachment step is doing right now.
  //
  // Local vision costs ~8s of CPU per image plus a ~16s one-time model load,
  // and transcription is seconds more. Without this the composer simply froze
  // for half a minute, which reads as a hang - and a turn ending with the
  // model apologising about "vision being unavailable" could not be told apart
  // from vision actually being unavailable. Cleared when the reply lands.
  useEffect(() => onNotification('chat.activity', (params: any) => {
    if (params?.what) setActivity(String(params.what));
  }), [onNotification]);

  useEffect(() => subscribeQuestions(setQuestions), []);
  useEffect(() => subscribeQuestionErrors(setQuestionErrors), []);

  // Answering from this page has to reach the backend the same way answering
  // from anywhere else does, so the store owns what is sent and this only
  // passes the call through.
  const answerQuestion = makeQuestionAnswers(send);

  // Permission requests are owned by the layout (it is always mounted, and
  // `onNotification` only keeps one handler per method) and published through
  // a store, so this page reads them rather than subscribing itself.
  useEffect(() => subscribeApprovals((reqs) => {
    setApprovals((prev) => (prev === reqs ? prev : reqs));
  }), []);

  useEffect(() => subscribeApprovalErrors((errs) => {
    setApprovalErrors((prev) => (prev === errs ? prev : errs));
  }), []);

  const answer = makeAnswers(send);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  /**
   * Turn picked, dropped or pasted files into attachments.
   *
   * Delegates to `lib/attachments.ts`, which is where the cap, the image
   * downscale and the text/binary decision live. This page used to carry its
   * own copy of all three; keeping one implementation is what stops the Chat
   * and Code composers accepting subtly different things.
   */
  const handleFiles = async (files: FileList | File[] | null) => {
    if (!files || !files.length) return;
    setProcessingFiles(true);
    try {
      const next = await filesToAttachments(files, attachments.length);
      if (next.length) {
        setAttachments(prev => [...prev, ...next].slice(0, MAX_ATTACHMENTS));
      }
    } catch {
      /* unreadable file — ignore */
    }
    setProcessingFiles(false);
  };

  const removeAttachment = (idx: number) => {
    setAttachments(prev => {
      // The shared helper revokes the thumbnail URL. Letting it go un-revoked
      // leaks one object URL per removed screenshot for the life of the page.
      releaseAttachment(prev[idx]);
      return prev.filter((_, i) => i !== idx);
    });
  };

  // ---- drag & drop ----------------------------------------------------------

  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    if (!Array.from(e.dataTransfer.types).includes('Files')) return;
    dragDepth.current += 1;
    setDragActive(true);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragActive(false);
    if (isLoading || wsState !== 'connected') return;
    handleFiles(e.dataTransfer.files);
  };

  // ---- paste ----------------------------------------------------------------

  /**
   * A pasted image or file becomes an attachment; plain text is left to the
   * textarea, which is what anyone pasting a sentence expects.
   *
   * The two arrive differently from the clipboard — a copied image sits in
   * `items`, while a screenshot taken with the snipping tool can come through
   * as a `files` entry — so `filesFromPaste` checks both rather than assuming.
   */
  const handlePaste = (e: React.ClipboardEvent) => {
    const { files } = filesFromPaste(e);
    if (!files.length) return;   // ordinary text paste — let the textarea have it
    e.preventDefault();
    if (isLoading || wsState !== 'connected') return;
    handleFiles(files);
  };

  const handleSend = async () => {
    if (isLoading || wsState !== 'connected') return;
    if (!input.trim() && !attachments.length) return;

    const text = input.trim() || 'Please analyze my attachment.';
    const sentAtts = attachments.map(a => ({ name: a.name, kind: a.kind, data: a.data }));
    const userMsg: Message = {
      role: 'user',
      content: text,
      timestamp: Date.now(),
      attachments: attachments.map(a => ({ name: a.name, kind: a.kind, preview: a.preview })),
    };
    setMessages(prev => [...prev, userMsg]);
    setInput('');
    setAttachments([]);
    setIsLoading(true);

    // Add streaming placeholder
    const assistantMsg: Message = { role: 'assistant', content: '', timestamp: Date.now(), streaming: true };
    setMessages(prev => [...prev, assistantMsg]);

    const applyReply = (reply: string) => {
      // Write through the store so the reply survives tab switches —
      // even if this component unmounted while the request was in flight.
      const next = fillReply(getChatMessages(), reply);
      setChatMessages(next);
      setMessages(next);
    };

    try {
      // `source` marks the turn as this page's own, so the backend does not
      // announce it back to us — we have already drawn both bubbles, and the
      // push would show the whole exchange a second time.
      const result = await send('chat.send', {
        message: text, attachments: sentAtts, source: 'dashboard',
      });
      applyReply(result?.response || 'No response');
    } catch (err: any) {
      applyReply(`Error: ${err.message}`);
    } finally {
      setIsLoading(false);
      // The last activity notice arrives before the reply, so clearing it here
      // (not in the handler) keeps it from outliving the turn it described.
      setActivity('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="relative flex flex-col h-full"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}>
      {/* Drop overlay */}
      {dragActive && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-[#0d1117]/85 border-2 border-dashed border-[#3380FF] rounded-lg pointer-events-none">
          <div className="text-center">
            <div className="text-3xl mb-2">⬇</div>
            <p className="text-sm font-medium text-[#e8eaed]">Drop to attach</p>
            <p className="text-xs text-[#8b949e] mt-1">Images, text, documents — or paste from the clipboard</p>
          </div>
        </div>
      )}
      {/* Header */}
      <header className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[#30363d]">
        <div className="min-w-0">
          <h1 className="text-sm font-semibold">Chat</h1>
          {workScope && (
            <p className="text-[11px] text-[#8b949e] truncate" title={workScope}>
              Work scope: {workScope}
            </p>
          )}
        </div>
        <span className="text-xs text-[#8b949e] shrink-0">
          {wsState === 'connected' ? 'Connected' : wsState === 'connecting' ? 'Connecting...' : 'Disconnected'}
        </span>
      </header>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[75%] rounded-2xl px-4 py-3 text-sm ${
              msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-assistant'
            }`}>
              {/* Where this came from, when it was not typed here. A message
                  sent from Telegram looks otherwise identical to one typed in
                  the app, and the user needs to know which thread it belongs
                  to before replying. */}
              {msg.sourceLabel && (
                <div className={`mb-1.5 -mt-0.5 text-[10px] flex items-center gap-1 ${
                  msg.role === 'user' ? 'text-blue-200' : 'text-[#8b949e]'
                }`}>
                  <span aria-hidden="true">{msg.sourceIcon}</span>
                  <span className="opacity-90">via {msg.sourceLabel}</span>
                </div>
              )}
              {msg.attachments && msg.attachments.length > 0 && (
                <div className="mb-2 space-y-1.5">
                  {msg.attachments.map((a, ai) => a.kind === 'image' && a.preview ? (
                    <img key={ai} src={a.preview} alt={a.name}
                      className="max-h-32 rounded-lg border border-black/20" />
                  ) : (
                    <div key={ai} className="flex items-center gap-1.5 text-xs opacity-90">
                      <span>{a.kind === 'text' ? '📄' : '📎'}</span>
                      <span className="truncate max-w-[200px]">{a.name}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="whitespace-pre-wrap break-words">
                {msg.content}
                {msg.streaming && <span className="typing-cursor" />}
              </div>
              <div className={`text-xs mt-1 ${msg.role === 'user' ? 'text-blue-200' : 'text-[#8b949e]'}`}>
                {msg.timestamp > 0 &&
                  new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              </div>
            </div>
          </div>
        ))}

        {/* Permission requests sit after the turn that raised them. They are
            not messages: the backend owns them and they disappear when
            answered, whether that answer came from here or from the card on
            another page. */}
        {approvals.map((req) => (
          <div key={req.approval_id} className="flex justify-start">
            <ApprovalCard
              request={req}
              error={approvalErrors[req.approval_id]}
              onAllow={answer(req).allow}
              onAlwaysAllow={answer(req).always}
              onAllowForSession={answer(req).session}
              onDeny={answer(req).deny}
            />
          </div>
        ))}
        {/* Questions the model asked. Like approvals these are not messages —
            the backend owns them, and they disappear once answered, whether the
            answer came from here or from the card on another page.
            Filtered to this page's own conversation: a question raised in a
            Telegram thread would render here with buttons whose resume targets
            that thread, offering the user a card they cannot really act on.
            `questionsFor` is the one place that decides what "belongs here"
            means, so this cannot drift from the store's own rule. */}
        {questionsFor('dashboard').map((q) => (
          <div key={q.question_id} className="flex justify-start">
            <QuestionCard
              question={q}
              error={questionErrors[q.question_id]}
              onAnswer={answerQuestion(q).answer}
              onDismiss={answerQuestion(q).dismiss}
            />
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t border-[#30363d] p-4">
        {usage && (
          <div className="flex flex-wrap items-center gap-1.5 mb-2">
            <span title="Skills the assistant can call right now"
              className="text-[10px] px-2 py-0.5 rounded-full bg-[#21262d] text-[#8b949e] border border-[#30363d]">
              🔧 {usage.skills ?? 0} skills
            </span>
            {(usage.mcpServers || []).map((s: string) => (
              <span key={s} title={`MCP server '${s}' is connected`}
                className="text-[10px] px-2 py-0.5 rounded-full bg-[#3fb95022] text-[#3fb950] border border-[#3fb95044] max-w-[220px] truncate">
                🧰 {s}
              </span>
            ))}
            {(usage.mcpTools || 0) > 0 && (
              <span title="Tools contributed by the connected MCP servers"
                className="text-[10px] px-2 py-0.5 rounded-full bg-[#21262d] text-[#8b949e] border border-[#30363d]">
                {usage.mcpTools} MCP tools
              </span>
            )}
            {(usage.used || []).length > 0
              ? (usage.used || []).map((u: string) => (
                <span key={u} title="Called while answering the last message"
                  className="text-[10px] px-2 py-0.5 rounded-full bg-[#1f6feb22] text-[#58a6ff] border border-[#1f6feb44] max-w-[280px] truncate">
                  ✓ used {u}
                </span>
              ))
              : <span title="No tool was called for the last message"
                  className="text-[10px] px-2 py-0.5 rounded-full bg-[#21262d] text-[#8b949e] border border-[#30363d]">
                  no tool used
                </span>}
          </div>
        )}
        {anchors.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-2">
            {anchors.map((a, i) => (
              <span key={i} title={a.text}
                className="text-[10px] px-2 py-0.5 rounded-full bg-[#1f6feb22] text-[#58a6ff] border border-[#1f6feb44] max-w-[280px] truncate">
                🧠 {a.type === 'facts' ? 'memory' : a.type === 'timeline' ? 'timeline' : 'recall'} · {a.text.slice(0, 60)}…
              </span>
            ))}
          </div>
        )}
        {activity && (
          <div className="mb-2 flex items-center gap-2 text-[11px] text-[#d29922]"
            role="status" aria-live="polite">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#d29922] animate-pulse" />
            <span>{activity}</span>
            <span className="text-[#8b949e]">
              This can take up to a minute on a local model.
            </span>
          </div>
        )}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {attachments.map((a, i) => (
              <div key={i} className="flex items-center gap-1.5 bg-[#21262d] border border-[#30363d] rounded-md pl-2 pr-1 py-1 text-xs text-[#e8eaed]">
                {a.kind === 'image' && a.preview ? (
                  <img src={a.preview} alt={a.name} className="h-8 w-8 object-cover rounded" />
                ) : (
                  <span>{a.kind === 'text' ? '📄' : '📎'}</span>
                )}
                <span className="max-w-[140px] truncate">{a.name}</span>
                <button onClick={() => removeAttachment(i)} title="Remove"
                  className="text-[#8b949e] hover:text-[#f85149] px-1">✕</button>
              </div>
            ))}
          </div>
        )}
        <div className="flex gap-2">
          <input ref={fileInputRef} type="file" multiple className="hidden"
            accept="image/*,.txt,.md,.json,.csv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.xml,.yaml,.yml,.ini,.cfg,.sh,.bat,.ps1,.toml,.sql,.pdf,.docx,.xlsx,.pptx"
            onChange={e => { handleFiles(e.target.files); e.target.value = ''; }} />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isLoading || wsState !== 'connected' || processingFiles}
            title="Attach an image, text file or document"
            className="bg-[#161b22] border border-[#30363d] hover:border-[#484f58] disabled:opacity-50 rounded-lg px-3 py-2 text-sm text-[#8b949e] transition-colors"
          >
            {processingFiles ? '⟳' : '📎'}
          </button>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={wsState === 'connected' ? 'Message Addled...' : 'Connecting to Addled...'}
            disabled={wsState !== 'connected' || isLoading}
            rows={1}
            className="flex-1 bg-[#161b22] border border-[#30363d] rounded-lg px-4 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] resize-none focus:outline-none focus:border-[#3380FF] disabled:opacity-50"
          />
          <button
            onClick={handleSend}
            disabled={(!input.trim() && !attachments.length) || isLoading || wsState !== 'connected'}
            className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg px-4 py-2 text-sm font-medium transition-colors"
          >
            {isLoading ? '...' : 'Send'}
          </button>
        </div>
        <p className="text-[10px] text-[#484f58] mt-1.5">Paste, drop or attach an image, text file or document. Images are analyzed by the visual model and described to the main model; text is sent as content.</p>
      </div>
    </div>
  );
}
