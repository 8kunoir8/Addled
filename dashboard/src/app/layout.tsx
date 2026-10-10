'use client';

import { useWS } from '@/lib/useWS';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState, useEffect } from 'react';
import {
  getApprovals, addApproval, addApprovals, dropApproval, subscribeApprovals,
  subscribeApprovalErrors, setApprovalError, makeAnswers,
  type ApprovalRequest,
} from '@/lib/approvalsStore';
import {
  addQuestion, removeQuestion, setQuestions, subscribeQuestions,
  subscribeQuestionErrors, makeQuestionAnswers, type QuestionRequest,
} from '@/lib/questionsStore';
import ApprovalCard from '@/components/ApprovalCard';
import QuestionCard from '@/components/QuestionCard';
import "./globals.css";

const NAV_ITEMS = [
  { href: '/chat', label: 'Chat', icon: '💬' },
  { href: '/memory', label: 'Memory', icon: '🧠' },
  { href: '/wiki', label: 'Wiki', icon: '📖' },
  { href: '/meetings', label: 'Meetings', icon: '🎙️' },
  { href: '/sop', label: 'Procedures', icon: '📋' },
  { href: '/skills', label: 'Skills', icon: '🧩' },
  { href: '/goals', label: 'Goals', icon: '🎯' },
  { href: '/code', label: 'Code', icon: '💻' },
  { href: '/swarm', label: 'Swarm', icon: '🐝' },
  { href: '/browser', label: 'Browser', icon: '🌐' },
  { href: '/calendar', label: 'Calendar', icon: '📅' },
  { href: '/bots', label: 'Bots', icon: '🤖' },
  { href: '/remote', label: 'Remote', icon: '📡' },
  { href: '/settings', label: 'Settings', icon: '⚙️' },
];

const STATE_LABELS: Record<string, string> = {
  idle: 'Idle 🟢',
  listening: 'Listening 🎤',
  observing: 'Observing 👁️',
  thinking: 'Thinking 🤔',
  has_suggestion: 'Has Suggestion 💡',
  acting: 'Acting ⚡',
  speaking: 'Speaking 🔊',
  sleeping: 'Sleeping 😴',
  blocked: 'Blocked 🚫',
  error: 'Error ❌',
  working: 'Working ⚙️',
  dreaming: 'Dreaming ✨',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { state: wsState, characterState, send, onNotification } = useWS();
  const [desktopPrompt, setDesktopPrompt] = useState(false);
  const [browserPrompt, setBrowserPrompt] = useState<string | null>(null);
  const [localPrompt, setLocalPrompt] = useState<any>(null);
  const [localProgress, setLocalProgress] = useState<any>(null);
  // A delegated swarm task that just finished, shown as a toast on pages that
  // are not the chat.
  const [swarmResult, setSwarmResult] =
    useState<{ agent: string; success: boolean; text: string } | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>(getApprovals);
  const [approvalErrors, setApprovalErrors] =
    useState<Record<string, string>>({});
  const [questions, setQuestionsState] = useState<QuestionRequest[]>([]);
  const [questionErrors, setQuestionErrors] =
    useState<Record<string, string>>({});

  // Desktop-control permission request from the backend
  useEffect(() => onNotification('desktop.permissionRequest', () => {
    setDesktopPrompt(true);
  }), [onNotification]);

  // A skill or tool is waiting on the user's permission.
  //
  // The layout owns this subscription because it is the only component that is
  // always mounted, and `onNotification` keeps exactly one handler per method:
  // a page-level subscriber would be replaced the moment another page mounted,
  // and a request raised in that gap would be lost. Pages read the store.
  useEffect(() => onNotification('action.approvalRequest', (p: any) => {
    if (!p?.approval_id) return;
    addApproval({
      approval_id: p.approval_id,
      action_type: p.action_type || '',
      kind: p.kind || 'action',
      name: p.name || p.action_type || 'a tool',
      grantable: Boolean(p.grantable),
      command: p.command || '',
    });
  }), [onNotification]);

  // An approval answered on ANOTHER surface — the bubble over the character, a
  // bot, or a second window. Every surface draws its own card from the same
  // request, so without this the card here would sit waiting for an answer that
  // has already been given, and pressing it would fail with "no such approval".
  //
  // Owned by the layout for the same reason the request subscription is: it is
  // always mounted and `onNotification` keeps one handler per method.
  useEffect(() => onNotification('action.approvalResolved', (p: any) => {
    if (p?.approval_id) dropApproval(String(p.approval_id));
  }), [onNotification]);

  useEffect(() => subscribeApprovals(setApprovals), []);
  useEffect(() => subscribeApprovalErrors(setApprovalErrors), []);
  useEffect(() => subscribeQuestions(setQuestionsState), []);
  useEffect(() => subscribeQuestionErrors(setQuestionErrors), []);

  // The model is asking the user something.
  //
  // Owned by the layout for the same reason as approvals: it is the only
  // component always mounted, and `onNotification` keeps ONE handler per
  // method, so a page-level subscriber would be replaced the moment another
  // page mounted and a question raised in that gap would be lost.
  useEffect(() => onNotification('question.ask', (p: any) => {
    if (!p?.question_id) return;
    addQuestion({
      question_id: p.question_id,
      question: p.question || '',
      options: Array.isArray(p.options) ? p.options : [],
      context: p.context || '',
      source: p.source || '',
      conversation: p.conversation || '',
      ttl: p.ttl || '',
      created: p.created,
    });
  }), [onNotification]);

  // A question answered on another surface stops showing here, so the same
  // question is never offered twice.
  useEffect(() => onNotification('question.settled', (p: any) => {
    if (p?.question_id) removeQuestion(p.question_id);
  }), [onNotification]);

  // Answer a question the same way the chat page does, so the two surfaces
  // cannot drift: `makeQuestionAnswers` is the one place that decides what is
  // sent, that the card is dropped only once the backend confirms, and that
  // answering resumes the work the question was blocking.
  const answerQuestion = makeQuestionAnswers(send);

  // Answer a request the same way the chat card does, so the two surfaces
  // cannot drift: `makeAnswers` in the store is the one place that decides what
  // "Allow once" and "Always allow" send, and that the card is dropped only
  // once the backend confirms.
  const answer = makeAnswers(send);

  // A request raised before the dashboard was open is not broadcast again, so
  // the list is re-read on every connect. This is the same recovery the local
  // model prompt uses, and for the same reason.
  useEffect(() => {
    if (wsState !== 'connected') return;
    let cancelled = false;
    (async () => {
      try {
        const r = await send('approvals.list', {});
        if (cancelled) return;
        const restored = (r?.pending || []).filter((p: any) => p?.approval_id);
        addApprovals(restored);
      } catch {
        /* nothing pending, or an older backend without the method */
      }
    })();
    return () => { cancelled = true; };
  }, [wsState, send]);

  // Questions are recovered the same way, and additionally *replace* the list
  // rather than merging into it: the backend is the authority on what is still
  // open, so a question it has already expired must disappear from the screen
  // instead of lingering as a card whose only possible answer is "too late".
  useEffect(() => {
    if (wsState !== 'connected') return;
    let cancelled = false;
    (async () => {
      try {
        const r = await send('questions.list', {});
        if (cancelled) return;
        setQuestions(r?.questions || []);
      } catch {
        /* no questions waiting, or an older backend without the method */
      }
    })();
    return () => { cancelled = true; };
  }, [wsState, send]);

  // Browser backend auto-install request
  useEffect(() => onNotification('browser.installRequest', (p: any) => {
    setBrowserPrompt(p?.backend || 'playwright');
  }), [onNotification]);

  // Local model download request (ask before downloading the configured GGUF)
  useEffect(() => onNotification('local.llmInstallRequest', (p: any) => {
    setLocalPrompt(p || {});
  }), [onNotification]);

  // A delegated swarm task finishing.
  //
  // Owned by the layout for the same reason approvals and questions are: it is
  // the only always-mounted component, and `onNotification` keeps ONE handler
  // per method, so a page-level subscriber would be replaced by whichever page
  // mounted next. The chat page ALSO listens, but only renders results from its
  // own source; this one is the toast for everywhere else, so finishing a task
  // from the Code page is not silent.
  useEffect(() => onNotification('swarm.agentResult', (p: any) => {
    if (!p?.agent) return;
    setSwarmResult({
      agent: String(p.agent),
      success: Boolean(p.success),
      text: String(p.text || ''),
    });
    // Long enough to read a short answer; the full text is on the Chat page and
    // on the agent's notebook, so the toast does not have to hold all of it.
    const t = setTimeout(() => setSwarmResult(null), 15000);
    return () => clearTimeout(t);
  }), [onNotification]);

  // Local model download progress
  useEffect(() => onNotification('local.llmProgress', (p: any) => {
    const phase = p?.phase || 'downloading';
    if (phase === 'done') { setLocalProgress(null); return; }
    setLocalProgress({ phase, pct: p?.pct ?? 0, detail: p?.detail || '' });
  }), [onNotification]);

  // The prompt must survive a missed broadcast (e.g. app started before the
  // dashboard was open) — re-check status whenever we connect.
  useEffect(() => {
    if (wsState !== 'connected') return;
    send('localLlm.status', {}).then((r: any) => {
      if (r?.should_ask) {
        setLocalPrompt({ size_mb: r.size_mb, model_file: r.model_file });
      }
      if (r?.downloading) {
        setLocalProgress({ phase: 'downloading', pct: r.progress ?? 0, detail: r.detail || '' });
      }
    }).catch(() => {});
  }, [wsState, send]);

  // Backend-initiated navigation (e.g. character right-click → Settings)
  useEffect(() => onNotification('ui.navigate', (p: any) => {
    let path = p?.path || '/';
    if (!path.startsWith('/')) path = '/' + path;
    if (typeof window !== 'undefined' && window.location.pathname !== path) {
      window.location.href = path;
    }
  }), [onNotification]);

  const wsStatusColor =
    wsState === 'connected' ? 'bg-green-500' :
    wsState === 'connecting' ? 'bg-yellow-500' : 'bg-red-500';

  return (
    <html lang="en" className="dark">
      <body className="flex h-screen overflow-hidden">
        {/* The window is frameless, so this shell IS the window chrome: one
            rounded surface holds the header, the rail and the page. */}
        <div className="bubble-shell flex flex-col w-full h-full">
          <WindowHeader
            wsStatusColor={wsStatusColor}
            wsState={wsState}
            characterState={characterState}
          />
          <div className="flex flex-1 min-h-0">
            <nav className="rail flex flex-col py-2 shrink-0" aria-label="Sections">
              <div className="flex-1 overflow-y-auto overflow-x-hidden">
                {NAV_ITEMS.map((item) => {
                  const isActive = pathname === item.href || pathname?.startsWith(item.href + '/');
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      title={item.label}
                      className={`rail-nav-item flex items-center gap-3 px-3 py-2 mx-1.5 my-0.5 text-sm ${isActive ? 'active' : ''}`}
                    >
                      <span className="text-base shrink-0 w-5 text-center">{item.icon}</span>
                      <span className="rail-label">{item.label}</span>
                    </Link>
                  );
                })}
              </div>
              <div className="border-t border-[#30363d] px-3 pt-2 pb-1 shrink-0">
                <div className="flex items-center gap-2 text-xs text-[#8b949e]">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${wsStatusColor}`} />
                  <span className="rail-label">
                    {STATE_LABELS[characterState] || characterState || 'Disconnected'}
                  </span>
                </div>
              </div>
            </nav>
            <main className="flex-1 min-w-0 overflow-hidden flex flex-col">
              {children}
            </main>
          </div>

        {/* Permission request, for pages that are not the chat.
            The chat renders its own copy in the message stream, next to the
            turn that asked, so showing this there as well would ask twice. */}
        {pathname !== '/chat' && approvals.length > 0 && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm">
            {approvals.slice(0, 3).map((req) => (
              <ApprovalCard
                key={req.approval_id}
                request={req}
                compact
                error={approvalErrors[req.approval_id]}
                onAllow={answer(req).allow}
                onAlwaysAllow={answer(req).always}
                onAllowForSession={answer(req).session}
                onDeny={answer(req).deny}
              />
            ))}
            {approvals.length > 3 && (
              <p className="text-[10px] text-[#8b949e] text-right">
                +{approvals.length - 3} more waiting
              </p>
            )}
          </div>
        )}

        {/* A question the model asked, for pages that are not the chat.
            Same rule as approvals: the chat renders its own copy in the message
            stream beside the turn that asked, so showing this there too would
            ask twice. Rendered for every page because a question raised from a
            background turn has no page of its own to appear on. */}
        {pathname !== '/chat' && questions.length > 0 && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm">
            {questions.slice(0, 2).map((q) => (
              <QuestionCard
                key={q.question_id}
                question={q}
                compact
                error={questionErrors[q.question_id]}
                onAnswer={answerQuestion(q).answer}
                onDismiss={answerQuestion(q).dismiss}
              />
            ))}
            {questions.length > 2 && (
              <p className="text-[10px] text-[#8b949e] text-right">
                +{questions.length - 2} more waiting
              </p>
            )}
          </div>
        )}

        {/* Desktop-control permission prompt */}
        {desktopPrompt && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border border-[#d29922] bg-[#161b22] p-4 shadow-xl">
            <p className="text-sm font-semibold text-[#e8eaed]">🖱 Desktop control requested</p>
            <p className="text-xs text-[#8b949e] mt-1">
              The agent wants to move the mouse / type on your desktop. Grant access for this session only?
              <br /><span className="text-[#484f58]">Emergency stop: mouse to top-left corner, or Ctrl+Shift+Alt+K.</span>
            </p>
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => { send('desktop.grant', {}).catch(() => {}); setDesktopPrompt(false); }}
                className="flex-1 bg-[#3380FF] hover:bg-[#4d94ff] text-white rounded-md py-1.5 text-sm font-medium">
                Allow this session
              </button>
              <button
                onClick={() => { send('desktop.revoke', {}).catch(() => {}); setDesktopPrompt(false); }}
                className="flex-1 bg-[#3d1f1f] hover:bg-[#5a2a2a] text-[#f85149] rounded-md py-1.5 text-sm font-medium">
                Deny
              </button>
            </div>
          </div>
        )}

        {/* Browser backend install prompt */}
        {browserPrompt && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border border-[#3380FF] bg-[#161b22] p-4 shadow-xl">
            <p className="text-sm font-semibold text-[#e8eaed]">⬇ Install {browserPrompt}?</p>
            <p className="text-xs text-[#8b949e] mt-1">
              Addled needs <span className="font-mono text-[#58a6ff]">{browserPrompt}</span> for this task
              {browserPrompt === 'playwright' ? ' (Playwright + Chromium, ~170 MB)' : ' (the browser-use framework)'}.
              Install it now?
            </p>
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => { send('browser.installApprove', { backend: browserPrompt }).catch(() => {}); setBrowserPrompt(null); }}
                className="flex-1 bg-[#3380FF] hover:bg-[#4d94ff] text-white rounded-md py-1.5 text-sm font-medium">
                Install
              </button>
              <button
                onClick={() => setBrowserPrompt(null)}
                className="flex-1 bg-[#21262d] hover:bg-[#30363d] text-[#8b949e] rounded-md py-1.5 text-sm font-medium">
                Not now
              </button>
            </div>
          </div>
        )}

        {/* Local model download prompt — nothing downloads without consent */}
        {localPrompt && (
          <div className="fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border border-[#8957e5] bg-[#161b22] p-4 shadow-xl">
            <p className="text-sm font-semibold text-[#e8eaed]">🧠 Download the local AI model?</p>
            <p className="text-xs text-[#8b949e] mt-1">
              Addled can run{' '}
              <span className="font-mono text-[#58a6ff]">{localPrompt.model_file || 'a local model'}</span>{' '}
              on this PC with llamafile — no API key and it works offline. About{' '}
              {localPrompt.size_mb ? `${(localPrompt.size_mb / 1024).toFixed(1)} GB` : '5.0 GB'} to download.
            </p>
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => { send('localLlm.installApprove', {}).catch(() => {}); setLocalPrompt(null); }}
                className="flex-1 bg-[#3380FF] hover:bg-[#4d94ff] text-white rounded-md py-1.5 text-sm font-medium">
                Download
              </button>
              <button
                onClick={() => { send('localLlm.installDecline', {}).catch(() => {}); setLocalPrompt(null); }}
                className="flex-1 bg-[#21262d] hover:bg-[#30363d] text-[#8b949e] rounded-md py-1.5 text-sm font-medium">
                No thanks
              </button>
            </div>
          </div>
        )}

        {/* A delegated swarm task finishing — a toast everywhere except the
            chat, which files the full answer into the conversation itself. */}
        {swarmResult && pathname !== '/chat' && (
          <div className="fixed bottom-4 right-4 z-40 w-80 rounded-lg border border-[#8957e5] bg-[#161b22] p-3 shadow-xl">
            <p className="text-xs font-semibold text-[#e8eaed]">
              {swarmResult.success ? `🐝 ${swarmResult.agent} finished`
                                    : `⚠ ${swarmResult.agent} could not finish`}
            </p>
            <p className="mt-1.5 text-[11px] text-[#8b949e] whitespace-pre-wrap break-words">
              {swarmResult.text.length > 260
                ? swarmResult.text.slice(0, 260) + '…'
                : swarmResult.text}
            </p>
            <div className="mt-2 flex gap-2">
              <Link href="/chat"
                    className="text-[10px] text-[#58a6ff] hover:text-[#4d94ff]">
                Open in Chat
              </Link>
              <button onClick={() => setSwarmResult(null)}
                      className="text-[10px] text-[#8b949e] hover:text-[#e8eaed]">
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Local model download progress */}
        {localProgress && (
          <div className="fixed bottom-4 right-4 z-40 w-72 rounded-lg border border-[#30363d] bg-[#161b22] p-3 shadow-xl">
            <p className="text-xs font-semibold text-[#e8eaed]">
              {localProgress.phase === 'failed' ? '⚠ Download failed'
                : localProgress.phase === 'cancelled' ? 'Download cancelled'
                : '⬇ Downloading local model'}
            </p>
            <div className="mt-2 h-1.5 w-full rounded bg-[#21262d] overflow-hidden">
              <div className="h-full bg-[#3380FF] transition-all"
                   style={{ width: `${Math.min(100, Math.max(0, localProgress.pct))}%` }} />
            </div>
            <p className="mt-1.5 text-[10px] text-[#8b949e] truncate">
              {localProgress.detail} · {Math.round(localProgress.pct)}%
            </p>
            {(localProgress.phase === 'failed' || localProgress.phase === 'cancelled') && (
              <button onClick={() => setLocalProgress(null)}
                      className="mt-2 text-[10px] text-[#8b949e] hover:text-[#e8eaed]">
                Dismiss
              </button>
            )}
          </div>
        )}
        </div>
      </body>
    </html>
  );
}

/**
 * The window's title bar, drawn by us because the window is frameless.
 *
 * It replaces the OS title bar AND Electron's default application menu, both of
 * which the window used to inherit: `electron-builder`/Electron gave every
 * window a File/Edit/View/Window/Help menu because nothing called
 * `Menu.setApplicationMenu`. None of those items were Addled's — they were
 * Electron's stock template — so the header carries only what this app actually
 * needs, and the menu is removed in `electron/main.js`.
 *
 * The header is the drag region, which is why `-webkit-app-region: drag` is on
 * `.bubble-header` and every control inside opts out with `no-drag`: without
 * that, the buttons would be dead, because Electron routes a press inside a
 * drag region to the window move instead of the button.
 */
function WindowHeader({
  wsStatusColor, wsState, characterState,
}: {
  wsStatusColor: string;
  wsState: string;
  characterState?: string;
}) {
  const api = typeof window !== 'undefined' ? (window as any).electronAPI : undefined;
  // Absent when this runs in a plain browser (`npm run dev` for the dashboard
  // alone, or the Next dev server), where there is no frameless window to
  // control. The buttons then do nothing rather than throwing.
  const minimise = () => api?.minimiseWindow?.();
  const maximise = () => api?.toggleMaximiseWindow?.();
  const close = () => api?.closeWindow?.();

  // Deliberately NOT a second title bar.
  //
  // Every page under `app/*/page.tsx` already renders its own `<h1>` and its own
  // connection state at the top of `<main>` (chat, memory, wiki, skills, goals,
  // bots, browser, calendar, swarm, remote all do). Putting the same words here
  // as well printed each page's name twice, one above the other — which is what
  // the first version of this header did. So this bar carries only what the
  // pages cannot: the app's identity, and the window controls, which exist
  // nowhere else because the window has no OS frame.
  //
  // The state dot and character state stay because they are *window*-level — a
  // glance from any page — and are not repeated per page the way the title is.
  return (
    <div className="bubble-header flex items-center gap-2 px-3 shrink-0">
      <span className="text-sm">⬡</span>
      <span className="font-semibold text-sm">Addled</span>

      {/* Only when something is wrong. A permanent "Connected" here was noise
          the pages already report, but a failure has to be visible from
          anywhere — a disconnected app looks identical to an idle one
          otherwise. */}
      {wsState !== 'connected' && (
        <span className="text-[11px] text-[#f85149]">
          {wsState === 'connecting' ? 'Connecting…'
            : <>Backend not running · <code className="text-[#58a6ff]">python backend/main.py</code></>}
        </span>
      )}

      {/* Pushes the window controls to the right edge. */}
      <span className="flex-1" />

      <span className={`w-2 h-2 rounded-full ${wsStatusColor}`} />
      <span className="text-[11px] text-[#8b949e] mr-1">
        {STATE_LABELS[characterState || ''] || characterState || ''}
      </span>

      <button className="win-btn" onClick={minimise} title="Minimise" aria-label="Minimise">
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <rect x="1" y="4.5" width="8" height="1" fill="currentColor" />
        </svg>
      </button>
      <button className="win-btn" onClick={maximise} title="Maximise" aria-label="Maximise">
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <rect x="1.5" y="1.5" width="7" height="7" fill="none"
                stroke="currentColor" strokeWidth="1" />
        </svg>
      </button>
      {/* Hides to the tray, it does not quit — the same thing the window's own
          close button did before the frame was removed (`mainWindow.on('close')`
          in electron/main.js hides unless `app.isQuitting`). Routing this to
          `app.quit()` would have silently turned "close" into "exit", losing the
          tray behaviour. */}
      <button className="win-btn win-btn-close" onClick={close} title="Close to tray" aria-label="Close">
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <path d="M1.5 1.5 L8.5 8.5 M8.5 1.5 L1.5 8.5"
                stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  );
}
