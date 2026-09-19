'use client';

import { useState, useRef, useEffect } from 'react';
import { useWS } from '@/lib/useWS';
import { getChatMessages, setChatMessages, subscribeChatMessages } from '@/lib/chatStore';

interface Attachment { name: string; kind: 'image' | 'text' | 'file'; data?: string; preview?: string; size?: number; }

interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  streaming?: boolean;
  attachments?: { name: string; kind: string; preview?: string }[];
}

const TEXT_EXTS = /\.(txt|md|json|csv|log|py|js|ts|tsx|jsx|html|css|xml|yaml|yml|ini|cfg|sh|bat|ps1|toml|sql)$/i;
const MAX_ATTACHMENTS = 5;

async function downscaleImage(file: File, maxSide = 1024): Promise<string> {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => rej(new Error('Could not read image'));
      i.src = url;
    });
    let { width, height } = img;
    const scale = Math.min(1, maxSide / Math.max(width, height));
    width = Math.round(width * scale);
    height = Math.round(height * scale);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d')!.drawImage(img, 0, 0, width, height);
    return canvas.toDataURL('image/jpeg', 0.85).split(',')[1] || '';
  } catch {
    // Canvas failed (rare) — send the raw file instead
    return await new Promise<string>((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res((fr.result as string).split(',')[1] || '');
      fr.onerror = () => rej(new Error('Could not read image'));
      fr.readAsDataURL(file);
    });
  } finally {
    URL.revokeObjectURL(url);
  }
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
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [processingFiles, setProcessingFiles] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [anchors, setAnchors] = useState<{type: string; text: string}[]>([]);
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

  // Messages pushed by the floating character / voice listener
  useEffect(() => onNotification('chat.push', (params: any) => {
    if (params?.role && params?.content) {
      setMessages(prev => [...prev, {
        role: params.role,
        content: params.content,
        timestamp: Date.now(),
      }]);
    }
  }), [onNotification]);

  // Memory anchors: which memories the backend injected into this turn
  useEffect(() => onNotification('memory.anchors', (params: any) => {
    if (params?.anchors) setAnchors(params.anchors);
    const t = setTimeout(() => setAnchors([]), 12000);
    return () => clearTimeout(t);
  }), [onNotification]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleFiles = async (files: FileList | null) => {
    if (!files || !files.length) return;
    setProcessingFiles(true);
    try {
      const next: Attachment[] = [];
      for (const f of Array.from(files).slice(0, MAX_ATTACHMENTS)) {
        if (f.type.startsWith('image/')) {
          const preview = URL.createObjectURL(f);
          const data = await downscaleImage(f);
          next.push({ name: f.name, kind: 'image', data, preview });
        } else if (TEXT_EXTS.test(f.name) || f.type.startsWith('text/')) {
          if (f.size <= 1024 * 1024) {
            const text = await f.text();
            next.push({ name: f.name, kind: 'text', data: text.slice(0, 100000) });
          } else {
            next.push({ name: f.name, kind: 'file', size: f.size });
          }
        } else {
          next.push({ name: f.name, kind: 'file', size: f.size });
        }
      }
      setAttachments(prev => [...prev, ...next].slice(0, MAX_ATTACHMENTS));
    } catch {
      /* unreadable file — ignore */
    }
    setProcessingFiles(false);
  };

  const removeAttachment = (idx: number) => {
    setAttachments(prev => {
      const a = prev[idx];
      if (a?.preview) URL.revokeObjectURL(a.preview);
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
      const result = await send('chat.send', { message: text, attachments: sentAtts });
      applyReply(result?.response || 'No response');
    } catch (err: any) {
      applyReply(`Error: ${err.message}`);
    } finally {
      setIsLoading(false);
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
            <p className="text-sm font-medium text-[#e8eaed]">Drop files to attach</p>
            <p className="text-xs text-[#8b949e] mt-1">Images, text files, code — up to 5</p>
          </div>
        </div>
      )}
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Chat</h1>
        <span className="text-xs text-[#8b949e]">
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
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="border-t border-[#30363d] p-4">
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
            accept="image/*,.txt,.md,.json,.csv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.xml,.yaml,.yml,.ini,.cfg,.sh,.bat,.ps1,.toml,.sql"
            onChange={e => { handleFiles(e.target.files); e.target.value = ''; }} />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isLoading || wsState !== 'connected' || processingFiles}
            title="Attach image or file"
            className="bg-[#161b22] border border-[#30363d] hover:border-[#484f58] disabled:opacity-50 rounded-lg px-3 py-2 text-sm text-[#8b949e] transition-colors"
          >
            {processingFiles ? '⟳' : '📎'}
          </button>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
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
        <p className="text-[10px] text-[#484f58] mt-1.5">Images are analyzed by the visual model and described to the main model. Text files are sent as content.</p>
      </div>
    </div>
  );
}
