'use client';

/**
 * Code — a real editor for the bound workspace.
 *
 * This page used to be a read-only `<pre>` over a flat list of 200 files: you
 * could look at a file and ask the model to change it, but you could not type
 * in it or save it, and the list had no folders, no search and no way to reach
 * a file the backend's cap had skipped.
 *
 * The saving method did not exist either. `code.edit` goes through a model and
 * `code.apply` only replays a reviewed edit, so a change made by hand had
 * nowhere to go; the page needed `code.write`, which the backend now provides
 * and which contains the path before writing.
 *
 * Everything here talks to the backend the same way. Nothing in this file
 * decides what is allowed — it only offers the UI for it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useWS } from '@/lib/useWS';
import CodeMirror, { EditorView, keymap } from '@uiw/react-codemirror';
import { oneDark } from '@codemirror/theme-one-dark';
import { StreamLanguage } from '@codemirror/language';
import { python } from '@codemirror/lang-python';
import { javascript } from '@codemirror/lang-javascript';
import { json } from '@codemirror/lang-json';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { markdown } from '@codemirror/lang-markdown';
import { c, cpp, csharp, java, kotlin, objectiveC, scala, dart } from '@codemirror/legacy-modes/mode/clike';
import { shell } from '@codemirror/legacy-modes/mode/shell';
import { powerShell } from '@codemirror/legacy-modes/mode/powershell';
import { dockerFile } from '@codemirror/legacy-modes/mode/dockerfile';
import { rust } from '@codemirror/legacy-modes/mode/rust';
import { go } from '@codemirror/legacy-modes/mode/go';
import { ruby } from '@codemirror/legacy-modes/mode/ruby';
import { lua } from '@codemirror/legacy-modes/mode/lua';
import { swift } from '@codemirror/legacy-modes/mode/swift';
import { r } from '@codemirror/legacy-modes/mode/r';
import { standardSQL } from '@codemirror/legacy-modes/mode/sql';
import { yaml } from '@codemirror/legacy-modes/mode/yaml';
import { toml } from '@codemirror/legacy-modes/mode/toml';
import { properties } from '@codemirror/legacy-modes/mode/properties';
import { xml } from '@codemirror/legacy-modes/mode/xml';

type CodeFile = { name: string; path: string; language: string; size: number };
type Tab = {
  path: string;
  name: string;
  language: string;
  content: string;
  saved: string;
  isNew: boolean;
};
type GrepHit = { filePath: string; line: number; text: string };
type Pending = { editId: string; filePath: string; diff: any };

// ---------------------------------------------------------------------------
// Syntax highlighting
// ---------------------------------------------------------------------------

/**
 * `backend/code/lang_detect.py` names languages for itself (`cpp`, `csharp`,
 * `shell`), which is not the vocabulary CodeMirror uses, so the two are mapped
 * rather than passed through. Anything unmapped stays plain text, which is
 * honest — a missing mode must not throw and take the page with it.
 */
function extensionsFor(language: string) {
  switch (language) {
    case 'python': return [python()];
    case 'javascript': return [javascript()];
    case 'typescript': return [javascript({ typescript: true })];
    case 'jsx': return [javascript({ jsx: true })];
    case 'tsx': return [javascript({ jsx: true, typescript: true })];
    case 'json': return [json()];
    case 'markdown': return [markdown()];
    case 'html': case 'vue': case 'svelte': return [html()];
    // A .php file is mostly markup with PHP islands, so html reads truer than
    // plain text until a PHP mode is worth the weight.
    case 'php': return [html()];
    case 'css': case 'scss': case 'less': return [css()];
    case 'xml': case 'svg': return [StreamLanguage.define(xml)];
    case 'c': return [StreamLanguage.define(c)];
    case 'cpp': case 'hpp': case 'cc': case 'hxx': return [StreamLanguage.define(cpp)];
    case 'csharp': return [StreamLanguage.define(csharp)];
    case 'java': return [StreamLanguage.define(java)];
    case 'kotlin': return [StreamLanguage.define(kotlin)];
    case 'scala': return [StreamLanguage.define(scala)];
    case 'dart': return [StreamLanguage.define(dart)];
    case 'objectivec': case 'objectivecpp': return [StreamLanguage.define(objectiveC)];
    case 'rust': return [StreamLanguage.define(rust)];
    case 'go': return [StreamLanguage.define(go)];
    case 'ruby': return [StreamLanguage.define(ruby)];
    case 'lua': return [StreamLanguage.define(lua)];
    case 'swift': return [StreamLanguage.define(swift)];
    case 'r': return [StreamLanguage.define(r)];
    case 'sql': case 'plsql': case 'mysql': case 'postgresql':
      return [StreamLanguage.define(standardSQL)];
    case 'yaml': case 'yml': return [StreamLanguage.define(yaml)];
    case 'toml': return [StreamLanguage.define(toml)];
    case 'ini': case 'cfg': case 'env': case 'conf':
      return [StreamLanguage.define(properties)];
    case 'dockerfile': return [StreamLanguage.define(dockerFile)];
    case 'shell': case 'bash': case 'zsh': return [StreamLanguage.define(shell)];
    case 'powershell': case 'ps1': return [StreamLanguage.define(powerShell)];
    default: return [];
  }
}

// The dashboard is dark already; oneDark supplies the token colours and this
// overrides the chrome so the editor sits on the same #0d1117 as the rest of
// the page instead of oneDark's own grey.
const chrome = EditorView.theme({
  '&': { backgroundColor: '#0d1117', color: '#e8eaed', height: '100%' },
  '.cm-scroller': {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: '12.5px',
    lineHeight: '1.6',
  },
  '.cm-gutters': { backgroundColor: '#0d1117', color: '#484f58', border: 'none' },
  '.cm-activeLine': { backgroundColor: '#161b2255' },
  '.cm-activeLineGutter': { backgroundColor: '#161b22', color: '#8b949e' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: '#58a6ff' },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': {
    backgroundColor: '#1f6feb55',
  },
  '.cm-panels': { backgroundColor: '#161b22', color: '#e8eaed', borderColor: '#30363d' },
  '.cm-panel input, .cm-panel button': {
    backgroundColor: '#0d1117', color: '#e8eaed',
    border: '1px solid #30363d', borderRadius: '4px', padding: '2px 6px',
  },
  '.cm-searchMatch': { backgroundColor: '#f2cc6022', outline: '1px solid #f2cc6044' },
  '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: '#3380ff55' },
  '.cm-tooltip': { backgroundColor: '#161b22', border: '1px solid #30363d' },
}, { dark: true });

// ---------------------------------------------------------------------------
// File tree
// ---------------------------------------------------------------------------

type TreeNode = {
  name: string; path: string; type: 'dir' | 'file';
  language?: string; size?: number; children?: TreeNode[];
};

/** The backend sends a flat list of `a/b/c.ts` paths; the page shows folders. */
function buildTree(files: CodeFile[]): TreeNode[] {
  const root: TreeNode[] = [];
  const dirs = new Map<string, TreeNode>();
  for (const file of [...files].sort((a, b) => a.path.localeCompare(b.path))) {
    const parts = file.path.split('/').filter(Boolean);
    if (!parts.length) continue;
    let level = root;
    let walked = '';
    for (let i = 0; i < parts.length - 1; i++) {
      walked = walked ? `${walked}/${parts[i]}` : parts[i];
      let dir = dirs.get(walked);
      if (!dir) {
        dir = { name: parts[i], path: walked, type: 'dir', children: [] };
        dirs.set(walked, dir);
        level.push(dir);
      }
      level = dir.children!;
    }
    level.push({
      name: parts[parts.length - 1], path: file.path, type: 'file',
      language: file.language, size: file.size,
    });
  }
  // Folders before files, each alphabetically — the order a file explorer uses.
  const sort = (nodes: TreeNode[]): TreeNode[] => {
    nodes.sort((a, b) =>
      a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'dir' ? -1 : 1);
    nodes.forEach(n => n.children && sort(n.children));
    return nodes;
  };
  return sort(root);
}

const LANGUAGE_COLOR: Record<string, string> = {
  typescript: '#3178c6', javascript: '#f7df1e', python: '#3572A5', rust: '#dea584',
  go: '#00ADD8', java: '#b07219', csharp: '#178600', cpp: '#f34b7d', c: '#555555',
  html: '#e34c26', css: '#563d7c', json: '#8b949e', markdown: '#083fa1',
  ruby: '#701516', php: '#4F5D95', swift: '#F05138', kotlin: '#A97BFF',
  shell: '#89e051', sql: '#e38c00', yaml: '#cb171e', xml: '#0060ac',
};
const langColor = (lang: string) => LANGUAGE_COLOR[lang] || '#484f58';

// ---------------------------------------------------------------------------

export default function CodePage() {
  const { state: wsState, send } = useWS();

  const [workspacePath, setWorkspacePath] = useState('');
  const [bound, setBound] = useState(false);
  const [binding, setBinding] = useState(false);
  const [bindError, setBindError] = useState('');
  const [showAll, setShowAll] = useState(false);

  const [files, setFiles] = useState<CodeFile[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [filter, setFilter] = useState('');

  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activePath, setActivePath] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  // The AI flow: instruction -> proposed diff -> review -> apply.
  const [panel, setPanel] = useState<'ai' | 'search'>('ai');
  const [instruction, setInstruction] = useState('');
  const [pending, setPending] = useState<Pending | null>(null);
  const [applied, setApplied] = useState('');

  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<GrepHit[]>([]);
  const [scanned, setScanned] = useState<number | null>(null);

  const [newFile, setNewFile] = useState('');
  const [cursor, setCursor] = useState('Ln 1, Col 1');
  const cursorRef = useRef('Ln 1, Col 1');
  const gotoLine = useRef<number | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const prefilled = useRef(false);
  const touched = useRef(false);
  // Read by the Ctrl+S keymap, which is built once and so must not close over a
  // stale handler.
  const saveRef = useRef<() => void>(() => {});

  const activeTab = tabs.find(t => t.path === activePath) || null;
  const dirty = tabs.some(t => t.content !== t.saved);

  // ---- binding ------------------------------------------------------------

  const bind = useCallback(async (folder: string, everything = false) => {
    setBinding(true); setBindError('');
    try {
      const r = await send('code.bind', { folderPath: folder, includeIgnored: everything });
      if (r?.error) {
        setBindError(r.error);
        setBound(false);
      } else {
        setFiles(r?.files || []);
        setTruncated(Boolean(r?.truncated));
        setBound(true);
      }
      return r;
    } catch (e: any) {
      setBindError(e?.message || 'Could not read that folder.');
      setBound(false);
    } finally {
      setBinding(false);
    }
  }, [send]);

  // Open whatever folder Addled is already configured to work in, so the page is
  // useful without retyping a path the user has already given.
  useEffect(() => {
    if (wsState !== 'connected' || prefilled.current) return;
    prefilled.current = true;
    send('settings.get', { section: 'workspace' })
      .then(r => {
        const root = r?.settings?.workspace?.root || '';
        if (!root || touched.current) return;
        setWorkspacePath(root);
        bind(root);
      })
      .catch(() => { /* the user can bind by hand */ });
  }, [wsState, send, bind]);

  const rebind = useCallback(async (everything = showAll) => {
    if (!workspacePath.trim()) return;
    const r = await bind(workspacePath.trim(), everything);
    if (r && !r.error) {
      // Drop tabs for files that are no longer in the workspace.
      const present = new Set((r.files || []).map((f: CodeFile) => f.path));
      setTabs(prev => prev.filter(t => t.isNew || present.has(t.path)));
    }
  }, [bind, workspacePath, showAll]);

  // ---- opening and saving -------------------------------------------------

  const open = useCallback(async (path: string, line?: number) => {
    gotoLine.current = line ?? null;
    if (tabs.some(t => t.path === path)) { setActivePath(path); return; }
    setBusy(`Opening ${path}`); setError('');
    try {
      const r = await send('code.read', { workspaceId: workspacePath, filePath: path });
      const content = String(r?.content ?? '');
      if (content.startsWith('// Refused:') || content.startsWith('// Error:')) {
        setError(content.replace(/^\/\/\s*/, ''));
        return;
      }
      const name = path.split('/').pop() || path;
      setTabs(prev => [...prev, {
        path, name, language: r?.language || 'text',
        content, saved: content, isNew: false,
      }]);
      setActivePath(path);
      setPending(null);
    } catch (e: any) {
      setError(e?.message || 'Could not open that file.');
    } finally { setBusy(''); }
  }, [send, tabs, workspacePath]);

  const save = useCallback(async (path: string) => {
    const tab = tabs.find(t => t.path === path);
    if (!tab) return;
    if (tab.content === tab.saved && !tab.isNew) { setNotice('No changes to save.'); return; }
    setBusy(`Saving ${tab.name}`); setError(''); setNotice('');
    try {
      const r = await send('code.write', {
        workspaceId: workspacePath, filePath: path, content: tab.content,
      });
      if (!r?.success) {
        setError(r?.error || 'The save was refused.');
        return;
      }
      setTabs(prev => prev.map(t => t.path === path
        ? { ...t, saved: t.content, isNew: false } : t));
      setNotice(r.created ? `Created ${tab.name}` : `Saved ${tab.name}`);
      // A brand-new file is not in the listing the tree was built from.
      if (r.created) rebind();
    } catch (e: any) {
      setError(e?.message || 'The save failed.');
    } finally { setBusy(''); }
  }, [send, tabs, workspacePath, rebind]);

  saveRef.current = () => { if (activePath) save(activePath); };

  const closeTab = useCallback((path: string) => {
    const tab = tabs.find(t => t.path === path);
    if (tab && tab.content !== tab.saved) {
      if (!window.confirm(`Discard unsaved changes to ${tab.name}?`)) return;
    }
    setTabs(prev => {
      const next = prev.filter(t => t.path !== path);
      if (path === activePath) setActivePath(next.length ? next[next.length - 1].path : '');
      return next;
    });
    setPending(null);
  }, [tabs, activePath]);

  const edit = useCallback((path: string, content: string) => {
    setTabs(prev => prev.map(t => t.path === path ? { ...t, content } : t));
  }, []);

  // ---- asking the model for a change --------------------------------------

  /**
   * `code.edit` diffs the model's answer against the file *on disk*, so unsaved
   * editor text would make the diff describe a file the user is not looking at.
   * The tab is saved first rather than silently diffing something else.
   */
  const askEdit = useCallback(async () => {
    if (!activeTab || !instruction.trim() || wsState !== 'connected') return;
    setBusy('Asking the model'); setError(''); setApplied(''); setPending(null);
    try {
      if (activeTab.content !== activeTab.saved) await save(activeTab.path);
      const r = await send('code.edit', {
        workspaceId: workspacePath,
        filePath: activeTab.path,
        instruction: instruction.trim(),
      });
      if (r?.status === 'refused') {
        setError(r?.message || 'That file is outside the workspace.');
      } else if (r?.editId && r?.diffs?.length) {
        setPending({ editId: r.editId, filePath: activeTab.path, diff: r.diffs[0] });
        setInstruction('');
      } else {
        setError(r?.message || r?.diffs?.[0]?.message || 'The model returned no change.');
      }
    } catch (e: any) {
      setError(e?.message || 'The edit failed.');
    } finally { setBusy(''); }
  }, [activeTab, instruction, send, save, workspacePath, wsState]);

  const applyEdit = useCallback(async () => {
    if (!pending) return;
    setBusy('Applying'); setError('');
    try {
      const r = await send('code.apply', {
        workspaceId: workspacePath, filePath: pending.filePath, editId: pending.editId,
      });
      if (!r?.success) { setError(r?.error || 'The change was refused.'); return; }
      setApplied(r.created ? 'Applied — the file was created.'
        : `Applied${r.backup ? ` — previous version kept at ${r.backup}` : ''}`);
      const fresh = await send('code.read', {
        workspaceId: workspacePath, filePath: pending.filePath,
      });
      const content = String(fresh?.content ?? '');
      setTabs(prev => prev.map(t => t.path === pending.filePath
        ? { ...t, content, saved: content, isNew: false } : t));
      setPending(null);
    } catch (e: any) {
      setError(e?.message || 'The change could not be applied.');
    } finally { setBusy(''); }
  }, [pending, send, workspacePath]);

  // ---- searching the workspace -------------------------------------------

  const search = useCallback(async () => {
    if (query.trim().length < 2 || wsState !== 'connected') return;
    setBusy('Searching'); setError(''); setHits([]); setScanned(null);
    try {
      const r = await send('code.grep', { workspaceId: workspacePath, query: query.trim() });
      if (r?.error) setError(r.error);
      setHits(r?.matches || []);
      setScanned(typeof r?.scanned === 'number' ? r.scanned : null);
    } catch (e: any) {
      setError(e?.message || 'The search failed.');
    } finally { setBusy(''); }
  }, [query, send, workspacePath, wsState]);

  // ---- keyboard -----------------------------------------------------------

  const saveKeys = useMemo(() => keymap.of([
    { key: 'Mod-s', preventDefault: true, run: () => { saveRef.current(); return true; } },
  ]), []);

  const onUpdate = useCallback((vu: any) => {
    const head = vu?.state?.selection?.main?.head;
    if (typeof head !== 'number') return;
    const line = vu.state.doc.lineAt(head);
    const label = `Ln ${line.number}, Col ${head - line.from + 1}`;
    // Only re-render when the position actually changes, not on every keypress.
    if (label !== cursorRef.current) {
      cursorRef.current = label;
      setCursor(label);
    }
  }, []);

  // Jump to the line a search result pointed at.
  useEffect(() => {
    const target = gotoLine.current;
    const view = viewRef.current;
    if (target == null || !view) return;
    const line = Math.max(1, Math.min(target, view.state.doc.lines));
    view.dispatch({
      selection: { anchor: view.state.doc.line(line).from },
      scrollIntoView: true,
    });
    view.focus();
    gotoLine.current = null;
  }, [activePath, tabs.length]);

  // Warn on a reload or a window close while something is unsaved.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) { e.preventDefault(); e.returnValue = ''; }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  const tree = useMemo(() => buildTree(files), [files]);
  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return null;
    return files.filter(f => f.path.toLowerCase().includes(q));
  }, [files, filter]);

  // ---- rendering ----------------------------------------------------------

  const renderNodes = (nodes: TreeNode[], depth: number): ReactNode =>
    nodes.map(node => {
      const pad = { paddingLeft: `${8 + depth * 12}px` };
      if (node.type === 'dir') {
        const isOpen = !collapsed.has(node.path);
        return (
          <div key={node.path}>
            <button
              onClick={() => setCollapsed(prev => {
                const next = new Set(prev);
                if (next.has(node.path)) next.delete(node.path); else next.add(node.path);
                return next;
              })}
              style={pad}
              className="w-full text-left py-[3px] pr-2 text-xs flex items-center gap-1 text-[#8b949e] hover:text-[#e8eaed] hover:bg-[#21262d]"
            >
              <span className="w-3 text-[10px]">{isOpen ? '▾' : '▸'}</span>
              <span className="truncate">{node.name}</span>
            </button>
            {isOpen && node.children && renderNodes(node.children, depth + 1)}
          </div>
        );
      }
      const isActive = node.path === activePath;
      const tab = tabs.find(t => t.path === node.path);
      const isDirty = Boolean(tab && tab.content !== tab.saved);
      return (
        <button
          key={node.path}
          onClick={() => open(node.path)}
          style={pad}
          title={node.path}
          className={`w-full text-left py-[3px] pr-2 text-xs flex items-center gap-2 hover:bg-[#21262d] ${
            isActive ? 'bg-[#1f6feb22] text-[#58a6ff]' : 'text-[#c9d1d9]'
          }`}
        >
          <span className="w-[6px] h-[6px] rounded-full shrink-0"
            style={{ background: langColor(node.language || '') }} />
          <span className="truncate">{node.name}</span>
          {isDirty && <span className="ml-auto text-[#d29922] shrink-0">●</span>}
        </button>
      );
    });

  const startNewFile = () => {
    const rel = newFile.trim().replace(/\\/g, '/').replace(/^\/+/, '');
    if (!rel) return;
    setNewFile('');
    setError('');
    if (tabs.some(t => t.path === rel)) { setActivePath(rel); return; }
    const ext = rel.includes('.') ? rel.split('.').pop()! : 'text';
    setTabs(prev => [...prev, {
      path: rel, name: rel.split('/').pop() || rel, language: ext,
      content: '', saved: '', isNew: true,
    }]);
    setActivePath(rel);
  };

  const status = error ? <span className="text-[#f85149]">{error}</span>
    : busy ? <span className="text-[#8b949e]">{busy}…</span>
    : notice ? <span className="text-[#3fb950]">{notice}</span>
    : <span className="text-[#484f58]">Mod+S saves · Mod+F finds inside the file</span>;

  return (
    <div className="flex h-full">
      {/* ---------------- file tree ---------------- */}
      <div className="w-64 border-r border-[#30363d] flex flex-col shrink-0">
        <div className="p-3 border-b border-[#30363d] space-y-2">
          {!bound ? (
            <>
              <input
                value={workspacePath}
                onChange={e => { touched.current = true; setWorkspacePath(e.target.value); }}
                placeholder="Folder path…"
                className="w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"
              />
              <button
                onClick={() => rebind()}
                disabled={!workspacePath.trim() || binding || wsState !== 'connected'}
                className="w-full bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-2 py-1.5 text-xs font-medium"
              >
                {binding ? 'Binding…' : 'Bind Workspace'}
              </button>
            </>
          ) : (
            <div className="flex items-center gap-2 text-xs">
              <span className="text-[#8b949e] truncate" title={workspacePath}>
                📁 {workspacePath.split(/[\\/]/).filter(Boolean).pop()}
              </span>
              <button onClick={() => rebind()}
                className="ml-auto text-[#8b949e] hover:text-[#58a6ff]" title="Reload the file list">⟳</button>
              <button
                onClick={() => { setBound(false); setFiles([]); setTabs([]); setActivePath(''); }}
                className="text-[#8b949e] hover:text-[#f85149]" title="Unbind">✕</button>
            </div>
          )}
          {bindError && <p className="text-[11px] text-[#f85149]">{bindError}</p>}
        </div>

        {bound && (
          <>
            <div className="p-2 border-b border-[#30363d] space-y-2">
              <input
                value={filter}
                onChange={e => setFilter(e.target.value)}
                placeholder="Filter files…"
                className="w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"
              />
              <div className="flex gap-1">
                <input
                  value={newFile}
                  onChange={e => setNewFile(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') startNewFile(); }}
                  placeholder="new/file.py"
                  className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"
                />
                <button
                  onClick={startNewFile}
                  disabled={!newFile.trim()}
                  className="text-xs px-2 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-40"
                  title="Create a file in the workspace"
                >＋</button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto py-1">
              {matches
                ? matches.map(f => (
                    <button key={f.path} onClick={() => open(f.path)} title={f.path}
                      className={`w-full text-left py-[3px] pr-2 pl-3 text-xs flex items-center gap-2 hover:bg-[#21262d] ${
                        f.path === activePath ? 'bg-[#1f6feb22] text-[#58a6ff]' : 'text-[#c9d1d9]'}`}>
                      <span className="w-[6px] h-[6px] rounded-full shrink-0"
                        style={{ background: langColor(f.language) }} />
                      <span className="truncate">{f.path}</span>
                    </button>
                  ))
                : renderNodes(tree, 0)}
              {!files.length && (
                <p className="px-3 py-2 text-[11px] text-[#484f58]">
                  No files. The folder may be empty, or everything in it is ignored.
                </p>
              )}
            </div>

            <div className="border-t border-[#30363d] px-3 py-2 flex items-center justify-between text-[11px] text-[#484f58]">
              <span>{files.length} file{files.length === 1 ? '' : 's'}{truncated ? '+' : ''}</span>
              <label className="flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox" checked={showAll}
                  onChange={e => { setShowAll(e.target.checked); rebind(e.target.checked); }}
                  className="accent-[#3380FF]"
                />
                show all
              </label>
            </div>
            {truncated && (
              <p className="px-3 pb-2 text-[11px] text-[#d29922]">
                The list was capped — turn on “show all”, or filter to reach the rest.
              </p>
            )}
          </>
        )}
      </div>

      {/* ---------------- editor ---------------- */}
      <div className="flex-1 flex flex-col min-w-0">
        {tabs.length > 0 && (
          <div className="flex items-stretch border-b border-[#30363d] bg-[#161b22] overflow-x-auto">
            {tabs.map(tab => (
              <div
                key={tab.path}
                className={`group flex items-center gap-2 px-3 py-2 text-xs border-r border-[#30363d] cursor-pointer whitespace-nowrap ${
                  tab.path === activePath ? 'bg-[#0d1117] text-[#e8eaed]' : 'text-[#8b949e] hover:bg-[#21262d]'}`}
                onClick={() => { setActivePath(tab.path); setPending(null); }}
                title={tab.path}
              >
                <span className="w-[6px] h-[6px] rounded-full shrink-0"
                  style={{ background: langColor(tab.language) }} />
                <span>{tab.name}</span>
                {tab.content !== tab.saved && <span className="text-[#d29922]">●</span>}
                <button
                  onClick={e => { e.stopPropagation(); closeTab(tab.path); }}
                  className="opacity-0 group-hover:opacity-100 text-[#8b949e] hover:text-[#f85149]"
                >✕</button>
              </div>
            ))}
          </div>
        )}

        {activeTab ? (
          <>
            <div className="flex items-center gap-3 px-4 py-2 border-b border-[#30363d] bg-[#161b22] text-xs shrink-0">
              <span className="text-[#e8eaed] truncate">{activeTab.path}</span>
              <span className="text-[#8b949e]">{activeTab.language}</span>
              {activeTab.isNew && <span className="text-[#d29922]">not saved yet</span>}
              <div className="ml-auto flex items-center gap-2">
                {dirty && <span className="text-[#d29922]">unsaved</span>}
                <button
                  onClick={() => save(activeTab.path)}
                  disabled={!dirty && !activeTab.isNew}
                  className="bg-[#238636] hover:bg-[#2ea043] disabled:opacity-40 text-white rounded px-2.5 py-1 font-medium"
                >Save</button>
              </div>
            </div>

            <div className="flex-1 min-h-0">
              <CodeMirror
                key={activeTab.path}
                value={activeTab.content}
                height="100%"
                theme="none"
                extensions={[...extensionsFor(activeTab.language), oneDark, chrome, saveKeys]}
                onChange={value => edit(activeTab.path, value)}
                onUpdate={onUpdate}
                onCreateEditor={view => { viewRef.current = view; }}
                basicSetup={{
                  lineNumbers: true, foldGutter: true, highlightActiveLine: true,
                  bracketMatching: true, closeBrackets: true, autocompletion: true,
                  searchKeymap: true, history: true, indentOnInput: true,
                }}
              />
            </div>

            {/* proposed change */}
            {pending && (
              <div className="border-t border-[#30363d] bg-[#161b22] max-h-64 overflow-y-auto shrink-0">
                <div className="flex items-center justify-between px-3 py-2 border-b border-[#21262d] sticky top-0 bg-[#161b22]">
                  <span className="text-xs text-[#8b949e]">
                    Proposed change to {pending.filePath}
                    <span className="text-[#3fb950]"> +{pending.diff?.added ?? 0}</span>
                    <span className="text-[#f85149]"> −{pending.diff?.removed ?? 0}</span>
                  </span>
                  <div className="flex gap-2">
                    <button onClick={applyEdit} disabled={Boolean(busy)}
                      className="text-xs px-2 py-1 rounded bg-[#238636] hover:bg-[#2ea043] disabled:opacity-50 text-white">
                      Apply
                    </button>
                    <button onClick={() => { setPending(null); setApplied(''); }}
                      className="text-xs px-2 py-1 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed]">
                      Discard
                    </button>
                  </div>
                </div>
                <pre className="p-3 text-xs font-mono whitespace-pre-wrap">
                  {String(pending.diff?.raw_diff || '')
                    .split('\n').filter(Boolean).map((line, i) => (
                      <div key={i} className={
                        line.startsWith('+') && !line.startsWith('+++') ? 'text-[#3fb950]'
                        : line.startsWith('-') && !line.startsWith('---') ? 'text-[#f85149]'
                        : line.startsWith('@@') ? 'text-[#58a6ff]' : 'text-[#8b949e]'}>{line}</div>
                    ))}
                </pre>
              </div>
            )}

            {/* ask / search */}
            <div className="border-t border-[#30363d] shrink-0">
              <div className="flex items-center gap-1 px-3 pt-2 text-xs">
                <button
                  onClick={() => setPanel('ai')}
                  className={`px-2 py-1 rounded-t ${panel === 'ai' ? 'bg-[#21262d] text-[#e8eaed]' : 'text-[#8b949e]'}`}
                >Ask Addled</button>
                <button
                  onClick={() => setPanel('search')}
                  className={`px-2 py-1 rounded-t ${panel === 'search' ? 'bg-[#21262d] text-[#e8eaed]' : 'text-[#8b949e]'}`}
                >Search{scanned !== null && hits.length ? ` (${hits.length})` : ''}</button>
              </div>

              {panel === 'ai' ? (
                <div className="p-3 flex gap-2">
                  <input
                    value={instruction}
                    onChange={e => setInstruction(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') askEdit(); }}
                    placeholder={`Ask Addled to change ${activeTab.name}…`}
                    className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"
                  />
                  <button
                    onClick={askEdit}
                    disabled={!instruction.trim() || wsState !== 'connected' || Boolean(busy)}
                    className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1.5 text-xs font-medium"
                  >Edit</button>
                </div>
              ) : (
                <div className="p-3 space-y-2">
                  <div className="flex gap-2">
                    <input
                      value={query}
                      onChange={e => setQuery(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') search(); }}
                      placeholder="Find in the workspace…"
                      className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"
                    />
                    <button
                      onClick={search}
                      disabled={query.trim().length < 2 || Boolean(busy)}
                      className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1.5 text-xs font-medium"
                    >Search</button>
                  </div>
                  {scanned !== null && (
                    <p className="text-[11px] text-[#484f58]">
                      {hits.length} match{hits.length === 1 ? '' : 'es'} in {scanned} file{scanned === 1 ? '' : 's'}
                    </p>
                  )}
                  {hits.length > 0 && (
                    <div className="max-h-40 overflow-y-auto border border-[#30363d] rounded">
                      {hits.map((hit, i) => (
                        <button
                          key={`${hit.filePath}:${hit.line}:${i}`}
                          onClick={() => open(hit.filePath, hit.line)}
                          className="w-full text-left px-2 py-1 text-[11px] hover:bg-[#21262d] border-b border-[#21262d] last:border-0"
                        >
                          <span className="text-[#58a6ff]">{hit.filePath}:{hit.line}</span>
                          <span className="text-[#8b949e] ml-2 font-mono">{hit.text}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-[#8b949e]">
            <div className="text-center">
              <span className="text-4xl mb-3 block">💻</span>
              <p className="text-sm">
                {bound ? 'Open a file from the tree to start editing'
                  : 'Bind a workspace to start coding'}
              </p>
              {bound && (
                <p className="text-[11px] text-[#484f58] mt-2">
                  Or type a path under “new/file.py” to write a new one.
                </p>
              )}
            </div>
          </div>
        )}

        <div className="flex items-center gap-4 px-3 py-1.5 border-t border-[#30363d] text-[11px] shrink-0">
          <span className="min-w-0 truncate">{status}</span>
          {applied && <span className="text-[#3fb950] ml-auto shrink-0">{applied}</span>}
          {activeTab && <span className="text-[#8b949e] ml-auto shrink-0">{cursor}</span>}
        </div>
      </div>
    </div>
  );
}
