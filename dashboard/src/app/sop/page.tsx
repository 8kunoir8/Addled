'use client';

import { useEffect, useState } from 'react';
import { useWS } from '@/lib/useWS';

interface Procedure {
  id: string;
  category: string;
  title: string;
  steps: string[];
  tools: string[];
  uses?: number;
  successes?: number;
  created?: string;
  updated?: string;
  source?: string;
}

interface Category { category: string; count: number }

const SOURCE_BADGE: Record<string, string> = {
  seed: 'bg-[#1f3a5f] text-[#79b8ff]',
  learned: 'bg-[#2d3a1f] text-[#a5d66a]',
  manual: 'bg-[#3a2d1f] text-[#d6a56a]',
};

export default function SopPage() {
  const { state: wsState, send } = useWS();
  const [procedures, setProcedures] = useState<Procedure[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [info, setInfo] = useState<any>(null);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<Procedure | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const [draftTitle, setDraftTitle] = useState('');
  const [draftCategory, setDraftCategory] = useState('');
  const [draftSteps, setDraftSteps] = useState('');
  const [draftTools, setDraftTools] = useState('');

  const load = async (category?: string) => {
    if (wsState !== 'connected') return;
    setLoading(true);
    try {
      const r = await send('sop.list', category ? { category } : {});
      setProcedures(r?.procedures || []);
      setCategories(r?.categories || []);
      setError('');
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  };

  const loadStatus = async () => {
    if (wsState !== 'connected') return;
    try { setInfo(await send('sop.status', {})); } catch { /* panel is optional */ }
  };

  useEffect(() => { load(filter || undefined); loadStatus(); },
    [wsState, filter]); // eslint-disable-line react-hooks/exhaustive-deps

  const openProcedure = (p: Procedure) => {
    setSelected(p);
    setDraftTitle(p.title || '');
    setDraftCategory(p.category || '');
    setDraftSteps((p.steps || []).join('\n'));
    setDraftTools((p.tools || []).join(', '));
    setNotice('');
  };

  const startNew = () => {
    setSelected(null);
    setDraftTitle('');
    setDraftCategory(filter || 'general');
    setDraftSteps('');
    setDraftTools('');
    setNotice('');
  };

  const save = async () => {
    if (!draftTitle.trim()) { setError('A procedure needs a title.'); return; }
    setBusy('save');
    try {
      const r = await send('sop.save', {
        id: selected?.id,
        title: draftTitle.trim(),
        category: draftCategory.trim() || 'general',
        steps: draftSteps.split('\n').map(s => s.trim()).filter(Boolean),
        tools: draftTools.split(',').map(s => s.trim()).filter(Boolean),
      });
      if (r?.success) {
        setNotice(`Saved "${r.procedure?.title}".`);
        setSelected(r.procedure);
        setError('');
        await load(filter || undefined);
        await loadStatus();
      } else {
        setError(r?.error || 'Could not save.');
      }
    } catch (e: any) { setError(e.message); }
    setBusy('');
  };

  const remove = async () => {
    if (!selected) return;
    setBusy('delete');
    try {
      const r = await send('sop.delete', { id: selected.id });
      if (r?.success) {
        setNotice(`Deleted "${selected.title}".`);
        setSelected(null);
        await load(filter || undefined);
        await loadStatus();
      } else {
        setError(r?.error || 'Could not delete.');
      }
    } catch (e: any) { setError(e.message); }
    setBusy('');
  };

  const restoreSeeds = async () => {
    setBusy('seed');
    try {
      const r = await send('sop.reseed', {});
      setNotice(r?.added ? `Restored ${r.added} built-in procedure(s).`
                         : 'The built-in procedures are already present.');
      await load(filter || undefined);
      await loadStatus();
    } catch (e: any) { setError(e.message); }
    setBusy('');
  };

  const field = 'w-full bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]';

  return (
    <div className="flex h-full">
      {/* categories */}
      <div className="w-48 border-r border-[#30363d] p-2 space-y-0.5 overflow-y-auto">
        <button onClick={() => setFilter('')}
          className={`w-full text-left px-3 py-2 rounded-md text-sm ${!filter ? 'bg-[#1f6feb] text-white' : 'text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]'}`}>
          All <span className="float-right text-xs opacity-70">{procedures.length && !filter ? procedures.length : ''}</span>
        </button>
        {categories.map(c => (
          <button key={c.category} onClick={() => setFilter(c.category)}
            className={`w-full text-left px-3 py-2 rounded-md text-sm ${filter === c.category ? 'bg-[#1f6feb] text-white' : 'text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]'}`}>
            {c.category}<span className="float-right text-xs opacity-70">{c.count}</span>
          </button>
        ))}
      </div>

      {/* list */}
      <div className="w-72 border-r border-[#30363d] overflow-y-auto">
        <div className="p-3 border-b border-[#30363d] flex items-center gap-2">
          <button onClick={startNew} className="px-3 py-1.5 rounded bg-[#1f6feb] text-white text-sm">+ New</button>
          <button onClick={restoreSeeds} disabled={busy === 'seed'}
            className="px-3 py-1.5 rounded bg-[#21262d] text-[#8b949e] hover:text-[#e8eaed] text-sm">
            {busy === 'seed' ? '…' : 'Restore built-ins'}
          </button>
        </div>
        {info && (
          <div className="px-3 py-2 text-xs text-[#8b949e] border-b border-[#30363d]">
            {info.enabled
              ? <>{info.count} stored · {info.learned} learned · learning {info.learn ? 'on' : 'off'}</>
              : <span className="text-[#d29922]">Procedures are turned off in Settings.</span>}
          </div>
        )}
        {loading && <div className="p-4 text-sm text-[#8b949e]">Loading…</div>}
        {!loading && procedures.length === 0 && (
          <div className="p-4 text-sm text-[#8b949e]">
            Nothing recorded yet. Procedures are added automatically after a task
            that used tools and worked, or by hand with <span className="text-[#e8eaed]">+ New</span>.
          </div>
        )}
        {procedures.map(p => (
          <button key={p.id} onClick={() => openProcedure(p)}
            className={`w-full text-left px-3 py-2.5 border-b border-[#21262d] ${selected?.id === p.id ? 'bg-[#161b22]' : 'hover:bg-[#161b22]'}`}>
            <div className="text-sm text-[#e8eaed] truncate">{p.title}</div>
            <div className="mt-1 flex items-center gap-2 text-xs">
              <span className={`px-1.5 py-0.5 rounded ${SOURCE_BADGE[p.source || 'manual'] || SOURCE_BADGE.manual}`}>
                {p.source || 'manual'}
              </span>
              <span className="text-[#8b949e]">{p.category}</span>
              <span className="text-[#8b949e]">· {(p.steps || []).length} steps</span>
              {(p.uses || 0) > 0 && (
                <span className="text-[#8b949e]">· {p.successes || 0}/{p.uses} ok</span>
              )}
            </div>
          </button>
        ))}
      </div>

      {/* editor */}
      <div className="flex-1 overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">
            {selected ? 'Edit procedure' : 'New procedure'}
          </h2>
          <div className="flex gap-2">
            {selected && (
              <button onClick={remove} disabled={busy === 'delete'}
                className="px-3 py-1.5 rounded bg-[#3d1d1d] text-[#f85149] text-sm">
                {busy === 'delete' ? '…' : 'Delete'}
              </button>
            )}
            <button onClick={save} disabled={busy === 'save'}
              className="px-4 py-1.5 rounded bg-[#1f6feb] text-white text-sm">
              {busy === 'save' ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>

        {error && <div className="mb-3 text-sm text-[#f85149]">{error}</div>}
        {notice && <div className="mb-3 text-sm text-[#3fb950]">{notice}</div>}

        <label className="block text-xs text-[#8b949e] mb-1">Title</label>
        <input value={draftTitle} onChange={e => setDraftTitle(e.target.value)}
          placeholder="Inspect before overwriting" className={`${field} mb-4`} />

        <label className="block text-xs text-[#8b949e] mb-1">
          Category — similar tasks share one, so this decides which procedure is offered
        </label>
        <input value={draftCategory} onChange={e => setDraftCategory(e.target.value)}
          placeholder="files" className={`${field} mb-4`} />

        <label className="block text-xs text-[#8b949e] mb-1">Steps — one per line, in order</label>
        <textarea value={draftSteps} onChange={e => setDraftSteps(e.target.value)} rows={10}
          placeholder={'Read the existing file first\nWrite the change\nRead it back to confirm'}
          className={`${field} mb-4 font-mono`} />

        <label className="block text-xs text-[#8b949e] mb-1">Tools — comma separated</label>
        <input value={draftTools} onChange={e => setDraftTools(e.target.value)}
          placeholder="read_file, write_file" className={`${field} mb-4 font-mono`} />

        {selected && (
          <div className="mt-4 text-xs text-[#8b949e] font-mono space-y-1">
            <div>id: {selected.id}</div>
            <div>applied {selected.uses || 0} time(s), {selected.successes || 0} successful</div>
            {selected.updated && <div>updated: {selected.updated}</div>}
          </div>
        )}
      </div>
    </div>
  );
}
