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

interface Fact {
  id: number;
  text: string;
  ts?: number;
  source?: string;
}

export default function MemoryPage() {
  const { state: wsState, send } = useWS();
  const [summaries, setSummaries] = useState<Summary[]>([]);
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [newFact, setNewFact] = useState('');
  const [triples, setTriples] = useState<any[]>([]);
  const [journalDays, setJournalDays] = useState<any[]>([]);
  const [profile, setProfile] = useState<any>({});
  const [editingProfile, setEditingProfile] = useState(false);
  const [profileDraft, setProfileDraft] = useState<any>({});
  const [profileError, setProfileError] = useState('');
  const [linkStats, setLinkStats] = useState<any>({});
  const [files, setFiles] = useState<any[]>([]);
  const [relKind, setRelKind] = useState('fact');
  const [relId, setRelId] = useState('');
  const [related, setRelated] = useState<any[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    if (wsState !== 'connected') return;
    setLoading(true);
    try {
      const result = await send('memory.list', {});
      setSummaries(result?.summaries || []);
      setMemories(result?.memories || []);
      try {
        const f = await send('memory.getFacts', {});
        setFacts(f?.facts || []);
      } catch { /* older backend */ }
      try {
        const t = await send('memory.listTriples', {});
        setTriples(t?.triples || []);
      } catch { /* older backend */ }
      try {
        const j = await send('journal.list', { limit: 14 });
        setJournalDays(j?.days || []);
      } catch { /* older backend */ }
      try {
        const p = await send('profile.get', {});
        setProfile(p?.profile || {});
      } catch { /* older backend */ }
      try {
        const l = await send('memory.links', {});
        setLinkStats(l || {});
        const fl = await send('memory.files', {});
        setFiles(fl?.files || []);
      } catch { /* older backend */ }
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

  const addFact = async () => {
    const text = newFact.trim();
    if (!text) return;
    setNewFact('');
    try {
      const r = await send('memory.setFact', { fact: text, source: 'dashboard' });
      if (r?.success) {
        const f = await send('memory.getFacts', {});
        setFacts(f?.facts || []);
      }
    } catch { /* ignore */ }
  };

  const deleteFact = async (id: number) => {
    try {
      const r = await send('memory.deleteFact', { id });
      if (r?.success) {
        const f = await send('memory.getFacts', {});
        setFacts(f?.facts || []);
      }
    } catch { /* ignore */ }
  };

  const deleteTriple = async (id: number) => {
    try {
      const r = await send('memory.deleteTriple', { id });
      if (r?.success) {
        const t = await send('memory.listTriples', {});
        setTriples(t?.triples || []);
      }
    } catch { /* ignore */ }
  };

  const lookupRelated = async () => {
    const id = relId.trim();
    if (!id) return;
    try {
      const r = await send('memory.related', { kind: relKind, id, depth: 2 });
      setRelated(r?.related || []);
    } catch {
      setRelated([]);
    }
  };

  const deleteLink = async (id: number) => {
    try {
      const r = await send('memory.deleteLink', { id });
      if (r?.success) {
        const l = await send('memory.links', {});
        setLinkStats(l || {});
        if (related) lookupRelated();
      }
    } catch { /* ignore */ }
  };

  const pruneLinks = async () => {
    try {
      const r = await send('memory.pruneLinks', {});
      if (r?.success) load();
    } catch { /* ignore */ }
  };

  const saveProfile = async () => {
    const fields = {
      tone: String(profileDraft.tone || '').trim(),
      hours: { start: profileDraft.start || '', end: profileDraft.end || '' },
      rituals: String(profileDraft.rituals || '').split(',')
        .map(s => s.trim()).filter(Boolean),
      preferences: String(profileDraft.preferences || '').split('\n')
        .map(s => s.trim()).filter(Boolean),
    };
    try {
      const r = await send('profile.update', { fields });
      if (r?.success) { setProfile(r.profile); setEditingProfile(false); setProfileError(''); }
      else setProfileError(r?.error || 'Could not save the profile');
    } catch (e: any) { setProfileError(e?.message || 'Could not save the profile'); }
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

        {/* Core facts */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
            Saved facts ({facts.length})
          </h2>
          <div className="flex gap-2 mb-3">
            <input
              value={newFact}
              onChange={(e) => setNewFact(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') addFact(); }}
              placeholder="e.g. Kun prefers PowerShell over CMD"
              className="flex-1 bg-[#161b22] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"
            />
            <button
              onClick={addFact}
              disabled={!newFact.trim()}
              className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium"
            >
              Save
            </button>
          </div>
          {facts.length === 0 && !loading && (
            <p className="text-sm text-[#8b949e]">No saved facts yet — add one above, or let the agent save its own (Settings → Memory → Auto memory notes).</p>
          )}
          <div className="space-y-2">
            {facts.map((f) => (
              <div key={f.id} className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 flex items-start gap-3">
                <span className="text-[10px] font-semibold uppercase mt-0.5 px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff]">fact</span>
                <p className="flex-1 text-sm text-[#e8eaed] whitespace-pre-wrap break-words min-w-0">{f.text}</p>
                <button
                  onClick={() => deleteFact(f.id)}
                  className="text-xs text-[#8b949e] hover:text-[#f85149] px-1"
                  title="Delete fact"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        </section>

        {/* Knowledge graph triples */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
            Knowledge graph ({triples.length})
          </h2>
          {triples.length === 0 && !loading && (
            <p className="text-sm text-[#8b949e]">No fact triples yet — enable Knowledge graph extraction in Settings → Memory (uses provider credits).</p>
          )}
          <div className="space-y-2">
            {triples.map((t) => (
              <div key={t.id} className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 flex items-start gap-3">
                <span className="text-[10px] font-semibold uppercase mt-0.5 px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff]">triple</span>
                <p className="flex-1 text-sm text-[#e8eaed] break-words min-w-0">
                  <span className="text-[#f0883e]">{t.subject}</span>
                  <span className="text-[#8b949e]"> {t.relation} </span>
                  <span className="text-[#58a6ff]">{t.object}</span>
                </p>
                <button
                  onClick={() => deleteTriple(t.id)}
                  className="text-xs text-[#8b949e] hover:text-[#f85149] px-1"
                  title="Delete triple"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        </section>

        {/* Relations — the graph across every memory store and the filesystem */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
            Relations ({linkStats.links ?? 0})
          </h2>
          <div className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 mb-3">
            <p className="text-xs text-[#e8eaed]">
              {linkStats.links ?? 0} link{(linkStats.links ?? 0) === 1 ? '' : 's'} across{' '}
              {linkStats.items ?? 0} item{(linkStats.items ?? 0) === 1 ? '' : 's'} ·{' '}
              {linkStats.files ?? 0} file{(linkStats.files ?? 0) === 1 ? '' : 's'} referenced
            </p>
            {linkStats.by_relation && Object.keys(linkStats.by_relation).length > 0 && (
              <p className="text-[11px] text-[#8b949e] mt-1">
                {Object.entries(linkStats.by_relation as Record<string, number>)
                  .map(([rel, n]) => `${rel} ${n}`)
                  .join(' · ')}
              </p>
            )}
          </div>

          <div className="flex gap-2 mb-3">
            <select
              value={relKind}
              onChange={(e) => setRelKind(e.target.value)}
              className="bg-[#161b22] border border-[#30363d] rounded-lg px-2 py-2 text-sm text-[#e8eaed] focus:outline-none focus:border-[#3380FF]"
            >
              {['fact', 'triple', 'memory', 'summary', 'journal', 'wiki', 'file'].map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
            <input
              value={relId}
              onChange={(e) => setRelId(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') lookupRelated(); }}
              placeholder="id — e.g. 12, a slug, a journal date, a file path"
              className="flex-1 bg-[#161b22] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"
            />
            <button
              onClick={lookupRelated}
              disabled={!relId.trim()}
              className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium"
            >
              Trace
            </button>
            <button
              onClick={pruneLinks}
              className="border border-[#30363d] hover:border-[#8b949e] text-[#8b949e] hover:text-[#e8eaed] rounded-lg px-3 py-2 text-sm"
              title="Drop links whose target no longer exists"
            >
              Prune
            </button>
          </div>

          {related !== null && related.length === 0 && (
            <p className="text-sm text-[#8b949e]">Nothing is connected to that item.</p>
          )}
          <div className="space-y-2">
            {related?.map((r, i) => (
              <div key={`${r.kind}-${r.ref_id}-${i}`} className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 flex items-start gap-3">
                <span className="text-[10px] font-semibold uppercase mt-0.5 px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff]">
                  {r.kind}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-[#e8eaed] break-words">{r.ref_id}</p>
                  <p className="text-[11px] text-[#8b949e]">
                    {r.via?.rel || 'relates_to'}
                    {r.depth > 1 ? ` · ${r.depth} hops` : ''}
                    {r.label ? ` · ${r.label}` : ''}
                    {r.exists === false ? ' · missing' : ''}
                  </p>
                </div>
              </div>
            ))}
          </div>

          {files.length > 0 && (
            <div className="mt-4">
              <p className="text-[10px] uppercase tracking-wide text-[#8b949e] mb-2">
                Files memory points at
              </p>
              <div className="space-y-1">
                {files.slice(0, 12).map((f) => (
                  <div key={f.ref_id} className="flex items-baseline gap-2 text-xs">
                    <span className={`${f.exists === false ? 'text-[#8b949e] line-through' : 'text-[#e8eaed]'} break-all`}>
                      {f.ref_id}
                    </span>
                    <span className="text-[#8b949e] shrink-0">{f.count ?? ''}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* Conversation memories */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
            Timeline & User model
          </h2>
          {/* Previously hidden unless it had preferences, and there was no way
              to change it at all — the handler did not exist. */}
          <div className="rounded-lg border border-[#30363d] bg-[#161b22] p-3 mb-3">
            <div className="flex items-center justify-between mb-1">
              <p className="text-[10px] uppercase tracking-wide text-[#8b949e]">Learned profile</p>
              <button
                onClick={() => {
                  setProfileDraft({
                    tone: profile.tone || '',
                    start: profile.hours?.start || '',
                    end: profile.hours?.end || '',
                    rituals: (profile.rituals || []).join(', '),
                    preferences: (profile.preferences || []).join('\n'),
                  });
                  setEditingProfile(e => !e);
                  setProfileError('');
                }}
                className="text-[11px] text-[#8b949e] hover:text-[#e8eaed]">
                {editingProfile ? 'Cancel' : 'Edit'}
              </button>
            </div>
            {!editingProfile ? (
              <>
                <p className="text-xs text-[#e8eaed]">
                  {profile.tone && <span>Tone: {profile.tone} · </span>}
                  {profile.hours?.start && <span>Around {profile.hours.start}–{profile.hours.end} · </span>}
                  <span>{profile.preferences?.length || 0} preferences</span>
                </p>
                {profile.rituals?.length > 0 && (
                  <p className="text-[11px] text-[#8b949e] mt-1">Rituals: {profile.rituals.join(' · ')}</p>
                )}
                {profile.preferences?.length > 0 && (
                  <ul className="mt-1 space-y-0.5">
                    {profile.preferences.slice(0, 6).map((p: string, i: number) => (
                      <li key={i} className="text-[11px] text-[#8b949e]">· {p}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <div className="space-y-2 mt-2">
                <label className="block text-[11px] text-[#8b949e]">Tone
                  <input value={profileDraft.tone || ''} onChange={e => setProfileDraft((d: any) => ({ ...d, tone: e.target.value }))}
                    className="mt-0.5 w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]" />
                </label>
                <div className="flex items-center gap-2">
                  <label className="text-[11px] text-[#8b949e] flex-1">Active from
                    <input type="time" value={profileDraft.start || ''} onChange={e => setProfileDraft((d: any) => ({ ...d, start: e.target.value }))}
                      className="mt-0.5 w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]" />
                  </label>
                  <label className="text-[11px] text-[#8b949e] flex-1">to
                    <input type="time" value={profileDraft.end || ''} onChange={e => setProfileDraft((d: any) => ({ ...d, end: e.target.value }))}
                      className="mt-0.5 w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]" />
                  </label>
                </div>
                <label className="block text-[11px] text-[#8b949e]">Rituals (comma separated)
                  <input value={profileDraft.rituals || ''} onChange={e => setProfileDraft((d: any) => ({ ...d, rituals: e.target.value }))}
                    className="mt-0.5 w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]" />
                </label>
                <label className="block text-[11px] text-[#8b949e]">Preferences (one per line)
                  <textarea value={profileDraft.preferences || ''} onChange={e => setProfileDraft((d: any) => ({ ...d, preferences: e.target.value }))} rows={4}
                    className="mt-0.5 w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]" />
                </label>
                {profileError && <p className="text-[11px] text-[#f85149]">{profileError}</p>}
                <button onClick={saveProfile}
                  className="bg-[#3380FF] hover:bg-[#4d94ff] text-white rounded px-3 py-1 text-xs font-medium">Save profile</button>
              </div>
            )}
          </div>
          {journalDays.length === 0 ? (
            <p className="text-sm text-[#8b949e]">No journal days yet — the timeline fills in as you chat.</p>
          ) : (
            <div className="space-y-2">
              {journalDays.slice(0, 7).map((d) => (
                <div key={d.date} className="rounded-lg border border-[#30363d] bg-[#161b22] p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-[#58a6ff]">{d.date}</span>
                    <span className="text-[10px] text-[#8b949e]">{d.entries} turns{d.summarized ? ' · summarized' : ''}</span>
                  </div>
                  {d.summary && <p className="text-xs text-[#e8eaed] mt-1">{d.summary}</p>}
                </div>
              ))}
            </div>
          )}
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
