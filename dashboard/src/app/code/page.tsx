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
import type {
  CodeAppliedFile, CodeApplyPlanResult, CodeApplyResult, CodeGitStatus,
  CodeVerifyResult,
} from '@/lib/ws-types';
import {
  appendMessage, clearSessionMessages, createSession, deleteSession,
  ensureLoaded, getActiveId, getActiveSession, listSessions, messageId,
  renameSession, sessionForWorkspace, setActiveSession, subscribeCodeSessions,
  titleFromPrompt, togglePinned, updateMessage,
  type CodeAttachment, type CodeMessage, type CodeSession,
} from '@/lib/codeSessionStore';
import {
  filesFromPaste, filesToAttachments, releaseAttachments,
  releaseAttachment, MAX_ATTACHMENTS,
  type Attachment,
} from '@/lib/attachments';
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

/**
 * The last change Addled wrote, kept so it can be undone.
 *
 * `code.applyPlan` already returns whether it committed and which sha, plus a
 * `.bak` path for every file — and the page used to read none of it, so an
 * applied change was irreversible from the UI even though the backend had
exactly the information needed to reverse it. This is that information, held.
 *
 * Both paths exist because both cases are real: a git repo undoes via
 * `git revert` (an inverse commit, so pushed history is never rewritten), and a
 * plain folder undoes by restoring the `.bak` beside each file.
 */
type Checkpoint = {
  files: { filePath: string; backup?: string; created?: boolean }[];
  /** The commit Addled made, when the workspace is a repo. */
  sha?: string;
  /** The project's own check, run by the backend before it committed. */
  verify?: CodeVerifyResult;
  summary: string;
};

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

  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  // The AI flow: instruction -> proposed diff -> review -> apply.
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

  // @-mentions: files named in the composer, and a snapshot of the editor's
  // selection when the user attached it. Both are HINTS for the planner, like
  // the pinned folders — they steer where it looks, they do not widen what it
  // may write, because every write still has to pass the workspace containment
  // check on the backend.
  const [mentions, setMentions] = useState<string[]>([]);
  const [pullFrom, setPullFrom] = useState<{ from: number; to: number } | null>(null);
  // Which file the @ menu is filtering, and where its token starts in the box.
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);

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

  // The last applied change, and what the workspace's own check said about it.
  // `null` means nothing has been applied in this session, so no undo is
  // offered.
  const [checkpoint, setCheckpoint] = useState<Checkpoint | null>(null);
  const [undoing, setUndoing] = useState(false);

  // The git panel: whether this is a repo, what is uncommitted, and the diff.
  const [gitStatus, setGitStatus] = useState<CodeGitStatus | null>(null);
  const [gitDiffText, setGitDiffText] = useState('');
  const [gitBusy, setGitBusy] = useState(false);

  // ---- sessions, drawer and attachments -----------------------------------

  // A snapshot of the session store, refreshed through `subscribeCodeSessions`.
  // Kept as a counter rather than the sessions themselves so every render reads
  // the store directly — one source of truth, and no chance of the list and the
  // active session disagreeing.
  const [, bumpSessions] = useState(0);
  useEffect(() => {
    ensureLoaded();
    bumpSessions(n => n + 1);
    return subscribeCodeSessions(() => bumpSessions(n => n + 1));
  }, []);

  // The left drawer (sessions + file tree), open state. `drawerToggled` records
  // that the user opened or closed it by hand, so the width-based auto-collapse
  // below does not fight a deliberate choice.
  const [drawerOpen, setDrawerOpen] = useState(true);
  const drawerToggled = useRef(false);
  // Which section of the drawer is expanded. Four views, all answering a
  // different question about the same workspace: what am I working on
  // (sessions), what is in it (files), where does this text live (search), and
  // what has Addled changed here (changes).
  const [drawerTab, setDrawerTab] = useState<
    'sessions' | 'files' | 'search' | 'changes'>('sessions');

  // Which half of the main column is showing: the session transcript or the
  // open files. The editor stays mounted across a switch, so an unsaved buffer
  // is not lost by glancing at the transcript.
  const [view, setView] = useState<'chat' | 'editor'>('chat');

  // The hidden file input the composer's Attach button opens.
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Composer attachments: pasted images, pasted text, dropped documents.
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const dragDepth = useRef(0);

  // Right-click on the editor selection.
  const [selMenu, setSelMenu] = useState<{ x: number; y: number } | null>(null);
  // Right-click on empty space in the file tree.
  const [treeMenu, setTreeMenu] = useState<{ x: number; y: number } | null>(null);

  const activeSession = getActiveSession();
  const sessionList = listSessions();

  // The last on-demand check, and whether one is running. Separate from the
  // checkpoint's `verify` because that one describes a specific apply; this one
  // describes the workspace right now and is what a "did I break it?" question
  // is actually asking.
  const [checkResult, setCheckResult] = useState<CodeVerifyResult | null>(null);
  const [checking, setChecking] = useState(false);


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

  /**
   * The editor's current selection, as 1-based line numbers.
   *
   * Read from the CodeMirror view rather than tracked per keystroke: the value
   * is only wanted at the moment the user attaches it, and keeping a copy in
   * state would re-render the page on every cursor move for no benefit.
   *
   * Empty selections return null. A caret is not a selection, and attaching
   * "lines 12-12" to a prompt would claim the user pointed at a line they had
   * merely clicked on.
   */
  const currentSelection = useCallback((): { from: number; to: number } | null => {
    const view = viewRef.current;
    if (!view) return null;
    const { from, to } = view.state.selection.main;
    if (from === to) return null;
    const first = view.state.doc.lineAt(from).number;
    const last = view.state.doc.lineAt(to).number;
    return { from: first, to: last };
  }, []);

  /** The selected text itself, for sending to the planner as context. */
  const selectedText = useCallback((): string => {
    const view = viewRef.current;
    if (!view) return '';
    const { from, to } = view.state.selection.main;
    return from === to ? '' : view.state.sliceDoc(from, to);
  }, []);

  // ---- attachments --------------------------------------------------------

  /**
   * Add files the user pasted, dropped or picked.
   *
   * All three arrive here so they share one path: the size cap, the image
   * downscale and the text/binary decision are made once, in
   * `lib/attachments.ts`, rather than three times slightly differently.
   */
  const addFiles = useCallback(async (files: FileList | File[] | null) => {
    const next = await filesToAttachments(files, attachments.length);
    if (!next.length) return;
    setAttachments(prev => [...prev, ...next].slice(0, MAX_ATTACHMENTS));
  }, [attachments.length]);

  const removeAttachmentAt = useCallback((index: number) => {
    setAttachments(prev => {
      releaseAttachment(prev[index]);
      return prev.filter((_, i) => i !== index);
    });
  }, []);

  /** Paste: an image or file becomes an attachment; plain text goes in the box. */
  const onComposerPaste = useCallback((e: React.ClipboardEvent) => {
    const { files } = filesFromPaste(e);
    if (!files.length) return;   // ordinary text paste — let the textarea have it
    e.preventDefault();
    addFiles(files);
  }, [addFiles]);

  // Drag & drop. A depth counter rather than a boolean: dragging over a child
  // element fires `dragleave` on the parent, so a boolean flickers the overlay.
  const onDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (!Array.from(e.dataTransfer.types).includes('Files')) return;
    dragDepth.current += 1;
    setDragActive(true);
  }, []);

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragActive(false);
    if (!bound || wsState !== 'connected') return;
    addFiles(e.dataTransfer.files);
  }, [addFiles, bound, wsState]);

  // ---- sessions -----------------------------------------------------------

  const startSession = useCallback(() => {
    // Titled from the first prompt, not here: naming it before any work exists
    // asks the user to decide something they cannot know yet.
    if (!workspacePath) return;
    createSession(workspacePath, 'New session');
    setDrawerTab('sessions');
  }, [workspacePath]);

  // ---- right-click actions ------------------------------------------------

  /** Copy a path, with a visible failure rather than a silent one. */
  const copyPath = useCallback(async (path: string) => {
    try {
      await navigator.clipboard.writeText(path);
      setNotice(`Copied ${path}`);
      setTimeout(() => setNotice(''), 2500);
    } catch {
      setError('Could not copy to the clipboard.');
    }
  }, []);

  /**
   * Reveal a path in the OS file manager.
   *
   * Deliberately not a backend call. The dashboard runs in the Electron shell
   * beside the app, and adding a "run this command for me" method is a much
   * larger surface than a reveal needs. `shell.reveal` is absent, so this
   * degrades to copying the path and saying so — honest about what it did.
   */
  const revealPath = useCallback(async (path: string) => {
    await copyPath(`${workspacePath}/${path}`.replace(/\/+/g, '/'));
    setNotice('Path copied — open your file manager to paste it');
    setTimeout(() => setNotice(''), 3500);
  }, [copyPath, workspacePath]);

  /**
   * Ask the model about the current editor selection.
   *
   * Writes a prompt into the composer rather than sending it, so the user can
   * add to it — "explain this" is usually a starting point, not the whole
   * request — and the selected lines ride along as context when they send.
   */
  const askAboutSelection = useCallback((kind: string) => {
    setSelMenu(null);
    if (!activePath) return;
    const sel = currentSelection();
    setPullFrom(sel);
    const prompts: Record<string, string> = {
      explain: 'Explain what this code does and why it is written this way.',
      refactor: 'Refactor this code to be clearer without changing behaviour.',
      test: 'Write tests for this code, covering the edge cases.',
      fix: 'Find the bug in this code and fix it.',
      document: 'Add a docstring/comment explaining this code.',
    };
    setInstruction(prompts[kind] || '');
  }, [activePath, currentSelection]);

  /** Put a file's path into the composer as an @-mention. */
  const attachFileToPrompt = useCallback((path: string) => {
    setMentions(prev => prev.includes(path) ? prev : [...prev, path]);
    setNotice(`Attached ${path} to the request`);
    setTimeout(() => setNotice(''), 2500);
  }, []);

  // A right-click menu anywhere must close on a click, a scroll or Escape.
  useEffect(() => {
    if (!selMenu && !treeMenu) return;
    const close = () => { setSelMenu(null); setTreeMenu(null); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
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
  }, [selMenu, treeMenu]);

  // ---- git, undo and the verify verdict ----------------------------------

  /**
   * Re-read whether this is a repo and what is uncommitted.
   *
   * Called on bind and after every apply, because those are the only two
   * moments the answer changes. A non-repo folder is a normal outcome, not an
   * error: the panel says so and the undo path falls back to `.bak` files.
   *
   * Declared ahead of `applyEdit` because `applyEdit` names it in a dependency
   * array, which is evaluated during render — a later declaration would be a
   * temporal-dead-zone crash rather than a hoisting convenience.
   */
  const refreshGit = useCallback(async (diff = true) => {
    if (!workspacePath || wsState !== 'connected') return;
    setGitBusy(true);
    try {
      const st: CodeGitStatus = await send('code.git.status', {
        workspaceId: workspacePath,
      });
      setGitStatus(st);
      if (diff && st?.isRepo) {
        const d = await send('code.git.diff', { workspaceId: workspacePath });
        setGitDiffText(String(d?.diff || ''));
      } else {
        setGitDiffText('');
      }
    } catch {
      // A workspace that cannot be read is reported by the bind, not here.
      setGitStatus({ isRepo: false });
      setGitDiffText('');
    } finally { setGitBusy(false); }
  }, [send, workspacePath, wsState]);

  /**
   * Undo the last change Addled wrote.
   *
   * Two mechanisms, because both cases are real and the UI must not imply the
   * wrong one ran:
   *
   *  * A repo undoes with `code.git.revert`, which adds an INVERSE commit. It
   *    never rewrites history — the branch may already be pushed — and the
   *    result names the new sha, so the undo is itself revertible.
   *  * A plain folder restores the `.bak` the backend kept beside each file.
   *    The backup is read back through `code.read` and written with
   *    `code.write`, so the same path containment that guards every other write
   *    guards this one too.
   *
   * A file the change CREATED has no backup, so it is reported as unrestorable
   * rather than quietly left in place as if the undo had worked.
   */
  const undoLastChange = useCallback(async () => {
    if (!checkpoint) return;
    const viaGit = Boolean(checkpoint.sha);
    const label = checkpoint.files.map(f => f.filePath).join(', ');
    if (!window.confirm(
      viaGit
        ? `Undo this change by reverting commit ${checkpoint.sha?.slice(0, 8)}?\n\n`
          + `Files: ${label}\n\nThis adds a new commit that reverses the change; `
          + 'nothing already committed is rewritten.'
        : `Undo this change by restoring the backup of:\n\n${label}\n\n`
          + 'The workspace is not a git repository, so the .bak files are used.'
    )) return;

    setUndoing(true); setError('');
    try {
      if (viaGit) {
        const r = await send('code.git.revert', {
          workspaceId: workspacePath, sha: checkpoint.sha,
        });
        if (!r?.success) {
          setError(r?.error || 'The revert was refused.');
          return;
        }
        setApplied(`Undone — reverted as ${String(r.sha || '').slice(0, 8)}`);
      } else {
        const restored: string[] = [];
        const missing: string[] = [];
        for (const f of checkpoint.files) {
          if (!f.backup) { missing.push(f.filePath); continue; }
          const b = await send('code.read', {
            workspaceId: workspacePath, filePath: f.backup,
          });
          const content = String(b?.content ?? '');
          if (content.startsWith('// Refused:') || content.startsWith('// Error:')) {
            missing.push(f.filePath);
            continue;
          }
          const w = await send('code.write', {
            workspaceId: workspacePath, filePath: f.filePath, content,
          });
          if (w?.success) restored.push(f.filePath);
          else missing.push(f.filePath);
        }
        setApplied(
          `Restored ${restored.length} file${restored.length === 1 ? '' : 's'}`
          + (missing.length ? ` — could not restore ${missing.join(', ')}` : '.')
        );
      }
      // The tabs now show something that is no longer on disk.
      for (const f of checkpoint.files) {
        if (!tabs.some(t => t.path === f.filePath)) continue;
        try {
          const fresh = await send('code.read', {
            workspaceId: workspacePath, filePath: f.filePath,
          });
          const content = String(fresh?.content ?? '');
          setTabs(prev => prev.map(t => t.path === f.filePath
            ? { ...t, content, saved: content, isNew: false } : t));
        } catch { /* the disk is right; a stale tab is not worth an error */ }
      }
      setCheckpoint(null);
      await refreshGit();
    } catch (e: any) {
      setError(e?.message || 'The change could not be undone.');
    } finally { setUndoing(false); }
  }, [checkpoint, send, workspacePath, tabs, refreshGit]);

  /**
   * Keep the git panel honest when the bound workspace changes.
   *
   * Driven by an effect rather than called from `bind` because the folder can
   * also arrive from the remembered setting on first load, and both paths must
   * end with a correct panel. A checkpoint belongs to the folder it was made in,
   * so it is cleared here — offering "undo" against a different repository would
   * revert a commit the user never made in this workspace.
   */
  useEffect(() => {
    setCheckpoint(null);
    if (wsState === 'connected' && workspacePath) refreshGit();
  }, [wsState, workspacePath, refreshGit]);

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
      const r: CodeApplyResult = await send('code.apply', {
        workspaceId: workspacePath, filePath: pending.filePath, editId: pending.editId,
      });
      if (!r?.success) { setError(r?.error || 'The change was refused.'); return; }
      setApplied(r.created ? 'Applied — the file was created.'
        : `Applied${r.backup ? ` — previous version kept at ${r.backup}` : ''}`);
      // Hold what the backend already told us, so this change can be undone.
      setCheckpoint({
        files: [{ filePath: pending.filePath, backup: r.backup,
                  created: r.created }],
        sha: r.git?.used ? r.git.sha : undefined,
        verify: r.verify,
        summary: `edit ${pending.filePath}`,
      });
      const fresh = await send('code.read', {
        workspaceId: workspacePath, filePath: pending.filePath,
      });
      const content = String(fresh?.content ?? '');
      setTabs(prev => prev.map(t => t.path === pending.filePath
        ? { ...t, content, saved: content, isNew: false } : t));
      setPending(null);
      refreshGit();
    } catch (e: any) {
      setError(e?.message || 'The change could not be applied.');
    } finally { setBusy(''); }
  }, [pending, send, workspacePath, refreshGit]);

  /**
   * Run the workspace's own test command, on demand.
   *
   * Two calls on purpose. `detectOnly` asks what WOULD run; if the answer is
   * nothing, that is reported and no command is executed. Running blind and
   * reporting "not ok" would be indistinguishable from a genuine test failure,
   * which is the worst possible confusion for a feature whose whole value is
   * telling those two apart.
   *
   * The command is discovered from the project, never chosen here — the page
   * does not get to decide how someone else's project is verified.
   */
  const runCheck = useCallback(async () => {
    if (!workspacePath || wsState !== 'connected' || checking) return;
    setChecking(true); setCheckResult(null); setError('');
    try {
      const probe: CodeVerifyResult = await send('code.verify', {
        workspaceId: workspacePath, detectOnly: true,
      });
      if (!probe?.command) {
        setCheckResult({
          ok: false, ran: false,
          reason: probe?.reason || 'no test or verify command was found',
        });
        return;
      }
      const r: CodeVerifyResult = await send('code.verify', {
        workspaceId: workspacePath, command: probe.command,
      });
      setCheckResult(r);
      await refreshGit(false);
    } catch (e: any) {
      setError(e?.message || 'The check could not be run.');
    } finally { setChecking(false); }
  }, [send, workspacePath, wsState, checking, refreshGit]);

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

    // The turn is recorded in the session BEFORE the request goes out, so a
    // slow or failed plan still leaves a visible trace of what was asked. The
    // reply is attached to this same message once the plan comes back.
    const session = getActiveSession();
    const userMsgId = messageId();
    const sentAttachments: CodeAttachment[] = attachments.map(a => ({
      name: a.name, kind: a.kind, data: a.data, preview: a.preview,
      size: a.size,
    }));
    if (session) {
      // First prompt names the session, so the sidebar says what this is about
      // without the user having to title anything.
      if (!session.messages.length) {
        renameSession(session.id, titleFromPrompt(ask));
      }
      appendMessage(session.id, {
        id: userMsgId, role: 'user', content: ask,
        timestamp: Date.now(),
        attachments: sentAttachments.length ? sentAttachments : undefined,
      });
    }
    // The composer's copy is handed to the session; it owns the URLs now.
    setAttachments([]);
    setInstruction('');

    const replyId = messageId();
    try {
      const r = await send('code.plan', {
        workspaceId: workspacePath,
        instruction: ask,
        contextFolders: aiContextFolders,
        // @-mentions and the attached selection travel the same way the pinned
        // folders do: as hints the planner is told to start from. They are not
        // a scope change and the backend does not treat them as one, so a file
        // named here still has to be inside the workspace to be written.
        contextFiles: [
          ...mentions.map(path => ({ path })),
          ...(pullFrom && activePath
            ? [{ path: activePath, from: pullFrom.from, to: pullFrom.to }]
            : []),
          // Selected code travels as text, not just a line range: the planner
          // may be asked about a file it has no reason to open otherwise, and
          // a range alone tells it nothing about what is in those lines.
          ...(pullFrom && activePath && selectedText()
            ? [{ path: activePath, text: selectedText().slice(0, 4000) }]
            : []),
        ],
        // Images and documents. The backend routes an image through the vision
        // model and reads a known document type, so a screenshot of an error is
        // as usable here as it is in Chat.
        attachments: sentAttachments.map(a => ({
          name: a.name, kind: a.kind, data: a.data,
        })),
      });
      const files = (r?.plan?.steps || []).map((s: any) => s.filePath);
      if (r?.status === 'refused') {
        setError(r?.message || 'That is outside the workspace.');
        if (session) {
          appendMessage(session.id, {
            id: replyId, role: 'assistant',
            content: r?.message || 'That is outside the workspace.',
            timestamp: Date.now(), note: 'Refused',
          });
        }
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
        if (session) {
          appendMessage(session.id, {
            id: replyId, role: 'assistant',
            content: r.plan.summary || `A plan touching ${files.length} file(s).`,
            timestamp: Date.now(), files,
            note: 'Plan ready — generate a diff to review it.',
          });
        }
      } else {
        const why = r?.message || 'The planner found nothing to change.';
        setError(why);
        if (session) {
          appendMessage(session.id, {
            id: replyId, role: 'assistant', content: why,
            timestamp: Date.now(), note: 'No plan',
          });
        }
      }
    } catch (e: any) {
      const why = e?.message || 'Planning failed.';
      setError(why);
      if (session) {
        appendMessage(session.id, {
          id: replyId, role: 'assistant', content: why,
          timestamp: Date.now(), note: 'Failed',
        });
      }
    } finally { setPlanning(false); setBusy(''); }
  }, [instruction, send, workspacePath, wsState, bound, aiContextFolders,
      mentions, pullFrom, activePath, attachments, selectedText]);

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
      const r: CodeApplyPlanResult = await send('code.applyPlan', {
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
        // Hold the checkpoint. `git.sha` is what makes "undo" a `git revert`
        // rather than a backup restore, and `verify` is what lets the page say
        // "verified" instead of only "written" — both were being discarded.
        setCheckpoint({
          files: done.map((d: CodeAppliedFile) => ({
            filePath: d.filePath, backup: d.backup, created: d.created,
          })),
          sha: r.git?.used ? r.git.sha : undefined,
          verify: r.verify,
          summary: plan?.summary || '',
        });
        // Note the outcome in the session, so the transcript says what happened
        // to the plan rather than only what was proposed.
        {
          const session = getActiveSession();
          if (session) {
            const verdict = r.verify?.ran
              ? (r.verify.ok ? 'check passed' : 'check did not pass')
              : 'not verified';
            appendMessage(session.id, {
              id: messageId(), role: 'assistant',
              content: `Applied ${done.length} file${done.length === 1 ? '' : 's'} — ${verdict}.`,
              timestamp: Date.now(),
              files: done.map((d: CodeAppliedFile) => d.filePath),
              note: r.git?.used
                ? `Committed ${String(r.git.sha || '').slice(0, 8)}`
                : 'Backups kept beside each file',
            });
          }
        }
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
      // The workspace just changed, so the panel's picture of it is stale.
      await refreshGit();
    } catch (e: any) {
      setError(e?.message || 'The changes could not be applied.');
    } finally { setBusy(''); }
  }, [prepared, send, workspacePath, plan, refreshGit]);

  // ---- keyboard -----------------------------------------------------------

  const saveKeys = useMemo(() => keymap.of([
    { key: 'Mod-s', preventDefault: true, run: () => { saveRef.current(); return true; } },
  ]), []);

  const onUpdate = useCallback((vu: any) => {
    const head = vu?.state?.selection?.main?.head;
    if (typeof head !== 'number') return;
    const line = vu.state.doc.lineAt(head);
    let label = `Ln ${line.number}, Col ${head - line.from + 1}`;
    // A selection is appended to the label so the value ALSO changes when the
    // selection does. It feeds `selectionAvailable`, and without this a
    // selection dragged back from the same head would leave the attach button
    // stale — the head is unchanged, so the label alone would not move.
    const sel = vu.state.selection.main;
    if (sel.from !== sel.to) {
      const a = vu.state.doc.lineAt(sel.from).number;
      const b = vu.state.doc.lineAt(sel.to).number;
      label += ` (${b - a + 1} line${b === a ? '' : 's'} selected)`;
    }
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
   * A fixed-width drawer and a working column need room between them.
   *
   * The drawer is 248px and `shrink-0`, so it never yields: in a narrow window
   * it wins the whole width and the composer is squeezed to ZERO. That is not
   * hypothetical — measured in the running app at a 288px viewport, the main
   * column reported `width: 0` and the page looked empty beside a full-height
   * sidebar. The old page had this guard and the rebuild dropped it while
   * keeping the fixed width, so the failure it documents came straight back.
   *
   * It hides itself below 720px, and the toggle overrides this either way —
   * `drawerToggled` records a deliberate choice so a resize cannot undo it.
   */
  useEffect(() => {
    const apply = () => {
      if (!drawerToggled.current) setDrawerOpen(window.innerWidth >= 720);
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

  /**
   * Files matching whatever is being typed after an "@".
   *
   * An empty token lists everything, which is the useful behaviour when the
   * user has just pressed "@" and does not yet know what is in the workspace.
   * Already-mentioned files are left out so the menu cannot suggest something
   * that is visibly already attached.
   */
  const mentionMatches = useMemo(() => {
    if (mentionQuery === null) return [];
    const q = mentionQuery.toLowerCase();
    return files
      .filter(f => !mentions.includes(f.path))
      .filter(f => !q || f.path.toLowerCase().includes(q))
      .slice(0, 30);
  }, [mentionQuery, files, mentions]);

  /**
   * Whether the editor currently holds a selection worth attaching.
   *
   * Read on every render, which is what makes the button appear and disappear as
   * the user selects. Cheap: it is two numbers off an existing object, not a
   * scan of the document.
   */
  const selectionAvailable = useMemo(
    () => Boolean(viewRef.current && currentSelection()),
    // `cursor` changes on every selection move, which is exactly the signal this
    // needs to re-evaluate — without it the button would only update when some
    // other state happened to change.
    [cursor, activePath, currentSelection],
  );

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
   * What the project's own check said about the last change.
   *
   * The distinction this exists to make is "written" versus "verified", which is
   * the difference between finished and done. The backend already ran the check
   * before it committed; the page simply never showed the answer.
   *
   * Three states, and the third is the one that matters: a check that never RAN
   * must not look like a pass. A green tick over nothing is worse than saying
   * plainly that there was nothing to run.
   */
  const verifyBadge = (v?: CodeVerifyResult) => {
    if (!v) return null;
    if (!v.ran) {
      return (
        <span className="text-[#8b949e]"
          title={v.reason || 'No test or verify command was found in this workspace.'}>
          not verified — {v.reason || 'no check found'}
        </span>
      );
    }
    const where = v.command ? ` (${v.command})` : '';
    if (v.ok) {
      return (
        <span className="text-[#3fb950]" title={v.verdict || `${v.command} passed`}>
          ✓ verified{where}
        </span>
      );
    }
    // Ran, but collected nothing. Most runners exit non-zero for this, so it
    // would otherwise render as a failure — telling the user their change broke
    // the tests when the project simply has none that match.
    if (v.noTests) {
      return (
        <span className="text-[#8b949e]"
          title={v.verdict || 'The check ran but found no tests.'}>
          no tests found{where}
        </span>
      );
    }
    return (
      <span className="text-[#d29922]"
        title={v.verdict || `${v.command || 'check'} did not pass`}>
        ⚠ written, but the check did not pass{where}
      </span>
    );
  };

  /**
   * The composer, shared by both states.
   *
   * Built as a variable rather than duplicated because it must appear whether
   * or not a file is open: planning is about the workspace, so needing an open
   * tab first would be the wrong shape. The `activeTab`-only bits inside it are
   * guarded.
  /**
   * The Changes view: what Addled changed and whether it is reversible.
   *
   * Carried over from the old bottom panel into the drawer, because the shell
   * changed but the question it answers did not. `code.git.status` and
   * `code.git.diff` are the source; a non-repo workspace is stated plainly
   * rather than shown as an error, and the backup-based undo is promised so
   * "no git" does not read as "no undo".
   */
  const changesPanel = (
    <div className="p-3 space-y-2">
      {!gitStatus ? (
        <p className="text-[11px] text-[#484f58]">
          {gitBusy ? 'Checking the workspace…' : 'Bind a workspace to see changes.'}
        </p>
      ) : !gitStatus.isRepo ? (
        <div className="space-y-1">
          <p className="text-[11px] text-[#8b949e]">Not a git repository.</p>
          <p className="text-[10px] text-[#484f58]">
            {gitStatus.available === false
              ? 'No git binary was found on this machine. '
              : ''}
            Undo still works: each written file keeps a <code>.bak</code>{' '}
            beside it, and the last change can be restored from it.
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 text-[11px]">
            <span className="text-[#e8eaed]">
              {gitStatus.branch ? `on ${gitStatus.branch}` : 'detached HEAD'}
            </span>
            <button
              onClick={runCheck}
              disabled={checking}
              className="ml-auto px-1.5 py-0.5 rounded border border-[#30363d] text-[10px] text-[#8b949e] hover:text-[#58a6ff] disabled:opacity-40"
              title="Run this project's own test or verify command"
            >{checking ? 'Running…' : 'Run check'}</button>
            <button
              onClick={() => refreshGit()}
              disabled={gitBusy}
              className="text-[#8b949e] hover:text-[#58a6ff] disabled:opacity-40"
              title="Re-read the workspace"
            >{gitBusy ? '…' : '⟳'}</button>
          </div>
          {checkResult && <p className="text-[10px]">{verifyBadge(checkResult)}</p>}
          {gitStatus.clean ? (
            <p className="text-[11px] text-[#3fb950]">Clean — nothing uncommitted.</p>
          ) : (
            <>
              {gitStatus.changed?.length ? (
                <div>
                  <p className="text-[10px] text-[#8b949e]">
                    Modified ({gitStatus.changed.length})
                  </p>
                  <div className="max-h-24 overflow-y-auto">
                    {gitStatus.changed.map(f => (
                      <button key={f} onClick={() => { open(f); setView('editor'); }}
                        className="w-full text-left px-1.5 py-0.5 text-[10px] font-mono text-[#d29922] hover:bg-[#21262d] truncate"
                        title={f}>{f}</button>
                    ))}
                  </div>
                </div>
              ) : null}
              {gitStatus.untracked?.length ? (
                <div>
                  <p className="text-[10px] text-[#8b949e]">
                    Untracked ({gitStatus.untracked.length})
                  </p>
                  <div className="max-h-24 overflow-y-auto">
                    {gitStatus.untracked.map(f => (
                      <button key={f} onClick={() => { open(f); setView('editor'); }}
                        className="w-full text-left px-1.5 py-0.5 text-[10px] font-mono text-[#484f58] hover:bg-[#21262d] truncate"
                        title={f}>{f}</button>
                    ))}
                  </div>
                </div>
              ) : null}
            </>
          )}
          {gitDiffText ? (
            <details className="border border-[#30363d] rounded">
              <summary className="px-2 py-1 text-[10px] text-[#8b949e] cursor-pointer">
                Uncommitted diff
              </summary>
              <pre className="p-2 text-[10px] font-mono whitespace-pre-wrap max-h-56 overflow-y-auto border-t border-[#21262d]">
                {gitDiffText.split('\n').map((line, i) => (
                  <div key={i} className={
                    line.startsWith('+++') || line.startsWith('---') ? 'text-[#8b949e]'
                    : line.startsWith('+') ? 'text-[#3fb950]'
                    : line.startsWith('-') ? 'text-[#f85149]'
                    : line.startsWith('@@') ? 'text-[#58a6ff]' : 'text-[#8b949e]'}>{line}</div>
                ))}
              </pre>
            </details>
          ) : null}
        </>
      )}
    </div>
  );

  /**
   * Search across the bound workspace. Results open the file at the line.
   *
   * Kept as its own view rather than folded into the composer's @ menu: the two
   * answer different questions. The @ menu names a file to use as context; this
   * finds where some text lives, which is what you want before deciding which
   * file to talk about.
   */
  const searchPanel = (
    <div className="p-3 space-y-2">
      <div className="flex gap-2">
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') search(); }}
          placeholder="Find in the workspace…"
          className="flex-1 min-w-0 bg-[#161b22] border border-[#30363d] rounded px-3 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"
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
        <div className="max-h-[calc(100vh-220px)] overflow-y-auto border border-[#30363d] rounded">
          {hits.map((hit, i) => (
            <button
              key={`${hit.filePath}:${hit.line}:${i}`}
              onClick={() => { open(hit.filePath, hit.line); setView('editor'); }}
              className="w-full text-left px-2 py-1 text-[11px] hover:bg-[#21262d] border-b border-[#21262d] last:border-0"
            >
              <span className="text-[#58a6ff]">{hit.filePath}:{hit.line}</span>
              <span className="text-[#8b949e] ml-2 font-mono">{hit.text}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
  /**
   * One row in the session list.
   *
   * A local function so the pinned and recent lists cannot drift apart in
   * behaviour — rename, pin and delete must work identically in both.
   */
  const sessionRow = (s: CodeSession, active: boolean) => (
    <div
      key={s.id}
      className={`group flex items-center gap-2 px-3 py-1.5 cursor-pointer ${
        active ? 'bg-[#1f6feb22] text-[#58a6ff]' : 'text-[#c9d1d9] hover:bg-[#161b22]'}`}
      onClick={() => setActiveSession(s.id)}
      title={`${s.title} · ${s.messages.length} message(s)`}
    >
      <span className="shrink-0 opacity-80" title={s.pinned ? 'Pinned' : undefined}>
        {s.pinned ? (
          <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a5.994 5.994 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707-.195-.195.512-1.22.707-1.414l3.182-3.182L2.099 8.22a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a5.995 5.995 0 0 1 1.013.16l3.134-3.133a2.772 2.772 0 0 1-.04-.461c0-.43.108-1.022.589-1.503a.5.5 0 0 1 .354-.147Z" />
          </svg>
        ) : (
          <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className="opacity-30">
            <path d="M4.5 2A1.5 1.5 0 0 0 3 3.5v9A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5v-9A1.5 1.5 0 0 0 11.5 2h-7Z" opacity=".35" />
          </svg>
        )}
      </span>
      <span className="flex-1 min-w-0 truncate text-xs">{s.title}</span>
      <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
        <button
          onClick={e => {
            e.stopPropagation();
            const next = window.prompt('Rename session', s.title);
            if (next !== null) renameSession(s.id, next);
          }}
          className="text-[#8b949e] hover:text-[#e8eaed] px-0.5"
          title="Rename"
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M11.013 1.427a1.75 1.75 0 0 1 2.474 0l1.086 1.086a1.75 1.75 0 0 1 0 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 0 1-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61Zm.176 4.823L9.75 4.81l-6.286 6.287a.253.253 0 0 0-.064.108l-.558 1.953 1.953-.558a.253.253 0 0 0 .108-.064l6.286-6.286Z" />
          </svg>
        </button>
        <button
          onClick={e => { e.stopPropagation(); togglePinned(s.id); }}
          className="text-[#8b949e] hover:text-[#e8eaed] px-0.5"
          title={s.pinned ? 'Unpin' : 'Pin to the top'}
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a5.994 5.994 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707-.195-.195.512-1.22.707-1.414l3.182-3.182L2.099 8.22a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a5.995 5.995 0 0 1 1.013.16l3.134-3.133a2.772 2.772 0 0 1-.04-.461c0-.43.108-1.022.589-1.503a.5.5 0 0 1 .354-.147Z" />
          </svg>
        </button>
        <button
          onClick={e => {
            e.stopPropagation();
            if (window.confirm(`Delete "${s.title}"? Its prompts are lost.`)) {
              deleteSession(s.id);
            }
          }}
          className="text-[#8b949e] hover:text-[#f85149] px-0.5"
          title="Delete"
        >X</button>
      </div>
    </div>
  );

  /**
   * The context chips above the composer — the Code page's answer to Claude
   * Code's "Local / app / main / worktree" row.
   *
   * Each chip states something the request is genuinely made against, so the
   * row is a summary of the turn's real context rather than decoration: where
   * the workspace is, what branch it is on, whether it is a repo at all, which
   * session is active, and whether anything is attached.
   */
  const contextChips = (
    <div className="flex flex-wrap items-center gap-1.5">
      <button
        onClick={() => { drawerToggled.current = true; setDrawerOpen(true); setDrawerTab('files'); }}
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[#30363d] bg-[#161b22] text-[11px] text-[#c9d1d9] hover:border-[#484f58]"
        title={workspacePath || 'No workspace bound'}
      >
        {/* A drawn glyph, not a word. An earlier version used the literal text
            "FOLDER" as a placeholder and it rendered as the word — which reads
            as a label, not an icon, and looked broken. */}
        <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"
          aria-hidden="true" className="shrink-0 opacity-80">
          <path d="M1.75 1A1.75 1.75 0 0 0 0 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0 0 16 13.25v-8.5A1.75 1.75 0 0 0 14.25 3H7.5a.25.25 0 0 1-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1Z" />
        </svg>
        <span className="max-w-[160px] truncate">
          {bound ? workspacePath.split(/[\\/]/).filter(Boolean).pop() : 'No workspace'}
        </span>
      </button>
      {gitStatus?.isRepo && gitStatus.branch && (
        <span
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[#30363d] bg-[#161b22] text-[11px] text-[#c9d1d9]"
          title={`git branch ${gitStatus.branch}`}
        >
          <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"
            aria-hidden="true" className="shrink-0 opacity-80">
            <path d="M9.5 3.25a2.25 2.25 0 1 1 3 2.122V6A2.5 2.5 0 0 1 10 8.5H6a1 1 0 0 0-1 1v1.128a2.251 2.251 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.5 0v1.836A2.492 2.492 0 0 1 6 7h4a1 1 0 0 0 1-1v-.628A2.25 2.25 0 0 1 9.5 3.25Zm-6 0a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Zm8.25-.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5ZM4.25 12a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
          </svg>
          <span className="max-w-[140px] truncate">{gitStatus.branch}</span>
        </span>
      )}
      {gitStatus && !gitStatus.isRepo && (
        <span
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[#30363d] bg-[#161b22] text-[11px] text-[#8b949e]"
          title="Not a git repository — undo falls back to the per-file backups"
        >no git</span>
      )}
      {activeSession && (
        <span
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[#30363d] bg-[#161b22] text-[11px] text-[#8b949e] max-w-[220px]"
          title={activeSession.title}
        >
          <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"
            aria-hidden="true" className="shrink-0 opacity-70">
            <path d="M3.5 3.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 5.25a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 5.25a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5ZM7 4.5a.75.75 0 0 1 .75-.75h5.5a.75.75 0 0 1 0 1.5h-5.5A.75.75 0 0 1 7 4.5Zm0 5.25a.75.75 0 0 1 .75-.75h5.5a.75.75 0 0 1 0 1.5h-5.5a.75.75 0 0 1-.75-.75Zm0 5.25a.75.75 0 0 1 .75-.75h5.5a.75.75 0 0 1 0 1.5h-5.5a.75.75 0 0 1-.75-.75Z" />
          </svg>
          <span className="truncate">{activeSession.title}</span>
        </span>
      )}
      {attachments.length > 0 && (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-[#1f6feb55] bg-[#1f6feb22] text-[11px] text-[#58a6ff]">
          {attachments.length} attached
        </span>
      )}
    </div>
  );

  return (
    <div
      className="relative flex h-full"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {dragActive && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-[#0d1117]/85 border-2 border-dashed border-[#3380FF] pointer-events-none">
          <div className="text-center">
            <p className="text-sm font-medium text-[#e8eaed]">Drop to attach</p>
            <p className="text-xs text-[#8b949e] mt-1">
              Screenshots, text, PDFs and documents — up to {MAX_ATTACHMENTS}
            </p>
          </div>
        </div>
      )}

      {/* ---------------- tree node menu ---------------- */}
      {menu && (
        <div
          className="fixed z-50 min-w-[230px] py-1 bg-[#161b22] border border-[#30363d] rounded shadow-lg text-xs"
          style={{ left: menu.x, top: menu.y }}
          onClick={e => e.stopPropagation()}
        >
          <div className="px-3 py-1 text-[10px] text-[#484f58] truncate border-b border-[#21262d] mb-1"
            title={menu.node.path}>
            {menu.node.name}
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
            <>
              <button
                onClick={() => {
                  const p = menu.node.path;
                  setMenu(null); setDrawerTab('files'); setFilter(p + '/');
                }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
                title="Filter the tree down to this folder"
              >Search inside this folder</button>
              <button
                onClick={() => addAiContext(menu.node.path)}
                disabled={aiContextFolders.includes(menu.node.path)}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d] disabled:opacity-40 disabled:hover:bg-transparent"
              >
                {aiContextFolders.includes(menu.node.path)
                  ? 'Already pinned as AI context' : 'Add as AI context'}
              </button>
            </>
          )}
          {menu.node.type === 'file' && (
            <>
              <button
                onClick={() => { setMenu(null); open(menu.node.path); }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
              >Open file</button>
              <button
                onClick={() => { setMenu(null); attachFileToPrompt(menu.node.path); }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
                title="Add this file to the next request as a mention"
              >Attach to prompt</button>
              <button
                onClick={() => { setMenu(null); copyPath(menu.node.path); }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
              >Copy path</button>
              <button
                onClick={() => { setMenu(null); revealPath(menu.node.path); }}
                className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
                title="Copy the full path so it can be opened in your file manager"
              >Reveal in file manager</button>
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

      {/* ---------------- selection menu (editor right-click) ---------------- */}
      {selMenu && (
        <div
          className="fixed z-50 min-w-[210px] py-1 bg-[#161b22] border border-[#30363d] rounded shadow-lg text-xs"
          style={{ left: selMenu.x, top: selMenu.y }}
          onClick={e => e.stopPropagation()}
        >
          <div className="px-3 py-1 text-[10px] text-[#484f58] border-b border-[#21262d] mb-1">
            {(() => {
              const s = currentSelection();
              return s ? `Lines ${s.from}-${s.to} of ${activeTab?.name}` : 'Selection';
            })()}
          </div>
          <button onClick={() => askAboutSelection('explain')}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]">Explain this</button>
          <button onClick={() => askAboutSelection('refactor')}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]">Refactor this</button>
          <button onClick={() => askAboutSelection('test')}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]">Write tests for this</button>
          <button onClick={() => askAboutSelection('fix')}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]">Find the bug in this</button>
          <button onClick={() => askAboutSelection('document')}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]">Document this</button>
          <div className="border-t border-[#21262d] mt-1 pt-1">
            <button
              onClick={() => { setSelMenu(null); setPullFrom(currentSelection()); }}
              className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
              title="Attach these lines to the next request"
            >Attach to prompt</button>
            <button
              onClick={async () => { setSelMenu(null); await copyPath(selectedText().slice(0, 4000)); }}
              className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
            >Copy selection</button>
          </div>
        </div>
      )}

      {/* ---------------- tree background menu ---------------- */}
      {treeMenu && (
        <div
          className="fixed z-50 min-w-[200px] py-1 bg-[#161b22] border border-[#30363d] rounded shadow-lg text-xs"
          style={{ left: treeMenu.x, top: treeMenu.y }}
          onClick={e => e.stopPropagation()}
        >
          <button
            onClick={() => { setTreeMenu(null); rebind(); }}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
          >Refresh</button>
          <button
            onClick={() => { setTreeMenu(null); setDrawerTab('files'); }}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
            title="Create a file — type its path in the box at the top of the tree"
          >New file...</button>
          <button
            onClick={() => { setTreeMenu(null); setCollapsed(new Set()); }}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
          >Collapse all folders</button>
          <button
            onClick={() => { setTreeMenu(null); setFilter(''); }}
            className="w-full text-left px-3 py-1.5 text-[#e8eaed] hover:bg-[#21262d]"
          >Clear the filter</button>
        </div>
      )}

      {/* ================= drawer: sessions + files ================= */}
      {drawerOpen ? (
        /* `max-w-[60vw]`: the auto-collapse below hides the drawer in a narrow
           window, but a viewport between that threshold and ~420px would still
           leave the composer cramped if the drawer always took its full 248px.
           Capping it means the working column always has room, whether or not
           the drawer is showing. */
        <div className="w-[248px] max-w-[60vw] border-r border-[#30363d] flex flex-col shrink-0 bg-[#0d1117]">
          <div className="p-3 border-b border-[#30363d] space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-[#e8eaed]">Code</span>
              <button
                onClick={() => { drawerToggled.current = true; setDrawerOpen(false); }}
                className="ml-auto text-[#8b949e] hover:text-[#e8eaed]"
                title="Hide the sidebar"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M9.78 12.78a.75.75 0 0 1-1.06 0L4.47 8.53a.75.75 0 0 1 0-1.06l4.25-4.25a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 1.042L6.06 8l3.72 3.72a.75.75 0 0 1 0 1.06Z" />
                </svg>
              </button>
            </div>
            {!bound ? (
              <>
                <input
                  value={workspacePath}
                  onChange={e => { touched.current = true; setWorkspacePath(e.target.value); }}
                  placeholder="Folder path..."
                  className="w-full bg-[#161b22] border border-[#30363d] rounded px-2 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"
                />
                <button
                  onClick={() => rebind()}
                  disabled={!workspacePath.trim() || binding || wsState !== 'connected'}
                  className="w-full bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-2 py-1.5 text-xs font-medium"
                >{binding ? 'Binding...' : 'Bind Workspace'}</button>
              </>
            ) : (
              <>
                <div className="flex items-center gap-1.5 text-xs">
                  <span className="text-[#8b949e] truncate" title={workspacePath}>
                    {workspacePath.split(/[\\/]/).filter(Boolean).pop()}
                  </span>
                  <button onClick={() => rebind()}
                    className="ml-auto text-[#8b949e] hover:text-[#58a6ff]"
                    title="Reload the file list">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                      <path d="M1.705 8.005a.75.75 0 0 1 .834.656 5.5 5.5 0 0 0 9.592 2.97l-1.204-1.204a.25.25 0 0 1 .177-.427h3.646a.25.25 0 0 1 .25.25v3.646a.25.25 0 0 1-.427.177l-1.38-1.38A7.002 7.002 0 0 1 1.05 8.84a.75.75 0 0 1 .656-.834ZM8 2.5a5.487 5.487 0 0 0-4.131 1.869l1.204 1.204A.25.25 0 0 1 4.896 6H1.25A.25.25 0 0 1 1 5.75V2.104a.25.25 0 0 1 .427-.177l1.38 1.38A7.002 7.002 0 0 1 14.95 7.16a.75.75 0 0 1-1.49.178A5.5 5.5 0 0 0 8 2.5Z" />
                    </svg>
                  </button>
                  <button
                    onClick={() => { setBound(false); setFiles([]); setTabs([]); setActivePath(''); }}
                    className="text-[#8b949e] hover:text-[#f85149]" title="Unbind">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                      <path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.749.749 0 0 1 1.275.326.749.749 0 0 1-.215.734L9.06 8l3.22 3.22a.749.749 0 0 1-.326 1.275.749.749 0 0 1-.734-.215L8 9.06l-3.22 3.22a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z" />
                    </svg>
                  </button>
                </div>
                <button
                  onClick={startSession}
                  className="w-full flex items-center gap-2 border border-[#30363d] hover:border-[#484f58] rounded px-2 py-1.5 text-xs text-[#c9d1d9]"
                  title="Start a new session in this workspace"
                >+ New session</button>
              </>
            )}
            {bindError && <p className="text-[11px] text-[#f85149]">{bindError}</p>}
          </div>

          <div className="flex border-b border-[#30363d] text-xs">
            <button
              onClick={() => setDrawerTab('sessions')}
              className={`flex-1 py-2 ${drawerTab === 'sessions'
                ? 'text-[#e8eaed] border-b-2 border-[#3380FF]'
                : 'text-[#8b949e] hover:text-[#e8eaed]'}`}
            >Sessions</button>
            <button
              onClick={() => setDrawerTab('files')}
              className={`flex-1 py-2 ${drawerTab === 'files'
                ? 'text-[#e8eaed] border-b-2 border-[#3380FF]'
                : 'text-[#8b949e] hover:text-[#e8eaed]'}`}
            >Files{files.length ? ` (${files.length})` : ''}</button>
            <button
              onClick={() => setDrawerTab('search')}
              className={`px-2 py-2 ${drawerTab === 'search'
                ? 'text-[#e8eaed] border-b-2 border-[#3380FF]'
                : 'text-[#8b949e] hover:text-[#e8eaed]'}`}
              title="Search the workspace"
            >Find</button>
            <button
              onClick={() => { setDrawerTab('changes'); refreshGit(); }}
              className={`px-2 py-2 ${drawerTab === 'changes'
                ? 'text-[#e8eaed] border-b-2 border-[#3380FF]'
                : 'text-[#8b949e] hover:text-[#e8eaed]'}`}
              title={gitStatus?.isRepo
                ? `git${gitStatus.branch ? ` on ${gitStatus.branch}` : ''} — what Addled changed`
                : 'What Addled changed in the workspace'}
            >Changes{gitStatus?.isRepo && gitStatus.changed?.length
              ? ` (${gitStatus.changed.length})` : ''}</button>
          </div>

          {drawerTab === 'sessions' ? (
            <div className="flex-1 overflow-y-auto py-1">
              {sessionList.pinned.length > 0 && (
                <>
                  <p className="px-3 py-1 text-[10px] text-[#484f58] uppercase tracking-wide">Pinned</p>
                  {sessionList.pinned.map(s => sessionRow(s, s.id === getActiveId()))}
                </>
              )}
              <p className="px-3 py-1 text-[10px] text-[#484f58] uppercase tracking-wide">Recents</p>
              {sessionList.recent.length === 0 && sessionList.pinned.length === 0 && (
                <p className="px-3 py-2 text-[11px] text-[#484f58]">
                  {bound
                    ? 'No sessions yet. Describe a change below and one is created for you.'
                    : 'Bind a workspace to start.'}
                </p>
              )}
              {sessionList.recent.map(s => sessionRow(s, s.id === getActiveId()))}
            </div>
          ) : drawerTab === 'files' ? (
            <>
              <div className="p-2 border-b border-[#30363d] space-y-2">
                <input
                  value={filter}
                  onChange={e => setFilter(e.target.value)}
                  placeholder="Filter files..."
                  className="w-full bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"
                />
                <div className="flex gap-1">
                  <input
                    value={newFile}
                    onChange={e => setNewFile(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') startNewFile(); }}
                    placeholder="new/file.py"
                    className="flex-1 min-w-0 bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] placeholder-[#484f58]"
                  />
                  <button
                    onClick={startNewFile}
                    disabled={!newFile.trim()}
                    className="text-xs px-2 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-40"
                    title="Create a file in the workspace"
                  >+</button>
                </div>
                {bound && !showAll && hiddenDirs.length > 0 && (
                  <p className="text-[10px] text-[#484f58]">
                    Hiding {hiddenDirs.length} folder{hiddenDirs.length === 1 ? '' : 's'}
                    {' ('}<span className="font-mono">{hiddenDirs.slice(0, 3).join(', ')}</span>
                    {hiddenDirs.length > 3 ? ', ...' : ''}{') — '}
                    <button
                      onClick={() => { setShowAll(true); rebind(true); }}
                      className="text-[#58a6ff] hover:underline"
                    >show all</button>
                  </p>
                )}
              </div>

              {/* Right-click on empty space gives the tree-level actions. */}
              <div
                className="flex-1 overflow-y-auto py-1"
                onContextMenu={e => {
                  e.preventDefault();
                  setTreeMenu({
                    x: Math.min(e.clientX, window.innerWidth - 208),
                    y: Math.min(e.clientY, window.innerHeight - 140),
                  });
                }}
              >
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
            </>
          ) : drawerTab === 'search' ? (
            <div className="flex-1 overflow-y-auto">{searchPanel}</div>
          ) : (
            <div className="flex-1 overflow-y-auto">{changesPanel}</div>
          )}
        </div>
      ) : (
        <div className="w-9 border-r border-[#30363d] flex flex-col items-center shrink-0">
          <button
            onClick={() => { drawerToggled.current = true; setDrawerOpen(true); }}
            className="w-9 py-2 text-[#8b949e] hover:text-[#e8eaed]"
            title="Show sessions and files"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className="mx-auto">
              <path d="M6.22 3.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L9.94 8 6.22 4.28a.75.75 0 0 1 0-1.06Z" />
            </svg>
          </button>
        </div>
      )}

      {/* ================= main column ================= */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar: what this session is working on. The chips are read-only
            summaries; clicking one goes to the place that owns the fact.

            No drawer toggle here. The drawer already owns one on each side (a
            `<<` in its header when open, a `>>` on the rail when closed), and a
            third in this bar rendered directly beneath the header's — two
            identical controls doing the same thing, one above the other. */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-[#30363d] shrink-0">
          <div className="min-w-0 flex-1">{contextChips}</div>
          {/* The file/chat toggle: the editor is one view of the session, and
              the transcript is the other. Both stay mounted over the same
              session so switching does not lose unsaved editor text. */}
          <div className="flex items-center gap-1 shrink-0 text-[11px]">
            <button
              onClick={() => setView('chat')}
              className={`px-2 py-1 rounded ${view === 'chat'
                ? 'bg-[#21262d] text-[#e8eaed]' : 'text-[#8b949e] hover:text-[#e8eaed]'}`}
            >Session</button>
            <button
              onClick={() => setView('editor')}
              className={`px-2 py-1 rounded ${view === 'editor'
                ? 'bg-[#21262d] text-[#e8eaed]' : 'text-[#8b949e] hover:text-[#e8eaed]'}`}
              title="The open files"
            >Editor{tabs.length ? ` (${tabs.length})` : ''}</button>
          </div>
        </div>

        {view === 'chat' ? (
          /* ---------------- the session transcript ---------------- */
          <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            {!activeSession || activeSession.messages.length === 0 ? (
              <div className="h-full flex items-center justify-center">
                <div className="text-center max-w-md">
                  <p className="text-sm text-[#8b949e]">
                    {bound
                      ? 'Describe a change. Addled works out which files to touch, then shows you a diff to approve.'
                      : 'Bind a workspace to start.'}
                  </p>
                  <p className="text-[11px] text-[#484f58] mt-2">
                    Paste a screenshot, drop a document, or type @ to name a file.
                  </p>
                </div>
              </div>
            ) : (
              activeSession.messages.map(msg => (
                <div key={msg.id}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm ${
                    msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-assistant'}`}>
                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className="mb-2 flex flex-wrap gap-1.5">
                        {msg.attachments.map((a, ai) => a.kind === 'image' && a.preview ? (
                          <img key={ai} src={a.preview} alt={a.name}
                            className="max-h-28 rounded border border-black/20" />
                        ) : (
                          <span key={ai}
                            className="text-[10px] px-2 py-0.5 rounded-full bg-black/20 opacity-90">
                            {a.name}
                          </span>
                        ))}
                      </div>
                    )}
                    <div className="whitespace-pre-wrap break-words">{msg.content}</div>
                    {msg.files && msg.files.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {msg.files.map(f => (
                          <button key={f} onClick={() => { open(f); setView('editor'); }}
                            className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-[#30363d] hover:border-[#58a6ff] hover:text-[#58a6ff]"
                            title={`Open ${f}`}
                          >{f}</button>
                        ))}
                      </div>
                    )}
                    {msg.note && (
                      <p className="text-[10px] mt-1.5 opacity-70">{msg.note}</p>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        ) : (
          /* ---------------- the editor ---------------- */
          <>
            {tabs.length > 0 && (
              <div className="flex items-stretch border-b border-[#30363d] bg-[#161b22] overflow-x-auto shrink-0">
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
                    {tab.content !== tab.saved && <span className="text-[#d29922]">*</span>}
                    <button
                      onClick={e => { e.stopPropagation(); closeTab(tab.path); }}
                      className="opacity-0 group-hover:opacity-100 text-[#8b949e] hover:text-[#f85149]"
                    >X</button>
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

                <div
                  className="flex-1 min-h-0"
                  onContextMenu={e => {
                    // Only offer the menu when something is actually selected:
                    // "Explain this" over a bare caret has nothing to explain.
                    if (!currentSelection()) return;
                    e.preventDefault();
                    setSelMenu({
                      x: Math.min(e.clientX, window.innerWidth - 220),
                      y: Math.min(e.clientY, window.innerHeight - 220),
                    });
                  }}
                >
                  <CodeMirror
                    key={activeTab.path}
                    value={activeTab.content}
                    height="100%"
                    theme="none"
                    extensions={[...extensionsFor(activeTab.language), oneDark, chrome, saveKeys,
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

                {pending && (
                  <div className="border-t border-[#30363d] bg-[#161b22] max-h-64 overflow-y-auto shrink-0">
                    <div className="flex items-center justify-between px-3 py-2 border-b border-[#21262d] sticky top-0 bg-[#161b22]">
                      <span className="text-xs text-[#8b949e]">
                        Proposed change to {pending.filePath}
                        <span className="text-[#3fb950]"> +{pending.diff?.added ?? 0}</span>
                        <span className="text-[#f85149]"> -{pending.diff?.removed ?? 0}</span>
                        <span className="text-[#484f58]"> · whole file</span>
                      </span>
                      <div className="flex gap-2">
                        <button onClick={applyEdit} disabled={Boolean(busy)}
                          className="text-xs px-2 py-1 rounded bg-[#238636] hover:bg-[#2ea043] disabled:opacity-50 text-white"
                          title="Write this whole change to disk. A backup is kept, and it can be undone.">
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
              </>
            ) : (
              <div className="flex-1 flex items-center justify-center text-[#8b949e]">
                <div className="text-center">
                  <p className="text-sm">No file open.</p>
                  <p className="text-[11px] text-[#484f58] mt-2">
                    Pick one from Files, or ask for a change and review its diff.
                  </p>
                </div>
              </div>
            )}
          </>
        )}

        {/* ================= composer ================= */}
        <div className="border-t border-[#30363d] shrink-0">
          {bindError && (
            <p className="px-3 pt-2 text-[11px] text-[#f85149]">
              {bindError} — pick another folder above, then try again.
            </p>
          )}

          {/* Attachments and mentions ride above the box, so what the request
              will carry is visible before it is sent. */}
          {(attachments.length > 0 || mentions.length > 0 || pullFrom) && (
            <div className="flex flex-wrap gap-1 px-3 pt-2">
              {attachments.map((a, i) => (
                <span key={`${a.name}-${i}`}
                  className="inline-flex items-center gap-1.5 bg-[#21262d] border border-[#30363d] rounded px-2 py-1 text-[11px] text-[#e8eaed]"
                  title={a.name}>
                  {a.kind === 'image' && a.preview
                    ? <img src={a.preview} alt={a.name} className="h-6 w-6 object-cover rounded" />
                    : <span>{a.kind === 'text' ? 'TXT' : 'FILE'}</span>}
                  <span className="max-w-[130px] truncate">{a.name}</span>
                  <button
                    onClick={() => removeAttachmentAt(i)}
                    className="text-[#8b949e] hover:text-[#f85149]"
                    title="Remove"
                  >X</button>
                </span>
              ))}
              {mentions.map(path => (
                <span key={path}
                  className="inline-flex items-center gap-1 bg-[#21262d] border border-[#30363d] rounded px-2 py-1 text-[10px] text-[#c9d1d9]"
                  title={path}>
                  @{path.split('/').pop()}
                  <button
                    onClick={() => setMentions(prev => prev.filter(p => p !== path))}
                    className="text-[#8b949e] hover:text-[#f85149]"
                    title="Remove this file"
                  >X</button>
                </span>
              ))}
              {pullFrom && activePath && (
                <span
                  className="inline-flex items-center gap-1 bg-[#1f6feb22] border border-[#1f6feb55] rounded px-2 py-1 text-[10px] text-[#58a6ff]"
                  title={`${activePath} lines ${pullFrom.from}-${pullFrom.to}`}>
                  {activePath.split('/').pop()}:{pullFrom.from}-{pullFrom.to}
                  <button
                    onClick={() => setPullFrom(null)}
                    className="text-[#8b949e] hover:text-[#f85149]"
                    title="Detach the selection"
                  >X</button>
                </span>
              )}
            </div>
          )}

          <div className="relative p-3">
            {/* @-mention menu */}
            {mentionQuery !== null && mentionMatches.length > 0 && (
              <div className="absolute z-20 bottom-full mb-1 w-full max-h-48 overflow-y-auto bg-[#161b22] border border-[#30363d] rounded shadow-lg">
              {mentionMatches.slice(0, 30).map(f => (
                <button
                  key={f.path}
                  onClick={() => {
                    setMentions(prev => prev.includes(f.path) ? prev : [...prev, f.path]);
                    setInstruction(prev => prev.replace(/@[^\s@]*$/, '').replace(/\s+$/, ''));
                    setMentionQuery(null);
                  }}
                  className="w-full text-left px-2 py-1 text-[11px] hover:bg-[#21262d] flex items-center gap-2"
                  title={f.path}
                >
                  <span className="w-[6px] h-[6px] rounded-full shrink-0"
                    style={{ background: langColor(f.language) }} />
                  <span className="truncate text-[#c9d1d9]">{f.path}</span>
                </button>
              ))}
            </div>
            )}

            {/* The plan, when one exists. Shown above the box because it is the
                thing being reviewed, and it is what the Apply buttons act on. */}
            {plan && (
              <div className="mb-2 border border-[#30363d] rounded bg-[#0d1117]">
                <div className="flex items-center justify-between px-2 py-1 border-b border-[#21262d]">
                  <span className="text-[10px] text-[#8b949e]">
                    Plan — {plan.steps.length} file{plan.steps.length === 1 ? '' : 's'}
                  </span>
                  <div className="flex items-center gap-2">
                    {plan.steps.length > 1 && plan.steps.length <= MANY_FILES && (
                      <button
                        onClick={generateAllDiffs}
                        disabled={Boolean(busy) || plan.steps.every(s => s.status === 'done')}
                        className="text-[10px] px-1.5 py-0.5 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-40"
                      >Generate all</button>
                    )}
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
                {plan.steps.length > MANY_FILES && (
                  <p className="px-2 py-1.5 text-[10px] text-[#d29922] border-b border-[#21262d]">
                    This plan touches {plan.steps.length} files. If that is more than
                    you expected, discard it and describe the change more narrowly.
                  </p>
                )}
                <div className="max-h-40 overflow-y-auto">
                  {plan.steps.map((step, i) => (
                    <div key={`${step.filePath}:${i}`}
                      className="px-2 py-1.5 border-b border-[#21262d] last:border-0">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => { open(step.filePath); setView('editor'); }}
                          className="text-[11px] text-[#58a6ff] hover:underline truncate font-mono"
                          title={`Open ${step.filePath}`}
                        >{step.filePath}</button>
                        <span className={`ml-auto shrink-0 text-[10px] ${
                          step.status === 'applied' || step.status === 'done' ? 'text-[#3fb950]'
                          : step.status === 'failed' ? 'text-[#f85149]'
                          : step.status === 'running' ? 'text-[#d29922]' : 'text-[#484f58]'}`}>
                          {step.status === 'applied' ? 'written'
                            : step.status === 'done' ? 'diff ready'
                            : step.status === 'failed' ? 'failed'
                            : step.status === 'running' ? 'working...'
                            : step.action || 'edit'}
                        </span>
                        <button
                          onClick={() => runPlanStep(i)}
                          disabled={Boolean(busy) || step.status === 'done'
                                    || step.status === 'applied'}
                          className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-40"
                        >{step.status === 'applied' ? 'written' : 'diff'}</button>
                      </div>
                      {step.reason && (
                        <p className="text-[10px] text-[#484f58] mt-0.5">{step.reason}</p>
                      )}
                    </div>
                  ))}
                </div>
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

            <div className="flex gap-2 items-end">
              <textarea
                value={instruction}
                onChange={e => {
                  const value = e.target.value;
                  setInstruction(value);
                  const caret = e.target.selectionStart ?? value.length;
                  const upto = value.slice(0, caret);
                  const at = upto.lastIndexOf('@');
                  const token = at === -1 ? null : upto.slice(at + 1);
                  setMentionQuery(at !== -1 && token !== null && !/\s/.test(token)
                    ? token : null);
                }}
                onPaste={onComposerPaste}
                onKeyDown={e => {
                  if (mentionQuery !== null && e.key === 'Escape') {
                    e.preventDefault(); setMentionQuery(null); return;
                  }
                  if (e.key === 'Enter' && !e.shiftKey) {
                    if (mentionQuery !== null && mentionMatches.length) {
                      e.preventDefault();
                      const f = mentionMatches[0];
                      setMentions(prev => prev.includes(f.path) ? prev : [...prev, f.path]);
                      setInstruction(prev => prev.replace(/@[^\s@]*$/, '').replace(/\s+$/, ''));
                      setMentionQuery(null);
                      return;
                    }
                    e.preventDefault(); makePlan();
                  }
                }}
                rows={3}
                placeholder={bound
                  ? 'Describe the change. Paste an image or drop a file. Type @ to name a file.'
                  : 'Bind a workspace first...'}
                className="flex-1 min-w-0 bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] resize-none focus:outline-none focus:border-[#3380FF]"
              />
              <button
                onClick={makePlan}
                disabled={!instruction.trim() || wsState !== 'connected' || !bound || planning}
                className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium shrink-0"
                title="Plan first — nothing is written until you approve a diff"
              >{planning ? '...' : 'Plan'}</button>
            </div>

            <div className="flex items-center gap-3 mt-1.5 text-[10px] text-[#484f58]">
              <label className="flex items-center gap-1 cursor-pointer">
                <input ref={fileInputRef} type="file" multiple className="hidden"
                  accept="image/*,.txt,.md,.json,.csv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.xml,.yaml,.yml,.ini,.cfg,.sh,.bat,.ps1,.toml,.sql,.pdf,.docx,.xlsx,.pptx"
                  onChange={e => { addFiles(e.target.files); e.target.value = ''; }} />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={wsState !== 'connected' || !bound}
                  className="hover:text-[#58a6ff] disabled:opacity-40"
                  title="Attach an image, text file or document"
                >Attach</button>
              </label>
              {activeTab && selectionAvailable && !pullFrom && (
                <button
                  onClick={() => setPullFrom(currentSelection())}
                  className="hover:text-[#58a6ff]"
                  title={`Attach the selected lines from ${activeTab.name}`}
                >Attach selection</button>
              )}
              {busy && !planning && plan && (
                <button
                  onClick={() => { cancelRef.current = true; setBusy('Cancelling...'); }}
                  className="hover:text-[#f85149]"
                  title="Stop after the diff in flight finishes — nothing is written"
                >Stop</button>
              )}
              <span className="ml-auto">
                Reads and searches the workspace and proposes edits as a diff you apply.
                It cannot run other tools — for those, use{' '}
                <a href="/chat" className="text-[#58a6ff] hover:underline">Chat</a>.
              </span>
            </div>
          </div>
        </div>

        {/* The last applied change, and its undo — the trust surface. An applied
            change is only safe to make if it is reversible, and both the commit
            and the verify verdict already existed and were being thrown away. */}
        {checkpoint && (
          <div className="flex items-center gap-2 px-3 py-1.5 border-t border-[#30363d] text-[11px] shrink-0 bg-[#161b22]">
            <span className="text-[#8b949e] min-w-0 truncate">
              {checkpoint.sha
                ? <>Committed <code className="text-[#58a6ff]">{checkpoint.sha.slice(0, 8)}</code></>
                : <>Changed {checkpoint.files.length} file{checkpoint.files.length === 1 ? '' : 's'} (no git — backups kept)</>}
            </span>
            <span className="shrink-0">{verifyBadge(checkpoint.verify)}</span>
            <button
              onClick={undoLastChange}
              disabled={undoing}
              className="ml-auto shrink-0 px-2 py-0.5 rounded border border-[#30363d] text-[#8b949e] hover:text-[#f85149] hover:border-[#f85149] disabled:opacity-40"
              title={checkpoint.sha
                ? 'Undo by reverting that commit — adds an inverse commit, never rewrites history'
                : 'Undo by restoring the .bak kept beside each file'}
            >{undoing ? 'Undoing...' : 'Undo this change'}</button>
            <button
              onClick={() => setCheckpoint(null)}
              disabled={undoing}
              className="shrink-0 text-[#484f58] hover:text-[#8b949e] disabled:opacity-40"
              title="Dismiss — leaves the change in place"
            >X</button>
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
