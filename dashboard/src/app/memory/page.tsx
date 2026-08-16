'use client';

import { useEffect, useState } from 'react';
import { useWS } from '@/lib/useWS';

interface Summary {
  ts: number;
  summary: string;
}

interface MemoryEntry {
  id: number;
  role?: string;
  text?: string;
  timestamp?: number;
  metadata?: { role?: string; text?: string } | null;
}

export default function MemoryPage() {
  const { state: wsState, send } = useWS();
  const [summaries, setSummaries] = useState<Summary[]>([]);
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    if (wsState !== 'connected') return;
    setLoading(true);
    try {
      const result = await send('memory.list', {});
      setSummaries(result?.summaries || []);
      setMemories(result?.memories || []);
      setError('');
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [wsState]); // eslint-disable-line react-hooks/exhaustive-deps

  const deleteSummary = async (index: number) => {
    try {
      const r = await send('memory.deleteSummary', { index });
      if (r?.success) load();
    } catch { /* ignore */ }
  };

  const deleteMemory = async (id: number) => {
    try {
      const r = await send('memory.deleteMemory', { id });
      if (r?.success) load();
    } catch { /* ignore */ }
  };

  const clearAll = async () => {
    if (!confirm('Delete ALL session summaries?')) return;
    try {
      await send('memory.clear', {});
      load();
    } catch { /* ignore */ }
  };

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Memory</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={load}
            disabled={wsState !== 'connected'}
            className="text-xs px-2 py-1 rounded bg-[#21262d] hover:bg-[#30363d] text-[#8b949e] disabled:opacity-40"
          >
            ↻ Refresh
          </button>
          <button
            onClick={clearAll}
            disabled={summaries.length === 0}
            className="text-xs px-2 py-1 rounded bg-[#3d1f1f] hover:bg-[#5a2a2a] text-[#f85149] disabled:opacity-40"
          >
            Clear summaries
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-6">
        {wsState !== 'connected' && (
          <p className="text-sm text-[#f85149]">Backend not connected.</p>
        )}
        {error && <p className="text-sm text-[#f85149]">{error}</p>}
        {loading && <p className="text-sm text-[#8b949e]">Loading…</p>}

        {/* Session summaries */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
            Session summaries ({summaries.length})
          </h2>
          {summaries.length === 0 && !loading && (
            <p className="text-sm text-[#8b949e]">
              No session summaries yet — they're written when you quit Addled.
            </p>
          )}
          <div className="space-y-2">
            {[...summaries].reverse().map((s, i) => {
              const index = summaries.length - 1 - i; // original index
              return (
                <div key={`${s.ts}-${i}`} className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-[#8b949e] mb-1">
                      {new Date(s.ts * 1000).toLocaleString()}
                    </p>
                    <p className="text-sm text-[#e8eaed] whitespace-pre-wrap break-words">{s.summary}</p>
                  </div>
                  <button
                    onClick={() => deleteSummary(index)}
                    className="text-xs text-[#8b949e] hover:text-[#f85149] px-1"
                    title="Delete summary"
                  >
                    ✕
                  </button>
                </div>
              );
            })}
          </div>
        </section>

        {/* Conversation memories */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
            Conversation memories ({memories.length})
          </h2>
          {memories.length === 0 && !loading && (
            <p className="text-sm text-[#8b949e]">No stored conversation turns.</p>
          )}
          <div className="space-y-2">
            {memories.map((m) => {
              const meta = m.metadata || {};
              const text = meta.text || m.text || '';
              const role = meta.role || m.role || 'user';
              return (
                <div key={m.id} className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 flex items-start gap-3">
                  <span className={`text-[10px] font-semibold uppercase mt-0.5 px-1.5 py-0.5 rounded ${role === 'assistant' ? 'bg-[#1f2937] text-[#58a6ff]' : 'bg-[#2d1f1f] text-[#f0883e]'}`}>
                    {role}
                  </span>
                  <p className="flex-1 text-sm text-[#e8eaed] whitespace-pre-wrap break-words min-w-0">{text}</p>
                  <button
                    onClick={() => deleteMemory(m.id)}
                    className="text-xs text-[#8b949e] hover:text-[#f85149] px-1"
                    title="Delete memory"
                  >
                    ✕
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}
