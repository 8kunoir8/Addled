'use client';

import { useEffect, useState } from 'react';
import { useWS } from '@/lib/useWS';

interface WikiPage {
  slug: string;
  title: string;
  tags: string[];
  sources: string[];
  created?: string;
  updated?: string;
  links?: string[];
  chars?: number;
  body?: string;
}

interface Lint {
  pages: number;
  healthy: boolean;
  broken_links: { slug: string; missing: string }[];
  orphans: string[];
  empty: string[];
  duplicate_titles?: string[];
}

export default function WikiPage() {
  const { state: wsState, send } = useWS();
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [dir, setDir] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<WikiPage | null>(null);
  const [backlinks, setBacklinks] = useState<string[]>([]);
  const [lint, setLint] = useState<Lint | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');

  const [draftTitle, setDraftTitle] = useState('');
  const [draftBody, setDraftBody] = useState('');
  const [draftTags, setDraftTags] = useState('');
  const [dirty, setDirty] = useState(false);
  const [ingestPath, setIngestPath] = useState('');

  const load = async () => {
    if (wsState !== 'connected') return;
    setLoading(true);
    try {
      const r = await send('wiki.list', {});
      setPages(r?.pages || []);
      setDir(r?.dir || '');
      setError('');
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [wsState]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = async (slug: string) => {
    try {
      const r = await send('wiki.get', { slug });
      if (!r?.success) return;
      const page: WikiPage = r.page;
      setSelected(page);
      setBacklinks(r.page.backlinks || []);
      setDraftTitle(page.title || page.slug);
      setDraftBody(page.body || '');
      setDraftTags((page.tags || []).join(', '));
      setDirty(false);
    } catch { /* ignore */ }
  };

  const search = async () => {
    const q = query.trim();
    if (!q) { load(); return; }
    try {
      const r = await send('wiki.search', { query: q });
      setPages(r?.pages || []);
    } catch { /* ignore */ }
  };

  const save = async () => {
    if (!selected) return;
    setBusy('Saving…');
    try {
      const r = await send('wiki.save', {
        slug: selected.slug,
        title: draftTitle,
        body: draftBody,
        tags: draftTags,
      });
      if (r?.success) {
        setDirty(false);
        await open(selected.slug);
        await load();
      } else {
        setError(r?.error || 'Save failed');
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  const create = async () => {
    const title = prompt('New page title');
    if (!title?.trim()) return;
    try {
      const r = await send('wiki.save', {
        slug: title,
        title,
        body: '# ' + title + '\n\n',
      });
      if (r?.success) {
        await load();
        open(r.page.slug);
      } else {
        setError(r?.error || 'Could not create the page');
      }
    } catch { /* ignore */ }
  };

  const remove = async () => {
    if (!selected) return;
    if (!confirm(`Delete the page "${selected.title}"?`)) return;
    try {
      const r = await send('wiki.delete', { slug: selected.slug });
      if (r?.success) {
        setSelected(null);
        await load();
      }
    } catch { /* ignore */ }
  };

  const runLint = async () => {
    setBusy('Checking…');
    try {
      const r = await send('wiki.lint', {});
      if (r?.success) setLint(r as Lint);
    } catch { /* ignore */ } finally {
      setBusy('');
    }
  };

  const ingest = async () => {
    const path = ingestPath.trim();
    if (!path) return;
    setBusy('Reading and rewriting pages…');
    setError('');
    try {
      const r = await send('wiki.ingest', { path });
      if (!r?.success) {
        setError(r?.error || 'Ingest failed');
      } else {
        setIngestPath('');
        await load();
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="flex flex-col h-full">
      <header className="px-4 py-3 border-b border-[#30363d] flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="text-sm font-semibold text-[#e8eaed]">
            Wiki {pages.length > 0 && <span className="text-[#8b949e]">({pages.length})</span>}
          </h1>
          {dir && <p className="text-[11px] text-[#8b949e] truncate">{dir}</p>}
        </div>
        <button
          onClick={create}
          className="text-xs px-2 py-1 rounded border border-[#30363d] hover:border-[#8b949e] text-[#8b949e] hover:text-[#e8eaed]"
        >
          New page
        </button>
        <button
          onClick={runLint}
          className="text-xs px-2 py-1 rounded border border-[#30363d] hover:border-[#8b949e] text-[#8b949e] hover:text-[#e8eaed]"
        >
          Check
        </button>
        <button
          onClick={async () => { await send('wiki.refreshLinks', {}); load(); }}
          className="text-xs px-2 py-1 rounded border border-[#30363d] hover:border-[#8b949e] text-[#8b949e] hover:text-[#e8eaed]"
          title="Re-mirror [[links]] and sources into the memory graph"
        >
          Relink
        </button>
      </header>

      <div className="flex-1 flex min-h-0">
        {/* Page list */}
        <div className="w-72 shrink-0 border-r border-[#30363d] flex flex-col min-h-0">
          <div className="p-2 border-b border-[#30363d] flex gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') search(); }}
              placeholder="Search pages…"
              className="flex-1 bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"
            />
          </div>
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {loading && <p className="text-xs text-[#8b949e] px-1">Loading…</p>}
            {!loading && pages.length === 0 && (
              <p className="text-xs text-[#8b949e] px-1">
                No pages yet — add one, or ingest a file below.
              </p>
            )}
            {pages.map((p) => (
              <button
                key={p.slug}
                onClick={() => open(p.slug)}
                className={`w-full text-left rounded px-2 py-1.5 text-xs ${
                  selected?.slug === p.slug
                    ? 'bg-[#1f2937] text-[#e8eaed]'
                    : 'text-[#8b949e] hover:bg-[#161b22] hover:text-[#e8eaed]'
                }`}
              >
                <span className="block truncate">{p.title}</span>
                <span className="block truncate text-[10px] text-[#484f58]">{p.slug}</span>
              </button>
            ))}
          </div>
          <div className="p-2 border-t border-[#30363d]">
            <input
              value={ingestPath}
              onChange={(e) => setIngestPath(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') ingest(); }}
              placeholder="Ingest a file path…"
              className="w-full bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"
            />
            <button
              onClick={ingest}
              disabled={!ingestPath.trim() || !!busy}
              className="mt-2 w-full bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-2 py-1 text-xs font-medium"
            >
              {busy || 'Ingest into wiki'}
            </button>
          </div>
        </div>

        {/* Page viewer / editor */}
        <div className="flex-1 min-w-0 overflow-y-auto p-4 space-y-4">
          {wsState !== 'connected' && (
            <p className="text-sm text-[#f85149]">Backend not connected.</p>
          )}
          {error && <p className="text-sm text-[#f85149]">{error}</p>}

          {lint && (
            <section className="rounded-lg border border-[#30363d] bg-[#161b22] p-3">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
                Wiki check
              </h2>
              <p className="text-xs text-[#e8eaed] mb-1">
                {lint.pages} page{lint.pages === 1 ? '' : 's'} · {lint.healthy ? 'clean' : 'problems found'}
              </p>
              {lint.broken_links?.length > 0 && (
                <p className="text-[11px] text-[#f0883e]">
                  Broken: {lint.broken_links.map((b) => `${b.slug} → ${b.missing}`).join(', ')}
                </p>
              )}
              {lint.orphans?.length > 0 && (
                <p className="text-[11px] text-[#8b949e]">Orphans: {lint.orphans.join(', ')}</p>
              )}
              {lint.empty?.length > 0 && (
                <p className="text-[11px] text-[#8b949e]">Stubs: {lint.empty.join(', ')}</p>
              )}
              <button
                onClick={() => setLint(null)}
                className="mt-2 text-[11px] text-[#8b949e] hover:text-[#e8eaed]"
              >
                Dismiss
              </button>
            </section>
          )}

          {!selected && !loading && (
            <p className="text-sm text-[#8b949e]">
              Pick a page on the left, or ingest a document to build the wiki out
              of your own files.
            </p>
          )}

          {selected && (
            <>
              <section>
                <div className="flex items-center gap-2 mb-2">
                  <input
                    value={draftTitle}
                    onChange={(e) => { setDraftTitle(e.target.value); setDirty(true); }}
                    className="flex-1 bg-transparent text-lg font-semibold text-[#e8eaed] focus:outline-none border-b border-transparent focus:border-[#3380FF]"
                  />
                  <button
                    onClick={save}
                    disabled={!dirty}
                    className="text-xs px-3 py-1 rounded bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-40 text-white"
                  >
                    Save
                  </button>
                  <button
                    onClick={remove}
                    className="text-xs px-2 py-1 rounded border border-[#3d1f1f] text-[#f85149] hover:bg-[#3d1f1f]"
                  >
                    Delete
                  </button>
                </div>
                <input
                  value={draftTags}
                  onChange={(e) => { setDraftTags(e.target.value); setDirty(true); }}
                  placeholder="tags, comma separated"
                  className="w-full bg-transparent text-[11px] text-[#8b949e] focus:outline-none border-b border-transparent focus:border-[#3380FF]"
                />
              </section>

              <section className="rounded-lg border border-[#30363d] bg-[#161b22] p-3">
                <textarea
                  value={draftBody}
                  onChange={(e) => { setDraftBody(e.target.value); setDirty(true); }}
                  spellCheck={false}
                  className="w-full min-h-[380px] bg-transparent text-sm text-[#e8eaed] font-mono leading-relaxed focus:outline-none resize-y"
                />
              </section>

              <section className="grid grid-cols-2 gap-3">
                <div className="rounded-lg border border-[#30363d] bg-[#161b22] p-3">
                  <p className="text-[10px] uppercase tracking-wide text-[#8b949e] mb-1">Links out</p>
                  {(selected.links || []).length === 0
                    ? <p className="text-xs text-[#8b949e]">None — use [[slug]] to link.</p>
                    : (
                      <div className="flex flex-wrap gap-1">
                        {selected.links!.map((l) => (
                          <button
                            key={l}
                            onClick={() => open(l)}
                            className="text-[11px] px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff] hover:bg-[#2a3542]"
                          >
                            {l}
                          </button>
                        ))}
                      </div>
                    )}
                </div>
                <div className="rounded-lg border border-[#30363d] bg-[#161b22] p-3">
                  <p className="text-[10px] uppercase tracking-wide text-[#8b949e] mb-1">Backlinks</p>
                  {backlinks.length === 0
                    ? <p className="text-xs text-[#8b949e]">Nothing links here.</p>
                    : (
                      <div className="flex flex-wrap gap-1">
                        {backlinks.map((b) => (
                          <button
                            key={b}
                            onClick={() => open(b)}
                            className="text-[11px] px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff] hover:bg-[#2a3542]"
                          >
                            {b}
                          </button>
                        ))}
                      </div>
                    )}
                </div>
              </section>

              {(selected.sources || []).length > 0 && (
                <section className="rounded-lg border border-[#30363d] bg-[#161b22] p-3">
                  <p className="text-[10px] uppercase tracking-wide text-[#8b949e] mb-1">Sources</p>
                  <ul className="space-y-0.5">
                    {selected.sources.map((src) => (
                      <li key={src} className="text-[11px] text-[#e8eaed] break-all">{src}</li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
