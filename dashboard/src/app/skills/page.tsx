'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

interface SkillInfo {
  name: string;
  category: string;
  description: string;
  requires_approval: boolean;
  /** Granted standing permission, so it does not ask any more. */
  always_allowed: boolean;
  /**
   * May this be remembered permanently?
   *
   * False for an action whose danger is in what it is handed rather than what
   * it is — `run_command` judges the command text against a list of
   * unrecoverable ones. A grant is keyed by name, so remembering it would
   * allow every command it will ever carry.
   */
  grantable: boolean;
  /** May it be allowed until Addled restarts? True for anything that asks. */
  session_grantable?: boolean;
  /** Already allowed for this session. */
  session_allowed?: boolean;
  /** Was allowed, but its files changed since, so it asks again. */
  code_changed?: boolean;
  enabled: boolean;
  deletable: boolean;
  source: string;
}

interface Candidate {
  name: string;
  repo: string;
  description: string;
  similarity: number;
}

const SOURCE_LABELS: Record<string, string> = {
  builtin: 'built-in',
  forged: 'forged',
  market: 'market',
  mcp: 'MCP',
};

export default function SkillsPage() {
  const { state: wsState, send } = useWS();
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [installInput, setInstallInput] = useState('');
  const [marketQuery, setMarketQuery] = useState('');
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [searching, setSearching] = useState(false);

  const load = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('skills.list', {});
      setSkills(r?.skills || []);
    } catch { /* backend offline */ }
    setLoading(false);
  }, [wsState, send]);

  useEffect(() => { load(); }, [load]);

  const toggle = async (name: string, enabled: boolean) => {
    setMsg(null);
    const r = await send('skills.setState', { name, enabled });
    setMsg(r?.success ? null : { ok: false, text: r?.error || 'Toggle failed' });
    load();
  };

  const remove = async (name: string) => {
    if (!confirm(`Delete skill "${name}"?`)) return;
    setMsg(null);
    const r = await send('skills.delete', { name });
    setMsg(r?.success
      ? { ok: true, text: `Deleted "${name}"` }
      : { ok: false, text: r?.error || 'Delete failed' });
    load();
  };

  // Standing permission: on means the skill never asks again, off puts the
  // prompt back. Written to the same place the approval card writes, so the
  // switch and the card cannot disagree.
  const setAlwaysAllowed = async (name: string, allowed: boolean) => {
    setMsg(null);
    const r = allowed
      ? await send('approvals.alwaysAllow', { kind: 'skill', name })
      : await send('approvals.revoke', { kind: 'skill', name });
    setMsg(r?.success
      ? { ok: true, text: allowed
          ? `"${name}" will not ask again until you turn this off`
          : `"${name}" will ask before it runs` }
      : { ok: false, text: r?.error || 'Could not change the permission' });
    load();
  };

  // The session answer, for the actions a permanent grant is refused for. It
  // lasts until Addled restarts and is never written to disk, which is what
  // makes it safe to give to something that judges each request on its
  // contents: the allowed scope ends with the sitting.
  const setSessionAllowed = async (name: string, allowed: boolean) => {
    setMsg(null);
    const r = allowed
      ? await send('approvals.alwaysAllow',
                   { kind: 'skill', name, session: true })
      : await send('approvals.revokeSession', { kind: 'skill', name });
    setMsg(r?.success
      ? { ok: true, text: allowed
          ? `"${name}" will not ask again until Addled restarts`
          : `"${name}" will ask before it runs` }
      : { ok: false, text: r?.error || 'Could not change the permission' });
    load();
  };

  const install = async () => {
    const v = installInput.trim();
    if (!v) return;
    setMsg(null);
    const r = v.startsWith('http')
      ? await send('skills.installFrom', { url: v })
      : await send('skills.installFrom', { repo: v });
    setMsg(r?.success
      ? { ok: true, text: `Installed "${r.skill}"` + (r.scripts?.length ? ` (${r.scripts.length} script(s))` : '') }
      : { ok: false, text: r?.error || 'Install failed' });
    setInstallInput('');
    load();
  };

  const searchMarket = async () => {
    const q = marketQuery.trim();
    if (!q) return;
    setSearching(true);
    setMsg(null);
    try {
      const r = await send('skills.searchMarket', { query: q });
      setCandidates(r?.results || []);
      if (!r?.success) setMsg({ ok: false, text: r?.error || 'Search failed' });
    } catch (e: any) {
      setMsg({ ok: false, text: e?.message || 'Search failed' });
    }
    setSearching(false);
  };

  const installCandidate = async (c: Candidate) => {
    setMsg(null);
    const r = await send('skills.installFrom', { repo: c.repo });
    setMsg(r?.success
      ? { ok: true, text: `Installed "${r.skill}" from ${c.repo}` }
      : { ok: false, text: r?.error || 'Install failed' });
    load();
  };

  const groups = skills.reduce<Record<string, SkillInfo[]>>((acc, s) => {
    (acc[s.category] = acc[s.category] || []).push(s);
    return acc;
  }, {});

  return (
    <div className="flex flex-col h-full">
      <header className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h1 className="text-sm font-semibold">Skills</h1>
        <span className="text-xs text-[#8b949e]">
          {wsState === 'connected' ? `${skills.length} skills · ${skills.filter(s => s.enabled).length} enabled` : 'Connecting…'}
        </span>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
        {/* Install + market search */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="rounded-lg border border-[#30363d] bg-[#161b22] p-4">
            <h2 className="text-sm font-semibold text-[#e8eaed] mb-1">📥 Install a skill</h2>
            <p className="text-xs text-[#8b949e] mb-3">
              Paste a raw SKILL.md URL or a GitHub repo like <span className="font-mono">owner/repo</span>. Skills with scripts need opt-in before they can run.
            </p>
            <div className="flex gap-2">
              <input
                value={installInput}
                onChange={(e) => setInstallInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') install(); }}
                placeholder="https://…/SKILL.md  or  owner/repo"
                className="flex-1 bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF] font-mono text-xs"
              />
              <button onClick={install} disabled={!installInput.trim()}
                className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium">
                Install
              </button>
            </div>
          </div>

          <div className="rounded-lg border border-[#30363d] bg-[#161b22] p-4">
            <h2 className="text-sm font-semibold text-[#e8eaed] mb-1">🔎 Search skill markets</h2>
            <p className="text-xs text-[#8b949e] mb-3">
              Finds SKILL.md skills in public GitHub markets (topic: claude-skills) and ranks them locally.
            </p>
            <div className="flex gap-2">
              <input
                value={marketQuery}
                onChange={(e) => setMarketQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') searchMarket(); }}
                placeholder="e.g. pdf processing, image compression"
                className="flex-1 bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"
              />
              <button onClick={searchMarket} disabled={!marketQuery.trim() || searching}
                className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium">
                {searching ? '⟳' : 'Search'}
              </button>
            </div>
            {candidates.length > 0 && (
              <div className="mt-3 space-y-1.5">
                {candidates.map((c) => (
                  <div key={c.repo} className="flex items-center justify-between gap-2 bg-[#0d1117] border border-[#21262d] rounded-md px-3 py-2">
                    <div className="min-w-0">
                      <p className="text-xs text-[#e8eaed] truncate">{c.name} <span className="text-[#484f58] font-mono">({c.repo})</span></p>
                      {c.description && <p className="text-[10px] text-[#8b949e] truncate">{c.description}</p>}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-[10px] text-[#d29922]">{Math.round((c.similarity || 0) * 100)}%</span>
                      <button onClick={() => installCandidate(c)}
                        className="text-xs px-2 py-1 rounded bg-[#21262d] hover:bg-[#30363d] text-[#3380FF]">
                        Install
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        {msg && <p className={`text-xs ${msg.ok ? 'text-[#3fb950]' : 'text-[#f85149]'}`}>{msg.text}</p>}

        {loading && <p className="text-sm text-[#8b949e]">Loading skills…</p>}

        {/* Skill groups */}
        {Object.entries(groups).map(([category, list]) => (
          <section key={category}>
            <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
              {category} ({list.length})
            </h2>
            <div className="space-y-1.5">
              {list.map((s) => (
                <div key={s.name}
                  className={`flex items-start gap-3 rounded-lg border p-3 ${s.enabled ? 'border-[#30363d] bg-[#161b22]' : 'border-[#21262d] bg-[#0d1117] opacity-70'}`}>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-[#e8eaed] font-mono">{s.name}</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff]">
                        {SOURCE_LABELS[s.source] || s.source}
                      </span>
                      {s.requires_approval && !s.always_allowed && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#2d1f1f] text-[#f0883e]">⚠ approval</span>
                      )}
                      {s.always_allowed && s.requires_approval && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#12261e] text-[#3fb950]">✓ always allowed</span>
                      )}
                      {s.session_allowed && !s.always_allowed && s.requires_approval && (
                        <span
                          title="Allowed until Addled restarts. It is not written to disk, so the prompt comes back when the app does."
                          className="text-[10px] px-1.5 py-0.5 rounded bg-[#2d2416] text-[#d29922] cursor-help">
                          ✓ allowed this session
                        </span>
                      )}
                      {s.code_changed && (
                        <span
                          title="You allowed this skill, but its files are not the ones you allowed — reinstalling or editing a skill clears its permission on purpose."
                          className="text-[10px] px-1.5 py-0.5 rounded bg-[#2d2416] text-[#d29922] cursor-help">
                          ⚠ code changed
                        </span>
                      )}
                      {!s.enabled && <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#30363d] text-[#8b949e]">off</span>}
                    </div>
                    {s.description && <p className="text-xs text-[#8b949e] mt-1 break-words">{s.description}</p>}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {/* Only shown for a skill that actually asks. A switch on
                        every row would read as "allow this skill to run",
                        which is the enable toggle beside it. */}
                    {s.requires_approval && (
                      s.grantable ? (
                        <button
                          onClick={() => setAlwaysAllowed(s.name, !s.always_allowed)}
                          title={s.always_allowed
                            ? 'Stop asking — click to make it ask again'
                            : 'Never ask for this skill again'}
                          aria-label={s.always_allowed
                            ? `Make ${s.name} ask again`
                            : `Always allow ${s.name}`}
                          className={`text-[10px] px-2 py-1 rounded border transition-colors ${
                            s.always_allowed
                              ? 'border-[#3fb95055] bg-[#12261e] text-[#3fb950]'
                              : 'border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] hover:border-[#484f58]'
                          }`}>
                          {s.always_allowed ? '✓ Always' : 'Always allow'}
                        </button>
                      ) : (
                        // Not permanently grantable, but still answerable —
                        // for a session. Previously this was a dead label
                        // saying "always asks", which left the user with no
                        // way to stop a safe command prompting every time.
                        <button
                          onClick={() => setSessionAllowed(s.name, !s.session_allowed)}
                          title={s.session_allowed
                            ? 'Allowed until Addled restarts — click to make it ask again'
                            : 'Never ask again until Addled restarts. This one judges each request on what it carries, so it cannot be remembered permanently.'}
                          aria-label={s.session_allowed
                            ? `Make ${s.name} ask again`
                            : `Allow ${s.name} for this session`}
                          className={`text-[10px] px-2 py-1 rounded border transition-colors ${
                            s.session_allowed
                              ? 'border-[#d2992255] bg-[#2d2416] text-[#d29922]'
                              : 'border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] hover:border-[#484f58]'
                          }`}>
                          {s.session_allowed ? '✓ Session' : 'Allow session'}
                        </button>
                      )
                    )}
                    <button onClick={() => toggle(s.name, !s.enabled)}
                      title={s.enabled ? 'Turn off' : 'Turn on'}
                      className={`w-9 h-5 rounded-full transition-colors ${s.enabled ? 'bg-[#3380FF]' : 'bg-[#30363d]'}`}>
                      <div className={`w-4 h-4 bg-white rounded-full transition-transform ${s.enabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
                    </button>
                    {s.deletable && (
                      <button onClick={() => remove(s.name)} title="Delete skill"
                        className="text-[#8b949e] hover:text-[#f85149] text-sm px-1">✕</button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
