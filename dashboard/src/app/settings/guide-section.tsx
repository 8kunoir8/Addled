'use client';

/**
 * Settings → Guide.
 *
 * Explains what every feature is for and how to use it. Deliberately not a
 * second copy of Settings: each entry says what the feature does, links to the
 * page or tab where it is used, and shows a badge only when this machine is
 * missing something the feature needs.
 *
 * The prose is static and always readable; only the badges, the skill catalogue
 * and the machine summary need the backend. So the guide still works when the
 * backend is offline — it just stops claiming to know anything about the
 * machine.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { APP_VERSION, GUIDE, LIMITS } from '@/lib/guide-content';
import type { GuideEntry, GuideNeed, GuideStatus } from '@/lib/guide-content';

interface SkillInfo {
  name: string;
  category: string;
  description: string;
  enabled: boolean;
  source: string;
}

const CARD = 'rounded-lg border border-[#30363d] bg-[#161b22] p-4';

export default function GuideSection(
  { send, connected }: { send: (m: string, p?: any) => Promise<any>; connected: boolean },
) {
  const [status, setStatus] = useState<GuideStatus | null>(null);
  const [statusMissing, setStatusMissing] = useState(false);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState(GUIDE[0]?.id || '');
  const refs = useRef<Record<string, HTMLElement | null>>({});

  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    (async () => {
      try {
        const result = await send('guide.status', {});
        if (!cancelled && result) setStatus(result as GuideStatus);
      } catch {
        // Expected right after deploying the dashboard ahead of a restart: the
        // running backend is an older build and does not know this method.
        if (!cancelled) setStatusMissing(true);
      }
      try {
        const result = await send('skills.list', {});
        if (!cancelled) setSkills(result?.skills || []);
      } catch { /* the catalogue is a bonus */ }
    })();
    return () => { cancelled = true; };
  }, [connected, send]);

  // ---- search ------------------------------------------------------------

  const needle = query.trim().toLowerCase();

  const shown = useMemo(() => {
    if (!needle) return GUIDE;
    return GUIDE.filter(entry =>
      [entry.title, entry.summary, entry.what, entry.id,
        ...(entry.how || []), ...(entry.skills || []), ...(entry.settingsTabs || [])]
        .join(' ').toLowerCase().includes(needle));
  }, [needle]);

  /** Named skills the search found, even when their section is not a match. */
  const skillHits = useMemo(() => {
    if (!needle || needle.length < 2) return [];
    return skills
      .filter(s => s.name.toLowerCase().includes(needle)
        || (s.description || '').toLowerCase().includes(needle))
      .slice(0, 12);
  }, [needle, skills]);

  const goTo = useCallback((id: string) => {
    setActiveId(id);
    refs.current[id]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  // Keep the rail in step with the reader. Guarded because a scroll-spy that
  // throws would take the whole tab with it.
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const nodes = shown
      .map(entry => refs.current[entry.id])
      .filter((node): node is HTMLElement => Boolean(node));
    if (!nodes.length) return;
    const observer = new IntersectionObserver(entries => {
      const first = entries
        .filter(entry => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      const id = first?.target instanceof HTMLElement ? first.target.dataset.guideId : '';
      if (id) setActiveId(id);
    }, { rootMargin: '-90px 0px -60% 0px', threshold: 0 });
    nodes.forEach(node => observer.observe(node));
    return () => observer.disconnect();
  }, [shown]);

  // ---- machine summary ---------------------------------------------------

  const machineRows = status ? [
    { label: 'AI provider', ok: status.any_model_ready,
      detail: status.providers_configured
        ? `${status.providers_configured} of ${status.providers_total} ready`
        : 'none configured' },
    { label: 'Local model', ok: status.local_model_running,
      detail: status.local_model_running ? 'running'
        : status.local_model_installed ? 'downloaded, not running' : 'not installed' },
    { label: 'Skills', ok: status.skills_total > 0, detail: `${status.skills_total} available` },
    { label: 'Workspace', ok: status.workspace_configured,
      detail: status.workspace_enforced ? 'bound, writes confined'
        : status.workspace_configured ? 'bound' : 'not bound' },
    { label: 'MCP', ok: status.mcp_connected > 0,
      detail: status.mcp_servers
        ? `${status.mcp_connected} of ${status.mcp_servers} connected · ${status.mcp_tools} tools`
        : 'no servers added' },
    { label: 'Browser automation', ok: status.browser_automation,
      detail: status.browser_automation ? 'Playwright installed'
        : 'Playwright missing — click and type unavailable' },
    { label: 'Desktop control', ok: status.desktop_input_allowed,
      detail: status.desktop_input_allowed ? 'allowed' : 'off' },
    { label: 'Remote access', ok: status.remote_enabled,
      detail: status.remote_enabled
        ? (status.tailscale_installed ? 'on · Tailscale ready' : 'on · Tailscale missing')
        : 'off' },
    { label: 'Voice', ok: status.stt_available && status.tts_available,
      detail: `${status.stt_available ? 'speech in' : 'no speech in'} · `
        + `${status.tts_available ? 'speech out' : 'no speech out'}` },
    { label: 'Bots', ok: status.bots_ready > 0,
      detail: status.bots_ready ? `${status.bots_ready} ready` : 'none set up' },
  ] : [];

  const skillsByCategory = useMemo(() => {
    const grouped: Record<string, SkillInfo[]> = {};
    for (const skill of skills) {
      const key = skill.category || 'other';
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(skill);
    }
    return Object.entries(grouped)
      .sort((a, b) => a[0].localeCompare(b[0]));
  }, [skills]);

  const badge = (entry: GuideEntry, need: GuideNeed) => {
    if (!status) return null;
    if (status[need.flag] === need.want) return null;
    return (
      <div key={need.flag}
        className="mt-3 flex items-start gap-2 rounded-md border border-[#5a4a1d] bg-[#2a2410] px-3 py-2">
        <span className="text-[#d29922] text-xs shrink-0">⚠</span>
        <p className="text-xs text-[#d29922]">{need.label}</p>
      </div>
    );
  };

  const skillChip = (name: string) => {
    const live = skills.find(s => s.name === name);
    return (
      <span
        key={name}
        title={live?.description || 'Not in the current catalogue'}
        className={`text-[10px] px-1.5 py-0.5 rounded font-mono ${
          live
            ? (live.enabled ? 'bg-[#1f2937] text-[#58a6ff]' : 'bg-[#30363d] text-[#8b949e]')
            : 'bg-[#30363d] text-[#484f58]'
        }`}
      >
        {name}{live && !live.enabled ? ' · off' : ''}
      </span>
    );
  };

  return (
    <div className="space-y-4">
      {/* ---- header ---- */}
      <div className="max-w-3xl">
        <p className="text-xs text-[#8b949e] px-1">
          What Addled can do, and how to use each part of it. Every feature here links to the
          page you use it from or the Settings tab that changes it, so this page explains
          rather than duplicates. Version {APP_VERSION}.
        </p>
      </div>

      <input
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder="Search the guide — a feature, a setting, or a skill name…"
        className="w-full max-w-3xl bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-sm text-[#e8eaed] placeholder-[#484f58] focus:outline-none focus:border-[#3380FF]"
      />

      {/* ---- this machine ---- */}
      {status && (
        <div className={`${CARD} max-w-3xl`}>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-3">
            On this machine
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
            {machineRows.map(row => (
              <div key={row.label}
                className="flex items-start gap-2 py-1.5 border-b border-[#21262d] last:border-0">
                <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${
                  row.ok ? 'bg-[#3fb950]' : 'bg-[#d29922]'}`} />
                <div className="min-w-0">
                  <p className="text-xs text-[#e8eaed]">{row.label}</p>
                  <p className="text-[11px] text-[#8b949e] truncate">{row.detail}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {!status && (
        <p className="max-w-3xl text-xs text-[#d29922]">
          {!connected
            ? 'Live status is unavailable because the backend is not connected. The explanations below are unaffected.'
            : statusMissing
              ? 'Live status is unavailable: this backend does not answer guide.status yet, so the badges and the machine summary are missing. Restarting Addled is what loads new backend code. The explanations below are unaffected.'
              : 'Live status is unavailable. The explanations below are unaffected.'}
        </p>
      )}

      <div className="flex gap-6 items-start">
        {/* ---- rail ---- */}
        <nav className="hidden xl:block w-52 shrink-0">
          <div className="sticky top-0 space-y-0.5">
            {shown.map(entry => (
              <button
                key={entry.id}
                onClick={() => goTo(entry.id)}
                className={`w-full text-left px-2 py-1.5 rounded-md text-xs transition-colors ${
                  activeId === entry.id
                    ? 'bg-[#1f6feb] text-white'
                    : 'text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]'}`}
              >
                <span className="mr-1.5">{entry.icon}</span>{entry.title}
              </button>
            ))}
            {!shown.length && (
              <p className="px-2 text-[11px] text-[#484f58]">Nothing matches.</p>
            )}
          </div>
        </nav>

        {/* ---- entries ---- */}
        <div className="flex-1 min-w-0 max-w-3xl space-y-5">
          {skillHits.length > 0 && (
            <div className={CARD}>
              <h2 className="text-xs font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
                Skills matching “{query.trim()}”
              </h2>
              {skillHits.map(skill => (
                <div key={skill.name} className="py-1.5 border-b border-[#21262d] last:border-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono text-[#e8eaed]">{skill.name}</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#1f2937] text-[#58a6ff]">
                      {skill.category}
                    </span>
                    {!skill.enabled && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#30363d] text-[#8b949e]">
                        off
                      </span>
                    )}
                  </div>
                  {skill.description && (
                    <p className="text-xs text-[#8b949e] mt-0.5 break-words">{skill.description}</p>
                  )}
                </div>
              ))}
            </div>
          )}

          {!shown.length && (
            <p className="text-sm text-[#8b949e]">
              No feature matches “{query.trim()}”. Try a shorter word, or clear the box to see
              everything.
            </p>
          )}

          {shown.map(entry => (
            <section
              key={entry.id}
              data-guide-id={entry.id}
              ref={node => { refs.current[entry.id] = node; }}
              className={`${CARD} scroll-mt-4`}
            >
              <h2 className="text-sm font-semibold text-[#e8eaed]">
                <span className="mr-2">{entry.icon}</span>{entry.title}
              </h2>
              <p className="text-xs text-[#8b949e] mt-0.5">{entry.summary}</p>

              <p className="text-sm text-[#c9d1d9] mt-3">{entry.what}</p>

              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[#8b949e] mt-4 mb-1.5">
                How to use it
              </h3>
              <ol className="space-y-1.5">
                {entry.how.map((step, index) => (
                  <li key={index} className="flex gap-2 text-sm text-[#c9d1d9]">
                    <span className="text-[#484f58] shrink-0 w-4 text-right">{index + 1}.</span>
                    <span>{step}</span>
                  </li>
                ))}
              </ol>

              {(entry.needs || []).map(need => badge(entry, need))}

              {(entry.routes || entry.settingsTabs) && (
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <span className="text-[10px] uppercase tracking-wide text-[#484f58]">Where</span>
                  {(entry.routes || []).map(route => (
                    <Link key={route.href} href={route.href}
                      className="text-[11px] px-2 py-1 rounded border border-[#30363d] text-[#58a6ff] hover:border-[#484f58]">
                      Open {route.label} →
                    </Link>
                  ))}
                  {(entry.settingsTabs || []).map(tab => (
                    <Link key={tab} href={`/settings?section=${tab}`}
                      className="text-[11px] px-2 py-1 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed] hover:border-[#484f58]">
                      Settings → {tab.charAt(0).toUpperCase() + tab.slice(1)}
                    </Link>
                  ))}
                </div>
              )}

              {(entry.skills || []).length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] uppercase tracking-wide text-[#484f58]">Skills</span>
                  {(entry.skills || []).map(skillChip)}
                </div>
              )}

              {entry.showSkillCatalogue && (
                <div className="mt-4">
                  <h3 className="text-[10px] font-semibold uppercase tracking-wide text-[#8b949e] mb-2">
                    Everything installed
                    {skills.length > 0 && (
                      <span className="ml-2 normal-case tracking-normal text-[#484f58]">
                        {skills.length} skills · {skills.filter(s => s.enabled).length} enabled
                      </span>
                    )}
                  </h3>
                  {!skills.length && (
                    <p className="text-xs text-[#8b949e]">
                      The catalogue needs the backend. Open the Skills page to see it, or reconnect.
                    </p>
                  )}
                  <div className="space-y-3">
                    {skillsByCategory.map(([category, list]) => (
                      <div key={category}>
                        <p className="text-[11px] text-[#58a6ff] mb-1">
                          {category} <span className="text-[#484f58]">({list.length})</span>
                        </p>
                        {list.map(skill => (
                          <div key={skill.name}
                            className="py-1 border-b border-[#21262d] last:border-0">
                            <div className="flex items-center gap-2">
                              <span className="text-xs font-mono text-[#e8eaed]">{skill.name}</span>
                              {!skill.enabled && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#30363d] text-[#8b949e]">
                                  off
                                </span>
                              )}
                            </div>
                            {skill.description && (
                              <p className="text-[11px] text-[#8b949e] break-words">
                                {skill.description}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </section>
          ))}

          {/* ---- honest limits ---- */}
          {!needle && (
            <section className={`${CARD} border-[#30363d]`}>
              <h2 className="text-sm font-semibold text-[#e8eaed]">Known limits</h2>
              <p className="text-xs text-[#8b949e] mt-0.5">
                Worth knowing before you plan around any of the above.
              </p>
              <ul className="mt-3 space-y-2">
                {LIMITS.map((limit, index) => (
                  <li key={index} className="flex gap-2 text-sm text-[#c9d1d9]">
                    <span className="text-[#484f58] shrink-0">•</span>
                    <span>{limit}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
