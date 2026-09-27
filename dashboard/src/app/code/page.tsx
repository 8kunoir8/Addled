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
// A right-click anywhere in the tree. `node` is the folder or file it landed on.
type TreeMenu = { x: number; y: number; node: TreeNode };
// One step of a plan: which file, and what to do to it.
type PlanStep = {
  filePath: string;
  action: string;
  instruction: string;
  reason: string;
  status?: 'pending' | 'running' | 'done' | 'failed' | 'applied' | 'skipped';
  message?: string;
  // Set once a diff exists for this step, so "apply all" applies exactly what
  // was generated and reviewed rather than asking for it again.
  editId?: string;
  diff?: any;
};
type Plan = {
  summary: string;
  steps: PlanStep[];
  uncertain?: { question: string }[];
};
// A generated-but-not-yet-written diff, held for the bulk apply.
type Prepared = { filePath: string; editId: string; diff: any };

// ---------------------------------------------------------------------------
// Syntax highlighting
// ---------------------------------------------------------------------------

/**
 * `backend/codemode/lang_detect.py` names languages for itself (`cpp`, `csharp`,
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
    // `overflow: auto` on the scroller is what gives the editor its scrollbars.
    // Without it a long file could not be scrolled at all, and `.cm-content`
    // defaulted to `min-width: max-content`, so long lines ran past the edge
    // with nowhere to go.
    overflow: 'auto',
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

/**
 * How many files a plan may name before the UI suggests the request was read
 * too loosely. A warning, not a limit: a real refactor can be wide, and refusing
 * it would make the feature useless for exactly the case planning was added for.
 */
const MANY_FILES = 8;

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
  // Folders the backend left out because they look generated or are dotted.
  // Shown in the tree so a partial listing does not read as a broken explorer.
  const [hiddenDirs, setHiddenDirs] = useState<string[]>([]);
  const [filter, setFilter] = useState('');

  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activePath, setActivePath] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [treeOpen, setTreeOpen] = useState(true);
  const treeToggled = useRef(false);

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

  // Right-click menu in the tree, and the folders the user pinned as extra
  // context for the planner. Pinned folders are a hint, not a scope change.
  const [menu, setMenu] = useState<TreeMenu | null>(null);
  const [aiContextFolders, setAiContextFolders] = useState<string[]>([]);

  // The plan-first flow: instruction -> plan (files it will touch) -> per-file
  // diffs the user applies one at a time.
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planning, setPlanning] = useState(false);
  // Diffs generated for the bulk flow but not yet written. Empty means "apply
  // all" is not offered — the button appears only after the diffs exist.
  const [prepared, setPrepared] = useState<Prepared[]>([]);
  // How many diffs the backend says may be in flight at once. 1 means the
  // provider serves one generation at a time (the local model does), so asking
  // for several would queue and look slow rather than fast.
  const [planConcurrency, setPlanConcurrency] = useState(1);
  // Read by the diff loop between steps. A ref, not state: the loop is async and
  // must see the current value without re-creating itself mid-run.
  const cancelRef = useRef(false);

  /**
   * Whether this page was served by the remote gateway.
   *
   * Binding a workspace is in `REMOTE_FORBIDDEN_METHODS` on the backend, so the
   * menu must not offer it to a remote session. The gateway is also the only
   * thing that injects `__ADDLED_WS_URL__` (see useWS.wsUrl), so its presence is
   * the check — no extra request, and no way for the UI to disagree with the
   * socket it is actually using.
   */
  const isRemote = typeof window !== 'undefined'
    && typeof (globalThis as any).__ADDLED_WS_URL__ === 'string'
    && Boolean((globalThis as any).__ADDLED_WS_URL__);

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
        const resolved = r?.workspaceId || folder;
        setWorkspacePath(resolved);
        setFiles(r?.files || []);
        setTruncated(Boolean(r?.truncated));
        setHiddenDirs(Array.isArray(r?.hiddenDirs) ? r.hiddenDirs : []);
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

  // ---- tree context menu --------------------------------------------------

  /** Open the menu at the pointer, clamped so it cannot fall off the window. */
  const openMenu = useCallback((e: React.MouseEvent, node: TreeNode) => {
    e.preventDefault();
    e.stopPropagation();
    const W = 230, H = 96;
    setMenu({
      x: Math.min(e.clientX, window.innerWidth - W - 8),
      y: Math.min(e.clientY, window.innerHeight - H - 8),
      node,
    });
  }, []);

  /**
   * Rebind the editor to a folder picked in the tree.
   *
   * This is deliberately the SAME call the "Bind Workspace" button makes.
   * `code.bind` already writes the global workspace root and the session's
   * active workspace (backend/ws_server.py:code_bind), and that root is what
   * every chat prompt is told about — so there is no narrower "code page only"
   * binding to offer here. Saying otherwise would be a lie in the UI.
   */
  const useAsWorkspace = useCallback(async (folder: string) => {
    setMenu(null);
    const r = await bind(folder, showAll);
    if (r && !r.error) {
      // Drop tabs for files that are no longer in the (new) workspace.
      const present = new Set((r.files || []).map((f: CodeFile) => f.path));
      setTabs(prev => prev.filter(t => t.isNew || present.has(t.path)));
      setAiContextFolders([]);
      setPlan(null);
      setNotice(`Workspace set to ${folder.split(/[\\/]/).filter(Boolean).pop()}`);
      setTimeout(() => setNotice(''), 3000);
    }
  }, [bind, showAll]);

  const addAiContext = useCallback((folder: string) => {
    setMenu(null);
    setAiContextFolders(prev =>
      prev.includes(folder) ? prev : [...prev, folder]);
  }, []);

  // A menu that survives a click elsewhere is a bug; so is one that survives a
  // scroll, because it is positioned in viewport coordinates.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(null); };
    window.addEventListener('click', close);
    window.addEventListener('resize', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('resize', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', onKey);
    };
  }, [menu]);

  // ---- plan-first editing -------------------------------------------------

  /**
   * Step one of the edit flow: ask for a PLAN, never an edit.
   *
   * The planner is given the workspace and the read-only tools, and its job is
   * to find which files the change actually touches before anything is
   * rewritten. Asking for prose here instead of a plan is what produced edits
   * against the wrong file, so a plan is required rather than optional.
   */
  const makePlan = useCallback(async () => {
    const ask = instruction.trim();
    if (!ask || wsState !== 'connected' || !bound) return;
    // A cancel from the previous plan must not stop this one.
    cancelRef.current = false;
    setPlanning(true); setBusy('Planning'); setError(''); setPlan(null); setApplied('');
    // A new plan invalidates diffs generated for the old one; leaving them would
    // let "apply all" write files the current plan never mentioned.
    setPrepared([]); setPending(null);
    try {
      const r = await send('code.plan', {
        workspaceId: workspacePath,
        instruction: ask,
        contextFolders: aiContextFolders,
      });
      if (r?.status === 'refused') {
        setError(r?.message || 'That is outside the workspace.');
      } else if (r?.plan?.steps?.length) {
        setPlan({
          summary: r.plan.summary || '',
          steps: r.plan.steps.map((s: any) => ({ ...s, status: 'pending' })),
          uncertain: r.plan.uncertain || [],
        });
        // The backend decides this, not the page: it is the one that knows
        // whether the provider queues. Default 1 so a missing field is treated
        // as "slow but safe" rather than "fire them all".
        setPlanConcurrency(Math.max(1, Number(r?.concurrency) || 1));
      } else {
        setError(r?.message || 'The planner found nothing to change.');
      }
    } catch (e: any) {
      setError(e?.message || 'Planning failed.');
    } finally { setPlanning(false); setBusy(''); }
  }, [instruction, send, workspacePath, wsState, bound, aiContextFolders]);

  /**
   * Step two: turn the plan into reviewable diffs, one file at a time.
   *
   * Only the first step is generated here — the user reviews it before the next
   * file is touched, so a plan is never applied blind in one sweep.
   */
  const runPlanStep = useCallback(async (index: number) => {
    const step = plan?.steps[index];
    if (!step || !plan) return;
    const mark = (patch: Partial<PlanStep>) =>
      setPlan(prev => prev ? {
        ...prev,
        steps: prev.steps.map((s, i) => i === index ? { ...s, ...patch } : s),
      } : prev);
    mark({ status: 'running' });
    setBusy(`Editing ${step.filePath}`); setError('');
    try {
      // The edit diffs against the file ON DISK, so an unsaved tab would make
      // the diff describe a file the user is not looking at.
      const openTab = tabs.find(t => t.path === step.filePath);
      if (openTab && openTab.content !== openTab.saved) await save(openTab.path);

      const r = await send('code.edit', {
        workspaceId: workspacePath,
        filePath: step.filePath,
        instruction: step.instruction,
      });
      if (r?.status === 'refused') {
        mark({ status: 'failed', message: r?.message || 'Outside the workspace.' });
        setError(r?.message || 'That file is outside the workspace.');
      } else if (r?.editId && r?.diffs?.length) {
        // Record the id on the step as well, so the bulk flow can reuse a diff
        // that was already generated and reviewed instead of asking again.
        mark({ status: 'done', editId: r.editId, diff: r.diffs[0],
               message: 'Diff ready to review' });
        setPrepared(prev => prev.some(p => p.filePath === step.filePath)
          ? prev
          : [...prev, { filePath: step.filePath, editId: r.editId, diff: r.diffs[0] }]);
        setPending({ editId: r.editId, filePath: step.filePath, diff: r.diffs[0] });
      } else {
        mark({ status: 'failed',
               message: r?.message || r?.diffs?.[0]?.message || 'No change proposed.' });
      }
    } catch (e: any) {
      mark({ status: 'failed', message: e?.message || 'The edit failed.' });
    } finally { setBusy(''); }
  }, [plan, send, workspacePath, tabs, save]);

  const cancelPlan = useCallback(() => {
    setPlan(null); setPending(null); setApplied(''); setError('');
  }, []);

  /**
   * Generate diffs for the plan, without writing anything.
   *
   * Three things shape this, and the second is the one that is easy to get
   * wrong:
   *
   * 1. CANCEL. Each step checks `cancelRef` before starting and after
   *    finishing, so the button responds between steps. A step already in
   *    flight is not aborted — `code.edit` writes nothing, it only stages a
   *    pending edit, so letting it finish costs time and leaves one unused
   *    entry rather than corrupting anything.
   *
   * 2. WIDTH FROM THE PROVIDER, not from here. `code.plan` reports how many
   *    diffs may be in flight at once. The local model answers 1 because it
   *    serves ONE generation at a time and queues the rest — asking for three
   *    there would look concurrent and behave serially, which is worse than
   *    saying so. A cloud provider answers up to 3.
   *
   * 3. ONLY WHAT WAS ASKED. Diffs are the expensive part on a slow model, so
   *    this never runs unprompted: the caller decides, and for a wide plan the
   *    UI does not offer the bulk path at all.
   */
  const generateAllDiffs = useCallback(async () => {
    if (!plan || !plan.steps.length) return;
    const steps = plan.steps;
    const total = steps.length;
    const width = Math.max(1, planConcurrency || 1);

    cancelRef.current = false;
    setBusy(width > 1
      ? `Generating diffs (${width} at a time)`
      : 'Generating diffs (one at a time)');
    setError('');
    const collected: Prepared[] = [];

    const mark = (index: number, patch: Partial<PlanStep>) =>
      setPlan(prev => prev ? {
        ...prev,
        steps: prev.steps.map((s, j) => j === index ? { ...s, ...patch } : s),
      } : prev);

    const doOne = async (index: number) => {
      if (cancelRef.current) return;
      const step = steps[index];
      if (step.status === 'done' && step.editId) {
        collected.push({ filePath: step.filePath, editId: step.editId,
                         diff: step.diff });
        return;
      }
      mark(index, { status: 'running' });
      try {
        // The edit diffs against the file ON DISK, so an unsaved tab would
        // make the diff describe something the user is not looking at.
        const openTab = tabs.find(t => t.path === step.filePath);
        if (openTab && openTab.content !== openTab.saved) await save(openTab.path);
        const r = await send('code.edit', {
          workspaceId: workspacePath,
          filePath: step.filePath,
          instruction: step.instruction,
        });
        if (cancelRef.current) {
          // Cancelled while this was in flight: the diff is real but was not
          // asked for, so it is not offered for applying. Marking it 'pending'
          // rather than 'done' keeps that honest.
          mark(index, { status: 'pending', message: 'Cancelled' });
          return;
        }
        if (r?.editId && r?.diffs?.length) {
          const diff = r.diffs[0];
          mark(index, { status: 'done', editId: r.editId, diff,
                        message: 'Diff ready to review' });
          collected.push({ filePath: step.filePath, editId: r.editId, diff });
        } else {
          mark(index, { status: 'failed',
                        message: r?.message || r?.diffs?.[0]?.message
                                 || 'No change proposed.' });
        }
      } catch (e: any) {
        mark(index, { status: 'failed', message: e?.message || 'The edit failed.' });
      }
    };

    // A bounded pool. With width 1 this is the old serial loop, which is the
    // only honest way to talk to the local model.
    let next = 0;
    const run = async () => {
      while (!cancelRef.current) {
        const index = next++;
        if (index >= total) return;
        setBusy(`Diff ${Math.min(next, total)} of ${total}: ${steps[index].filePath}`);
        await doOne(index);
      }
    };
    await Promise.all(Array.from({ length: Math.min(width, total) }, run));

    setPrepared(collected);
    setBusy('');
    if (cancelRef.current) {
      setNotice(`Cancelled — ${collected.length} diff${collected.length === 1 ? '' : 's'} ready`);
      setTimeout(() => setNotice(''), 4000);
    } else if (!collected.length) {
      setError('No diffs were produced, so there is nothing to apply.');
    }
  }, [plan, send, workspacePath, tabs, save, planConcurrency]);

  /**
   * Write every prepared diff in one call.
   *
   * Only diffs that were generated AND shown can be applied — they are the ids
   * `code.edit` returned, so the review step has already happened. The backend
   * refuses anything it does not find pending, which is what stops this from
   * becoming a way to write files unseen.
   */
  const applyAllDiffs = useCallback(async () => {
    if (!prepared.length) return;
    if (!window.confirm(
      `Apply ${prepared.length} reviewed change`
      + `${prepared.length === 1 ? '' : 's'}? `
      + 'A backup of each file is kept beside it.'
    )) return;
    setBusy('Applying all'); setError(''); setApplied('');
    try {
      const r = await send('code.applyPlan', {
        workspaceId: workspacePath,
        edits: prepared.map(p => ({
          filePath: p.filePath,
          editId: p.editId,
          // A step the planner called "create" writes a file that is not there
          // yet; every other step must already exist.
          create: plan?.steps.find(s => s.filePath === p.filePath)?.action === 'create',
        })),
      });
      const done = r?.applied || [];
      const left = r?.skipped || [];
      if (r?.failed) {
        setError(
          `Stopped at ${r.failed.filePath}: ${r.failed.reason}`
          + (done.length ? ` — ${done.length} file(s) were written first.` : '')
        );
      }
      if (done.length) {
        setApplied(`Applied ${done.length} change${done.length === 1 ? '' : 's'}`
          + (left.length ? `, ${left.length} not attempted.` : '.'));
      }
      // Refresh every file that changed, so the tabs show what is on disk.
      await Promise.all(done.map(async (d: any) => {
        try {
          const fresh = await send('code.read', {
            workspaceId: workspacePath, filePath: d.filePath,
          });
          const content = String(fresh?.content ?? '');
          setTabs(prev => prev.map(t => t.path === d.filePath
            ? { ...t, content, saved: content, isNew: false } : t));
        } catch { /* the file is written; a stale tab is not worth an error */ }
      }));
      // Mark the plan steps so the list reflects reality.
      setPlan(prev => prev ? {
        ...prev,
        steps: prev.steps.map((s, i) => {
          const hit = done.find((d: any) => d.index === i);
          const missed = left.find((d: any) => d.index === i);
          if (hit) return { ...s, status: 'applied', message: 'Written to disk' };
          if (missed) return { ...s, status: 'skipped', message: missed.reason };
          return s;
        }),
      } : prev);
      setPrepared([]);
      setPending(null);
    } catch (e: any) {
      setError(e?.message || 'The changes could not be applied.');
    } finally { setBusy(''); }
  }, [prepared, send, workspacePath, plan]);

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

  /**
   * A 256px tree and an editor need room between them. Below roughly 900px the
   * tree used to win the whole window and the editor was squeezed to zero
   * width, which makes an editor useless. It now hides itself, and the toggle
   * in the tree header overrides this either way.
   */
  useEffect(() => {
    const apply = () => {
      if (!treeToggled.current) setTreeOpen(window.innerWidth >= 900);
    };
    apply();
    window.addEventListener('resize', apply);
    return () => window.removeEventListener('resize', apply);
  }, []);

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
              onContextMenu={e => openMenu(e, node)}
              title={`${node.path}  (right-click for options)`}
              style={pad}
              className="w-full text-left py-[3px] pr-2 text-xs flex items-center gap-1 text-[#8b949e] hover:text-[#e8eaed] hover:bg-[#21262d]"
            >
              <span className="w-3 text-[10px]">{isOpen ? '▾' : '▸'}</span>
              <span className="truncate">{node.name}</span>
              {aiContextFolders.includes(node.path) && (
                <span className="ml-auto shrink-0 text-[10px] text-[#58a6ff]"
                  title="Pinned as AI context">§</span>
              )}
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
          onContextMenu={e => openMenu(e, node)}
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

  /**
   * The composer, shared by both states.
   *
   * Built as a variable rather than duplicated because it must appear whether
   * or not a file is open: planning is about the workspace, so needing an open
   * tab first would be the wrong shape. The `activeTab`-only bits inside it are
   * guarded.
   */
  const askPanel = (
    <div className="border-t border-[#30363d] shrink-0">
      {/*
        A failed bind is stated here, next to the thing it disabled. It used to
        appear only in the tree header, so the visible symptom was "the message
        area is gone" rather than "the folder could not be opened".
      */}
      {bindError && (
        <p className="px-3 pt-2 text-[11px] text-[#f85149]">
          {bindError} — pick another folder above, then try again.
        </p>
      )}
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
        <div className="p-3 space-y-2">
          {/* pinned AI context — a hint for the planner, not a scope */}
          {aiContextFolders.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {aiContextFolders.map(folder => (
                <span key={folder}
                  className="inline-flex items-center gap-1 bg-[#1f6feb22] border border-[#1f6feb55] rounded px-2 py-0.5 text-[10px] text-[#58a6ff]"
                  title={folder}>
                  § {folder.split(/[\\/]/).filter(Boolean).pop()}
                  <button
                    onClick={() => setAiContextFolders(prev => prev.filter(f => f !== folder))}
                    className="text-[#8b949e] hover:text-[#f85149]"
                    title="Remove from AI context">×</button>
                </span>
              ))}
            </div>
          )}

          {/* the requirement: always plan before editing */}
          <div className="flex gap-2 items-start">
            <textarea
              value={instruction}
              onChange={e => setInstruction(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); makePlan(); }
              }}
              rows={2}
              placeholder={bound
                ? 'Describe the change — Addled plans which files to touch first…'
                : 'Bind a workspace first…'}
              className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-xs text-[#e8eaed] placeholder-[#484f58] resize-none focus:outline-none focus:border-[#3380FF]"
            />
            <button
              onClick={makePlan}
              disabled={!instruction.trim() || wsState !== 'connected' || !bound || planning}
              className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1.5 text-xs font-medium shrink-0"
              title="Plan first — nothing is written until you approve a diff"
            >{planning ? '…' : 'Plan'}</button>
            {/*
              Cancel, shown only while a bulk run is in flight. Generating diffs
              for a wide plan on the local model is minutes of waiting, and
              before this there was no way out of it short of reloading.

              It sets a flag rather than aborting the socket: the loop checks it
              between steps, and a step already running is allowed to finish.
              `code.edit` only stages a pending edit, so letting one complete
              costs time and nothing else.
            */}
            {busy && !planning && plan && (
              <button
                onClick={() => { cancelRef.current = true; setBusy('Cancelling…'); }}
                className="border border-[#30363d] hover:border-[#f85149] hover:text-[#f85149] text-[#8b949e] rounded px-3 py-1.5 text-xs font-medium shrink-0"
                title="Stop after the diff in flight finishes — nothing is written"
              >Stop</button>
            )}
          </div>

          {/* single file, one shot: the older, still-useful path */}
          {activeTab && (
            <button
              onClick={askEdit}
              disabled={!instruction.trim() || wsState !== 'connected' || Boolean(busy)}
              className="text-[10px] text-[#8b949e] hover:text-[#58a6ff] disabled:opacity-40"
              title={`Edit only ${activeTab.name}, skipping the plan`}
            >Or edit just {activeTab.name} →</button>
          )}

          {/*
            What this box can and cannot do.

            It looks like the Chat composer but has a much smaller toolbox, and
            nothing said so: the Code page's tools are read-only on purpose (a
            change is proposed as a diff and applied by the user, so `write_file`
            here would retire the review step), and it has no memory recall or
            chat history. So a request that needs a web search, an MCP tool or a
            second file works in Chat and does not work here — previously with no
            hint as to why.
          */}
          <p className="text-[10px] text-[#484f58]">
            Reads and searches the workspace, and proposes edits as a diff you
            apply. It cannot run other tools — for those, use{' '}
            <a href="/chat" className="text-[#58a6ff] hover:underline">Chat</a>.
          </p>

          {plan && (
            <div className="border border-[#30363d] rounded bg-[#0d1117]">
              <div className="flex items-center justify-between px-2 py-1 border-b border-[#21262d]">
                <span className="text-[10px] text-[#8b949e]">
                  Plan — {plan.steps.length} file{plan.steps.length === 1 ? '' : 's'}
                </span>
                <div className="flex items-center gap-2">
                  {/*
                    Lazy diffs. Generating is the expensive half, so the bulk
                    path is offered only when it is worth the wait: a small plan.
                    A wide one would be minutes of the model's time for diffs
                    that may never be reviewed, so the per-file button is the
                    way in and the hint below says why.
                  */}
                  {plan.steps.length > 1 && plan.steps.length <= MANY_FILES && (
                    <button
                      onClick={generateAllDiffs}
                      disabled={Boolean(busy) || plan.steps.every(s => s.status === 'done')}
                      className="text-[10px] px-1.5 py-0.5 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-40"
                      title={planConcurrency > 1
                        ? `Produce every diff (${planConcurrency} at a time) so the plan can be reviewed at once`
                        : 'Produce every diff one at a time — this model answers one request at a time, so it will take a while'}
                    >Generate all</button>
                  )}
                  {/*
                    "Discard plan", not "Cancel". There is also a Cancel in the
                    composer that stops a run in progress, and two buttons with
                    the same word doing different things is a trap — one throws
                    the plan away, the other stops the work. Named for what they
                    actually do.
                  */}
                  <button onClick={cancelPlan}
                    title="Throw this plan away — nothing is written"
                    className="text-[10px] text-[#8b949e] hover:text-[#f85149]">Discard plan</button>
                </div>
              </div>
              {plan.summary && (
                <p className="px-2 py-1 text-[10px] text-[#8b949e] border-b border-[#21262d]">
                  {plan.summary}
                </p>
              )}
              {/*
                A plan naming many files is more often a misread request than a
                genuinely wide change. Say so rather than letting someone click
                through twenty diffs — but do not block it, because a real
                refactor can legitimately be wide.
              */}
              {plan.steps.length > MANY_FILES && (
                <p className="px-2 py-1.5 text-[10px] text-[#d29922] border-b border-[#21262d]">
                  ⚠ This plan touches {plan.steps.length} files. If that is more
                  than you expected, cancel and describe the change more
                  narrowly — a wide plan usually means the request was read
                  loosely. Diffs are generated one file at a time from the list
                  below, because generating all of them would take a long while.
                </p>
              )}
              {plan.steps.length > 1 && plan.steps.length <= MANY_FILES
                && planConcurrency === 1 && (
                <p className="px-2 py-1.5 text-[10px] text-[#484f58] border-b border-[#21262d]">
                  This model answers one request at a time, so generating all
                  diffs will take a while — cancel at any point.
                </p>
              )}
              <div className="max-h-52 overflow-y-auto">
                {plan.steps.map((step, i) => (
                  <div key={`${step.filePath}:${i}`}
                    className="px-2 py-1.5 border-b border-[#21262d] last:border-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] text-[#e8eaed] truncate font-mono"
                        title={step.filePath}>{step.filePath}</span>
                      <span className={`ml-auto shrink-0 text-[10px] ${
                        step.status === 'applied' ? 'text-[#3fb950]'
                        : step.status === 'done' ? 'text-[#3fb950]'
                        : step.status === 'failed' ? 'text-[#f85149]'
                        : step.status === 'running' ? 'text-[#d29922]'
                        : step.status === 'skipped' ? 'text-[#8b949e]'
                        : 'text-[#484f58]'}`}>
                        {step.status === 'applied' ? '✓ written'
                          : step.status === 'done' ? '✓ diff ready'
                          : step.status === 'failed' ? '✕ failed'
                          : step.status === 'running' ? 'working…'
                          : step.status === 'skipped' ? '– not applied'
                          : step.action || 'edit'}
                      </span>
                      <button
                        onClick={() => runPlanStep(i)}
                        disabled={Boolean(busy) || step.status === 'done'
                                  || step.status === 'applied'}
                        className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-40"
                      >{step.status === 'applied' ? 'Written'
                         : step.status === 'done' ? 'Reviewing'
                         : 'Generate diff'}</button>
                    </div>
                    {step.reason && (
                      <p className="text-[10px] text-[#484f58] mt-0.5">{step.reason}</p>
                    )}
                    {step.message && step.status !== 'pending' && (
                      <p className={`text-[10px] mt-0.5 ${
                        step.status === 'failed' ? 'text-[#f85149]'
                        : step.status === 'applied' ? 'text-[#3fb950]'
                        : 'text-[#484f58]'}`}>{step.message}</p>
                    )}
                  </div>
                ))}
              </div>
              {plan.uncertain?.length ? (
                <div className="px-2 py-1.5 border-t border-[#21262d]">
                  {plan.uncertain.map((u, i) => (
                    <p key={i} className="text-[10px] text-[#d29922]">? {u.question}</p>
                  ))}
                </div>
              ) : null}
              {/*
                Apply-all appears only once diffs exist. That ordering is the
                safety property: it writes exactly the ids `code.edit` returned,
                so nothing can be applied that has not been generated and put in
                front of the user.
              */}
              {prepared.length > 0 && (
                <div className="flex items-center gap-2 px-2 py-1.5 border-t border-[#21262d]">
                  <span className="text-[10px] text-[#8b949e]">
                    {prepared.length} reviewed change{prepared.length === 1 ? '' : 's'} ready
                  </span>
                  <button
                    onClick={applyAllDiffs}
                    disabled={Boolean(busy)}
                    className="ml-auto text-[10px] px-2 py-0.5 rounded bg-[#238636] hover:bg-[#2ea043] disabled:opacity-40 text-white font-medium"
                    title="Write every reviewed change. A backup of each file is kept beside it."
                  >Apply all</button>
                </div>
              )}
            </div>
          )}
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
  );

  return (
    <div className="flex h-full">
      {/* ---------------- right-click menu ---------------- */}
      {menu && (
        <div
          className="fixed z-50 min-w-[210px] py-1 bg-[#161b22] border border-[#30363d] rounded shadow-lg text-xs"
          style={{ left: menu.x, top: menu.y }}
          onClick={e => e.stopPropagation()}
        >
          <div className="px-3 py-1 text-[10px] text-[#484f58] truncate border-b border-[#21262d] mb-1"
            title={menu.node.path}>
            {menu.node.type === 'dir' ? '📁 ' : ''}{menu.node.name}
          </div>
          {menu.node.type === 'dir' && !isRemote && (
            <button
              onClick={() => useAsWorkspace(menu.node.path)}
              className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
              title="Rebind the editor (and Addled's workspace) to this folder"
            >Use as workspace</button>
          )}
          {menu.node.type === 'dir' && isRemote && (
            <div className="px-3 py-1.5 text-[#484f58]"
              title="Binding a workspace is done on the machine running Addled">
              Use as workspace — unavailable remotely
            </div>
          )}
          {menu.node.type === 'dir' && (
            <button
              onClick={() => addAiContext(menu.node.path)}
              disabled={aiContextFolders.includes(menu.node.path)}
              className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d] disabled:opacity-40 disabled:hover:bg-transparent"
            >
              {aiContextFolders.includes(menu.node.path)
                ? 'Already pinned as AI context' : 'Add as AI context'}
            </button>
          )}
          {menu.node.type === 'file' && (
            <>
              <button
                onClick={() => { setMenu(null); open(menu.node.path); }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
              >Open file</button>
              <button
                onClick={() => {
                  const dir = menu.node.path.split('/').slice(0, -1).join('/');
                  addAiContext(dir || '.');
                }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
              >Add its folder as AI context</button>
            </>
          )}
        </div>
      )}

      {/* ---------------- file tree ---------------- */}
      {!treeOpen && (
        <div className="w-9 border-r border-[#30363d] flex flex-col items-center shrink-0">
          <button
            onClick={() => { treeToggled.current = true; setTreeOpen(true); }}
            className="w-9 py-2 text-[#8b949e] hover:text-[#e8eaed]"
            title="Show the file tree"
          >☰</button>
        </div>
      )}
      <div className={`w-64 border-r border-[#30363d] flex flex-col shrink-0 ${treeOpen ? '' : 'hidden'}`}>
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
              <button
                onClick={() => { treeToggled.current = true; setTreeOpen(false); }}
                className="text-[#8b949e] hover:text-[#e8eaed]" title="Hide the file tree">«</button>
            </div>
          )}
          {bindError && <p className="text-[11px] text-[#f85149]">{bindError}</p>}
          {/*
            Say which folders are being hidden rather than showing a partial
            tree as if it were the whole thing. A folder that simply is not
            there reads as the explorer being broken — which is exactly how it
            was reported.
          */}
          {bound && !showAll && hiddenDirs.length > 0 && (
            <p className="text-[10px] text-[#484f58]">
              {hiddenDirs.length === 1
                ? <>Hiding <span className="font-mono">{hiddenDirs[0]}</span></>
                : <>Hiding {hiddenDirs.length} folders
                    {' ('}
                    <span className="font-mono">
                      {hiddenDirs.slice(0, 3).join(', ')}
                      {hiddenDirs.length > 3 ? ', …' : ''}
                    </span>
                    {')'}</>}
              {' — '}
              <button
                onClick={() => { setShowAll(true); rebind(true); }}
                className="text-[#58a6ff] hover:underline"
              >show all</button>
            </p>
          )}
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
              <div className="border-t border-[#30363d] px-3 py-1.5 text-[10px] text-[#d29922] shrink-0">
                Only the first {files.length} files are listed. Filter to reach
                the rest, or tick “show all” if what you want is inside a
                generated or dot folder.
              </div>
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
                extensions={[...extensionsFor(activeTab.language), oneDark, chrome, saveKeys,
                             // Wrap long lines. Without this a long line ran off the
                             // right edge and the only way to read it was the thin
                             // horizontal scrollbar, which reads as "no scrollbar at
                             // all". Wrapping is what a code editor is expected to do.
                             EditorView.lineWrapping]}
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
            {askPanel}
          </>
        ) : (
          <>
            <div className="flex-1 flex items-center justify-center text-[#8b949e]">
              <div className="text-center">
                <span className="text-4xl mb-3 block">💻</span>
                <p className="text-sm">
                  {bound ? 'Open a file from the tree to start editing'
                    : 'Bind a workspace to start coding'}
                </p>
                {bound && (
                  <p className="text-[11px] text-[#484f58] mt-2">
                    Or describe a change below — Addled plans which files to touch.
                  </p>
                )}
              </div>
            </div>
            {/*
              The composer is shown even when nothing is bound. It used to be
              `{bound && askPanel}`, so a FAILED bind looked like the message
              area had vanished — the remembered folder had been deleted, the
              bind errored, and the whole panel disappeared with the error
              buried in the tree header. The panel is harmless without a
              workspace: the buttons below are what need one, and they are
              already disabled on `!bound`.
            */}
            {askPanel}
          </>
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
