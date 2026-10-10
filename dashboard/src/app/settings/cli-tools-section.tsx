'use client';

import { useState, useEffect, useCallback } from 'react';
import type {
  CliToolApplyResult,
  CliToolDetailResult,
  CliToolDraftResult,
  CliToolListResult,
  CliToolSuggestResult,
  CliToolSummary,
  CliToolTestResult,
} from '@/lib/ws-types';

/**
 * CLI Tools — ask for a capability, review what was written, then keep it.
 *
 * The page has one job and it is deliberately slow in the middle. Asking for a
 * tool produces SOURCE, not a saved tool, and the source is shown before
 * anything is written. That is the whole reason `draft` and `apply` are two
 * RPCs: code that runs on the user's machine should be read by the user first,
 * and a page that hid the middle step would be fetching and running code the
 * way the market does — only without the review that makes it acceptable.
 *
 * The three states the page moves through are the point:
 *   list   → what you have built, and whether it is any use (the run stats)
 *   draft  → what the model wrote, editable, not yet saved
 *   detail → what a saved tool is, its source, and a button that runs it
 */

/** The RPC surface these handlers expose. Narrower than the generic send. */
type Send = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;

export default function CliToolsSection({ send, connected }: {
  send: Send;
  connected: boolean;
}) {
  const [tools, setTools] = useState<CliToolSummary[]>([]);
  const [directory, setDirectory] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // The one field that matters: what the tool should do, in the user's words.
  const [capability, setCapability] = useState('');
  const [slug, setSlug] = useState('');
  const [slugTouched, setSlugTouched] = useState(false);
  const [drafting, setDrafting] = useState(false);
  // Seconds spent on the current draft. A draft takes 15-45s and the wait is
  // unbounded, so a spinner that never says how long it has been running is
  // indistinguishable from one that has hung.
  const [draftSeconds, setDraftSeconds] = useState(0);
  const [draft, setDraft] = useState<CliToolDraftResult | null>(null);
  const [source, setSource] = useState('');
  const [saving, setSaving] = useState(false);

  const [openSlug, setOpenSlug] = useState<string | null>(null);
  const [openSource, setOpenSource] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<CliToolTestResult | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    if (!connected) return;
    setLoading(true);
    try {
      const r = await send<CliToolListResult>('cliTools.list', {});
      setTools(r?.tools || []);
      setDirectory(r?.directory || '');
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not list the tools.');
    }
    setLoading(false);
  }, [connected, send]);

  useEffect(() => { refresh(); }, [refresh]);

  // A deep link from chat carries the capability the agent could not serve —
  // `?section=cli-tools&prefill=...` — so the box arrives filled with the words
  // the user already used rather than empty.
  useEffect(() => {
    const prefill = new URLSearchParams(window.location.search).get('prefill');
    if (!prefill) return;
    setCapability(prefill);
    send<CliToolSuggestResult>('cliTools.suggest', { capability: prefill })
      .then(r => { if (r?.slug) setSlug(r.slug); })
      .catch(() => {});
  }, [send]);

  // The slug follows the description as it is typed, until the user edits it.
  // A second name to invent is friction on a form whose point is the
  // description, and the backend derives the same thing when it is left blank.
  useEffect(() => {
    if (slugTouched || !capability.trim() || !connected) return;
    const t = setTimeout(() => {
      send<CliToolSuggestResult>('cliTools.suggest', { capability })
        .then(r => { if (r?.slug) setSlug(r.slug); })
        .catch(() => {});
    }, 400);
    return () => clearTimeout(t);
  }, [capability, slugTouched, connected, send]);

  const doDraft = async () => {
    if (!capability.trim() || !connected) return;
    setDrafting(true);
    setDraftSeconds(0);
    setError('');
    setTestResult(null);
    // Counts up while the model works. Cleared in the `finally` so a draft that
    // throws cannot leave the timer running forever.
    const started = Date.now();
    const ticker = setInterval(
      () => setDraftSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    try {
      const r = await send<CliToolDraftResult>('cliTools.draft', {
        capability, slug: slug.trim() || undefined,
      });
      if (!r?.success) {
        setError(r?.detail || r?.error || 'The draft failed.');
        setDraft(null);
      } else {
        setDraft(r);
        setSource(r.source || '');
        if (r.slug) setSlug(r.slug);
        if (r.warnings?.length) setNotice(r.warnings.join(' '));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The draft failed.');
    } finally {
      clearInterval(ticker);
      setDrafting(false);
    }
  };

  const doSave = async () => {
    if (!draft || !connected) return;
    setSaving(true);
    setError('');
    try {
      const r = await send<CliToolApplyResult>('cliTools.apply', {
        spec: draft.spec, source, slug: draft.slug || slug,
      });
      if (!r?.success) {
        setError(r?.error || 'Could not save the tool.');
      } else {
        setNotice(`Saved ${r.name}. It is available now, everywhere.`);
        setDraft(null);
        setSource('');
        setCapability('');
        setSlug('');
        setSlugTouched(false);
        await refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the tool.');
    }
    setSaving(false);
  };

  const doTest = async (target: string) => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await send<CliToolTestResult>('cliTools.test', { slug: target });
      setTestResult(r || { success: false, error: 'no result' });
    } catch (e) {
      setTestResult({
        success: false,
        error: e instanceof Error ? e.message : 'the test failed',
      });
    }
    setTesting(false);
  };

  const doRemove = async (target: string) => {
    setRemoving(target);
    try {
      const r = await send<CliToolApplyResult>('cliTools.remove', {
        slug: target, deleteFiles: true,
      });
      if (!r?.success) setError(r?.error || 'Could not remove the tool.');
      else {
        setNotice(`Removed ${target}.`);
        if (openSlug === target) setOpenSlug(null);
        await refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not remove the tool.');
    }
    setRemoving(null);
  };

  const open = async (t: CliToolSummary) => {
    setOpenSlug(t.slug);
    setOpenSource('');
    setTestResult(null);
    try {
      const r = await send<CliToolDetailResult>('cliTools.get', { slug: t.slug });
      setOpenSource(r?.tool?.source || '');
    } catch {
      setOpenSource('');
    }
  };

  if (!connected) {
    return <p className="text-sm text-[#8b949e]">Connecting…</p>;
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <p className="text-sm text-[#8b949e]">
        Ask for a tool and Addled will write one. You see the code before it is
        saved, and once saved it is a skill like any other — chat, the Code page
        and your swarm agents all use it. Tools you build are preferred over
        anything downloaded.
      </p>

      {error && (
        <div className="text-sm rounded-md border border-[#f85149] bg-[#3d1a1a] px-3 py-2 text-[#f85149]">
          {error}
        </div>
      )}
      {notice && (
        <div className="text-sm rounded-md border border-[#30363d] bg-[#161b22] px-3 py-2 text-[#8b949e]">
          {notice}
        </div>
      )}

      {/*
        The draft was RUN before it was shown, so this reports what happened
        rather than what might. Kept separate from `notice` deliberately: every
        other notice describes the tool, and this one is evidence about it —
        the difference between "this should work" and "this was run".
      */}
      {draft?.test_result && (
        <div
          className={`text-sm rounded-md border px-3 py-2 ${
            draft.test_result.success
              ? 'border-[#238636] bg-[#0f2417] text-[#3fb950]'
              : 'border-[#9e6a03] bg-[#2a2000] text-[#d29922]'
          }`}
        >
          {draft.test_result.success ? (
            'Ran it before showing you: the test invocation succeeded.'
          ) : draft.test_result.fixture ? (
            <>
              Ran it, and it could not use the sample value the test provided
              {draft.test_result.error
                ? `: ${draft.test_result.error}`
                : '.'}{' '}
              That is usually the test&rsquo;s fault rather than the code&rsquo;s
              — the tool may work when called with a real path. You can edit and
              save it below.
            </>
          ) : (
            `Ran it before showing you, and it FAILED${
              draft.test_result.error ? `: ${draft.test_result.error}` : '.'
            } You can still edit the code below and save it.`
          )}
        </div>
      )}

      {/* ── Ask for one ─────────────────────────────────────────────── */}
      <div className="border border-[#30363d] rounded-md p-4 space-y-3">
        <label className="block text-sm font-medium">What should the tool do?</label>
        <textarea
          value={capability}
          onChange={e => setCapability(e.target.value)}
          rows={3}
          placeholder="e.g. turn a folder of phone photos into resized PNGs"
          className="w-full bg-[#0d1117] border border-[#30363d] rounded-md px-3 py-2 text-sm"
        />
        <div className="flex items-center gap-2">
          <label className="text-xs text-[#8b949e] w-16">Name</label>
          <input
            value={slug}
            onChange={e => { setSlug(e.target.value); setSlugTouched(true); }}
            placeholder="derived from the description"
            className="flex-1 bg-[#0d1117] border border-[#30363d] rounded-md px-3 py-1.5 text-sm"
          />
        </div>
        <button
          onClick={doDraft}
          disabled={drafting || !capability.trim()}
          className="px-3 py-1.5 rounded-md bg-[#1f6feb] text-white text-sm disabled:opacity-50"
        >
          {drafting ? 'Writing…' : 'Write the tool'}
        </button>
        {drafting && (
          <div className="space-y-1.5" role="status" aria-live="polite">
            {/* An indeterminate bar, not a percentage: the backend reports no
                progress, and a number that crawls to 90% and then stops would
                be worse than saying plainly that it is working. The bar is
                full-width and translated, which is what reads as motion. */}
            <div className="h-1 w-full overflow-hidden rounded-full bg-[#21262d]">
              <div className="cli-progress-bar h-full w-1/3 rounded-full
                              bg-[#1f6feb]" />
            </div>
            <p className="text-xs text-[#8b949e]">
              {draftSeconds < 3
                ? 'Writing the tool…'
                : `Still writing… ${draftSeconds}s. Larger tools take longer.`}
            </p>
            <p className="text-xs text-[#8b949e]">
              Nothing is saved until you review it.
            </p>
          </div>
        )}
      </div>

      {/* ── Review what was written ─────────────────────────────────── */}
      {draft && (
        <div className="border border-[#1f6feb] rounded-md p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">Review before saving</h3>
            {draft.package && (
              <span className="text-xs text-[#8b949e]">
                needs the {draft.package} package
              </span>
            )}
          </div>
          <textarea
            value={source}
            onChange={e => setSource(e.target.value)}
            rows={18}
            spellCheck={false}
            className="w-full bg-[#0d1117] border border-[#30363d] rounded-md px-3 py-2 text-xs font-mono"
          />
          <p className="text-xs text-[#8b949e]">
            This is what will run on your machine. Edit it if you want — your
            version is what gets saved.
          </p>
          <div className="flex gap-2">
            <button
              onClick={doSave}
              disabled={saving}
              className="px-3 py-1.5 rounded-md bg-[#238636] text-white text-sm disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save this tool'}
            </button>
            <button
              onClick={() => { setDraft(null); setSource(''); }}
              className="px-3 py-1.5 rounded-md border border-[#30363d] text-sm"
            >
              Discard
            </button>
          </div>
        </div>
      )}

      {/* ── What you have ───────────────────────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-medium">
            Your tools {tools.length > 0 && <span className="text-[#8b949e]">({tools.length})</span>}
          </h3>
          {directory && (
            <span className="text-xs text-[#8b949e] font-mono" title={directory}>
              {directory}
            </span>
          )}
        </div>

        {loading && <p className="text-sm text-[#8b949e]">Loading…</p>}
        {!loading && tools.length === 0 && (
          <p className="text-sm text-[#8b949e] border border-dashed border-[#30363d] rounded-md px-3 py-6 text-center">
            No tools yet. Describe one above and Addled will write it.
          </p>
        )}

        <div className="space-y-2">
          {tools.map(t => (
            <div key={t.slug} className="border border-[#30363d] rounded-md">
              <div className="flex items-start justify-between px-3 py-2">
                <div className="min-w-0">
                  <div className="text-sm font-medium font-mono">{t.name}</div>
                  <div className="text-xs text-[#8b949e] truncate">{t.description}</div>
                  <div className="text-xs text-[#8b949e] mt-0.5">
                    {t.stats?.runs
                      ? `used ${t.stats.runs} time${t.stats.runs === 1 ? '' : 's'}`
                      : 'never used yet'}
                    {t.stats?.lastError ? ` · last error: ${t.stats.lastError.slice(0, 80)}` : ''}
                  </div>
                </div>
                <div className="flex gap-1 shrink-0 ml-2">
                  <button
                    onClick={() => (openSlug === t.slug ? setOpenSlug(null) : open(t))}
                    className="px-2 py-1 text-xs rounded-md border border-[#30363d]"
                  >
                    {openSlug === t.slug ? 'Close' : 'Open'}
                  </button>
                  <button
                    onClick={() => doTest(t.slug)}
                    disabled={testing}
                    className="px-2 py-1 text-xs rounded-md border border-[#30363d] disabled:opacity-50"
                  >
                    {testing ? '…' : 'Test'}
                  </button>
                  <button
                    onClick={() => doRemove(t.slug)}
                    disabled={removing === t.slug}
                    className="px-2 py-1 text-xs rounded-md border border-[#f85149] text-[#f85149] disabled:opacity-50"
                  >
                    {removing === t.slug ? '…' : 'Delete'}
                  </button>
                </div>
              </div>

              {openSlug === t.slug && (
                <div className="border-t border-[#30363d] px-3 py-2 space-y-2">
                  <pre className="text-xs font-mono bg-[#0d1117] rounded-md p-2 overflow-auto max-h-80">
                    {openSource || 'Reading…'}
                  </pre>
                  <p className="text-xs text-[#8b949e]">
                    You can also run it yourself: <span className="font-mono">
                      python {directory ? directory + '\\' + t.slug + '\\tool.py' : 'tool.py'} --help
                    </span>
                  </p>
                </div>
              )}
            </div>
          ))}
        </div>

        {testResult && (
          <div className={`mt-2 text-xs rounded-md border px-3 py-2 font-mono whitespace-pre-wrap ${
            testResult.success
              ? 'border-[#238636] bg-[#0d1117] text-[#3fb950]'
              : 'border-[#f85149] bg-[#0d1117] text-[#f85149]'
          }`}>
            {testResult.success
              ? `It works.\n${(testResult.stdout || '').slice(0, 1500)}`
              : `It failed.\n${(testResult.error || '').slice(0, 1500)}`}
          </div>
        )}
      </div>
    </div>
  );
}
