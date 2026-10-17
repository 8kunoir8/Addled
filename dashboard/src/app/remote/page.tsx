'use client';

import { useCallback, useEffect, useState } from 'react';
import { useWS } from '@/lib/useWS';

interface Session {
  id: string;
  browser?: string;
  remote_addr?: string;
  tailscale_user?: string;
  tailscale_device?: string;
  idle_s?: number;
  expires_in_s?: number;
}

interface Serve {
  configured?: boolean;
  funnel?: boolean;
  url?: string;
  target?: string;
}

interface Tailscale {
  installed?: boolean;
  version?: string;
  backend_state?: string;
  running?: boolean;
  logged_in?: boolean;
  needs_login?: boolean;
  dns_name?: string;
  tailnet?: string;
  ips?: string[];
  peers_online?: number;
  peer_count?: number;
  serve?: Serve;
  blockers?: string[];
  error?: string;
  login?: { state?: string; url?: string; error?: string };
  cli?: string;
  needs_enablement?: boolean;
  enable_url?: string;
}

interface Installer {
  phase?: string;
  pct?: number;
  detail?: string;
  error?: string;
  method?: string;
  preferred?: string;
  running?: boolean;
  installed?: boolean;
  winget?: boolean;
  can_install?: boolean;
}

interface RemoteStatus {
  enabled?: boolean;
  running?: boolean;
  port?: number;
  bind?: string;
  password_set?: boolean;
  sessions?: Session[];
  session_count?: number;
  bridges?: number;
  dashboard_found?: boolean;
  remote_url?: string;
  remote_session?: boolean;
  blockers?: string[];
  error?: string;
  tailscale?: Tailscale;
  installer?: Installer;
}

const DOT = (tone: 'ok' | 'warn' | 'off') =>
  tone === 'ok' ? 'bg-[var(--success)]' : tone === 'warn' ? 'bg-[var(--warning)]' : 'bg-[var(--text-muted)]';

function Row({ label, children, description }: any) {
  return (
    <div className="flex items-start justify-between gap-6 py-2.5 border-b border-[var(--bg-tertiary)] last:border-0">
      <div className="min-w-0">
        <div className="text-sm text-[var(--text-primary)]">{label}</div>
        {description && <div className="text-xs text-[var(--text-secondary)] mt-0.5">{description}</div>}
      </div>
      <div className="text-sm text-[var(--text-secondary)] shrink-0 text-right">{children}</div>
    </div>
  );
}

function Card({ title, children, right }: any) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] mb-4">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
        <h2 className="text-sm font-semibold text-[var(--text-primary)]">{title}</h2>
        {right}
      </div>
      <div className="px-4 py-1">{children}</div>
    </div>
  );
}

export default function RemotePage() {
  const { state: wsState, send, onNotification } = useWS();
  const [status, setStatus] = useState<RemoteStatus | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [freshPassword, setFreshPassword] = useState('');
  const [authKey, setAuthKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [loginUrl, setLoginUrl] = useState('');
  const [confirmInstall, setConfirmInstall] = useState(false);
  const [installPhase, setInstallPhase] = useState('');
  const [installPct, setInstallPct] = useState(0);
  const [installDetail, setInstallDetail] = useState('');
  const [installError, setInstallError] = useState('');
  // Whether the backend has answered at least once. Without it the cards below
  // render their fallbacks — "Not installed", "Missing", "Off" — which read as
  // real answers. That is how a correctly detected Tailscale install looked
  // broken, so nothing is shown until there is something true to show.
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('remote.status', {});
      setStatus(r || null);
      setLoaded(true);
      if (r?.tailscale?.login?.url) setLoginUrl(r.tailscale.login.url);
    } catch (e: any) {
      setError(e.message);
    }
  }, [wsState, send]);

  // Poll while something is transitional (signing in, or a gateway that is
  // meant to be up but is not yet), so the page stays live without hammering
  // the CLI the rest of the time.
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const busyish =
      status?.tailscale?.login?.state === 'starting' ||
      status?.tailscale?.login?.state === 'waiting' ||
      (status?.enabled && !status?.running);
    if (!busyish) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [status, load]);

  // The sign-in URL arrives as a push, because `tailscale up` waits on a browser.
  useEffect(() => onNotification('tailscale.loginUrl', (p: any) => {
    if (p?.url) setLoginUrl(p.url);
  }), [onNotification]);
  useEffect(() => onNotification('tailscale.status', () => { load(); }), [load, onNotification]);

  // The install runs in the background on the backend, so progress arrives as a
  // push rather than as the reply to the click that started it.
  useEffect(() => onNotification('tailscale.installProgress', (p: any) => {
    if (!p) return;
    setInstallPhase(p.phase || '');
    setInstallPct(p.pct ?? 0);
    setInstallDetail(p.detail || '');
    if (p.phase === 'failed') setInstallError(p.error || 'The install failed.');
    if (p.phase === 'done') { setConfirmInstall(false); setInstallError(''); }
  }), [onNotification]);

  // A page opened mid-install should show where things got to.
  const installState = status?.installer;
  useEffect(() => {
    if (!installState?.phase) return;
    setInstallPhase(installState.phase);
    setInstallPct(installState.pct ?? 0);
    setInstallDetail(installState.detail || '');
    if (installState.phase === 'failed') {
      setInstallError(installState.error || 'The install failed.');
    }
  }, [installState?.phase, installState?.pct, installState?.detail, installState?.error]);

  const act = async (label: string, method: string, params?: any) => {
    setBusy(label); setError(''); setNote('');
    try {
      const r = await send(method, params || {});
      if (r && r.success === false) setError(r.error || 'That did not work.');
      else if (r?.message) setNote(r.message);
      else if (r?.warning) setError(r.warning);
      await load();
      return r;
    } catch (e: any) { setError(e.message); return null; }
    finally { setBusy(''); }
  };

  const toggle = async (section: string, key: string, value: any) => {
    setBusy(`${section}.${key}`); setError('');
    try {
      const r = await send('settings.set', { section, key, value });
      if (r?.warning) setError(r.warning);
      await load();
    } catch (e: any) { setError(e.message); }
    setBusy('');
  };

  const generate = async () => {
    const r = await act('gen', 'remote.generatePassword');
    if (r?.password) setFreshPassword(r.password);
  };

  const startInstall = async () => {
    setInstallError('');
    setInstallPhase('checking');
    setInstallPct(0);
    setInstallDetail('');
    setConfirmInstall(false);
    try {
      const r = await send('tailscale.install', {});
      if (r && r.success === false) {
        setInstallError(r.error || 'Could not start the install.');
        setInstallPhase('');
      }
    } catch (e: any) {
      setInstallError(e.message);
      setInstallPhase('');
    }
    load();
  };

  const ts = status?.tailscale || {};
  const serve = ts.serve || {};
  const blockers: string[] = [...(status?.blockers || []), ...(ts.blockers || [])];
  const url = status?.remote_url || serve.url || '';
  const installer = status?.installer || {};
  const isRemoteSession = status?.remote_session === true;
  const installUsesWinget = installer.preferred === 'winget';
  const installing = !!installer.running ||
    ['checking', 'downloading', 'verifying', 'launching', 'waiting'].includes(installPhase);

  const stateTone: 'ok' | 'warn' | 'off' =
    status?.running && serve.configured ? 'ok'
      : ts.installed && ts.logged_in ? 'warn' : 'off';

  if (!loaded) {
    return (
      <div className="p-6 max-w-3xl">
        <h1 className="text-lg font-semibold mb-1">Remote access</h1>
        <p className="text-sm text-[var(--text-secondary)] mb-6">
          Reach this Addled from your phone or another machine, in a browser,
          over Tailscale.
        </p>
        <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-4">
          <div className="text-sm text-[var(--text-primary)] mb-1">
            {wsState === 'connected'
              ? 'Reading remote status…'
              : 'Waiting for Addled to connect…'}
          </div>
          <div className="text-xs text-[var(--text-secondary)]">
            {error || 'This page fills in once Addled answers.'}
          </div>
          <button onClick={load}
            className="mt-3 px-3 py-1.5 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
            Try again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-6 max-w-3xl">
      <div className="flex items-center gap-3 mb-1">
        <span className={`w-2.5 h-2.5 rounded-full ${DOT(stateTone)}`} />
        <h1 className="text-lg font-semibold">Remote access</h1>
      </div>
      <p className="text-sm text-[var(--text-secondary)] mb-6">
        Reach this Addled from your phone or another machine, in a browser, over
        Tailscale. Nothing is on the public internet unless you turn that on.
      </p>

      {error && <div className="mb-4 text-sm text-[var(--danger)] bg-[#2d1113] border border-[#5a1d1d] rounded px-3 py-2">{error}</div>}
      {note && <div className="mb-4 text-sm text-[var(--success)] bg-[#0f2a17] border border-[#1d5a2d] rounded px-3 py-2">{note}</div>}

      {blockers.length > 0 && (
        <div className="mb-4 rounded-lg border border-[#5a4a1d] bg-[#2a2410] px-4 py-3">
          <div className="text-xs font-semibold text-[var(--warning)] mb-1">
            {ts.needs_enablement ? 'One thing left, and it needs you' : 'Not reachable yet'}
          </div>
          <ul className="text-xs text-[var(--warning)] list-disc pl-4 space-y-0.5">
            {blockers.map((b, i) => <li key={i}>{b}</li>)}
          </ul>
          {ts.needs_enablement && (
            <div className="mt-3 pt-3 border-t border-[#5a4a1d]">
              <div className="text-xs text-[var(--text-secondary)] mb-2">
                Sharing to a tailnet is a one-time switch that only the tailnet
                owner can accept, in a browser. Addled cannot press it for you —
                but once you have, it finishes the rest on its own within a
                minute, without you coming back here.
              </div>
              {ts.enable_url ? (
                <a href={ts.enable_url} target="_blank" rel="noreferrer"
                   className="inline-block px-3 py-1.5 rounded bg-[var(--accent)] text-white text-xs">
                  Enable Tailscale Serve
                </a>
              ) : (
                <span className="text-xs text-[var(--text-secondary)]">
                  Open the Tailscale admin console and enable Serve / HTTPS for
                  this tailnet.
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {url && (
        <div className="mb-4 rounded-lg border border-[#1d5a2d] bg-[#0f2a17] px-4 py-3">
          <div className="text-xs text-[var(--success)] mb-1">Open this on any device on your tailnet</div>
          <div className="flex items-center gap-3">
            <a href={url} target="_blank" rel="noreferrer"
               className="text-sm text-[var(--accent)] hover:underline break-all">{url}</a>
            <button onClick={() => navigator.clipboard?.writeText(url)}
                    className="shrink-0 px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">Copy</button>
          </div>
        </div>
      )}

      {/* ---- 1. password ---- */}
      <Card title="1. Password">
        <Row label="Status" description="Remote access refuses to open without one">
          {status?.password_set
            ? <span className="text-[var(--success)]">Set</span>
            : <span className="text-[var(--warning)]">Not set</span>}
        </Row>
        <Row label="Generate a new one" description="Replaces the current password and signs every device out">
          <button onClick={generate} disabled={busy === 'gen'}
            className="px-3 py-1.5 rounded bg-[var(--accent)] text-white text-xs">
            {busy === 'gen' ? '…' : 'Generate'}
          </button>
        </Row>
        {freshPassword && (
          <div className="py-3">
            <div className="text-xs text-[var(--warning)] mb-1">
              Copy this now — it is stored only as a hash and cannot be shown again.
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-[var(--bg-primary)] border border-[var(--border)] rounded px-3 py-2 text-sm text-[var(--text-primary)] break-all">{freshPassword}</code>
              <button onClick={() => navigator.clipboard?.writeText(freshPassword)}
                className="px-2 py-2 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">Copy</button>
            </div>
          </div>
        )}
      </Card>

      {/* ---- 2. gateway ---- */}
      <Card title="2. Gateway">
        <Row label="Listening" description={status?.bind ? `Loopback only: ${status.bind}` : undefined}>
          {status?.running
            ? <span className="text-[var(--success)]">Yes</span>
            : status?.enabled ? <span className="text-[var(--warning)]">No</span>
              : <span className="text-[var(--text-secondary)]">Off</span>}
        </Row>
        <Row label="Dashboard build"
             description={status?.dashboard_found ? undefined : 'Run the dashboard build first'}>
          {status?.dashboard_found
            ? <span className="text-[var(--success)]">Found</span>
            : <span className="text-[var(--danger)]">Missing</span>}
        </Row>
        <Row label="Connected devices">
          <span>{status?.session_count ?? 0}</span>
        </Row>
        <Row label="Turn on remote access" description="Starts the gateway. Requires a password.">
          <button
            onClick={() => toggle('remote', 'enabled', !status?.enabled)}
            disabled={busy === 'remote.enabled'}
            className={`w-10 h-5 rounded-full transition-colors ${status?.enabled ? 'bg-[var(--accent)]' : 'bg-[var(--border)]'}`}>
            <div className={`w-4 h-4 bg-white rounded-full transition-transform ${status?.enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </Row>
      </Card>

      {/* ---- 3. tailscale ---- */}
      <Card title="3. Tailscale" right={
        <button onClick={() => act('refresh', 'tailscale.status', { refresh: true })}
          disabled={busy === 'refresh'}
          className="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
          {busy === 'refresh' ? '…' : 'Refresh'}
        </button>
      }>
        <Row label="Installed">
          {ts.installed
            ? <span className="text-[var(--success)]">{ts.version || 'Yes'}</span>
            : <span className="text-[var(--danger)]">Not installed</span>}
        </Row>
        {ts.installed && (
          <>
            <Row label="Backend">{ts.backend_state || 'unknown'}</Row>
            <Row label="Tailnet">{ts.tailnet || '—'}</Row>
            <Row label="This machine">{ts.dns_name || '—'}</Row>
            <Row label="Tailnet address">{ts.ips?.[0] || '—'}</Row>
            <Row label="Peers">{ts.peers_online ?? 0} online of {ts.peer_count ?? 0}</Row>
          </>
        )}

        {ts.installed && !ts.logged_in && (
          <>
            <div className="py-3">
              <div className="text-xs text-[var(--text-secondary)] mb-2">
                Sign in to join this machine to your tailnet. Addled opens a
                sign-in URL; you finish it in a browser.
              </div>
              {loginUrl ? (
                <div className="text-xs text-[var(--text-secondary)]">
                  Waiting for sign-in.{' '}
                  <a href={loginUrl} target="_blank" rel="noreferrer"
                     className="text-[var(--accent)] hover:underline break-all">{loginUrl}</a>
                </div>
              ) : (
                <>
                  <button onClick={() => act('login', 'tailscale.login')}
                    disabled={busy === 'login'}
                    className="px-3 py-1.5 rounded bg-[var(--accent)] text-white text-xs">
                    {busy === 'login' ? 'Starting…' : 'Sign in to Tailscale'}
                  </button>
                  <div className="mt-3">
                    <button onClick={() => setShowKey(!showKey)}
                      className="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                      {showKey ? '− Hide' : '+ Use an auth key instead'}
                    </button>
                    {showKey && (
                      <div className="flex items-center gap-2 mt-2">
                        <input value={authKey} onChange={e => setAuthKey(e.target.value)}
                          placeholder="tskey-auth-…" type="password"
                          className="flex-1 bg-[var(--bg-primary)] border border-[var(--border)] rounded px-3 py-1.5 text-xs text-[var(--text-primary)] font-mono" />
                        <button onClick={() => act('login', 'tailscale.login', { authKey })}
                          disabled={!authKey || busy === 'login'}
                          className="px-3 py-1.5 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">Connect</button>
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
            {ts.login?.error && <div className="pb-3 text-xs text-[var(--danger)]">{ts.login.error}</div>}
          </>
        )}

        {ts.installed && ts.logged_in && (
          <Row label="Sign out" description="Leaves the tailnet. Remote access stops working.">
            <button onClick={() => act('logout', 'tailscale.logout')}
              disabled={busy === 'logout'}
              className="px-3 py-1.5 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
              {busy === 'logout' ? '…' : 'Sign out'}
            </button>
          </Row>
        )}

        {!ts.installed && (
          <div className="py-3">
            {installing ? (
              <>
                <div className="text-xs text-[var(--text-secondary)] mb-2">{installDetail || 'Working…'}</div>
                <div className="h-1.5 w-full bg-[var(--bg-tertiary)] rounded overflow-hidden">
                  <div className="h-full bg-[var(--accent)] transition-all"
                       style={{ width: `${installPct}%` }} />
                </div>
                <div className="text-xs text-[var(--text-secondary)] mt-2">
                  {installPhase === 'launching'
                    ? 'A Windows administrator prompt has appeared on the machine — accept it to continue.'
                    : installPhase === 'waiting'
                      ? 'Finish the Tailscale setup window on the machine.'
                      : 'This can take a minute.'}
                </div>
              </>
            ) : !confirmInstall ? (
              <>
                <div className="text-xs text-[var(--text-secondary)] mb-2">
                  Tailscale is not installed on this machine, so there is nothing
                  for Addled to connect to yet.
                </div>
                {isRemoteSession ? (
                  <div className="text-xs text-[var(--warning)]">
                    Installing has to be done on the machine itself — it raises a
                    Windows administrator prompt there.
                  </div>
                ) : (
                  <button onClick={() => setConfirmInstall(true)}
                    className="px-3 py-1.5 rounded bg-[var(--accent)] text-white text-xs">
                    Install Tailscale
                  </button>
                )}
              </>
            ) : (
              <div className="rounded border border-[var(--border)] bg-[var(--bg-primary)] p-3">
                <div className="text-xs text-[var(--text-primary)] mb-2">
                  Addled will {installUsesWinget ? (
                    <>run <code className="text-[var(--text-secondary)]">winget install Tailscale.Tailscale</code></>
                  ) : (
                    <>download the installer from <code className="text-[var(--text-secondary)]">pkgs.tailscale.com</code> and check its signature</>
                  )}, then hand it to Windows.
                </div>
                <div className="text-xs text-[var(--text-secondary)] mb-3">
                  A Windows administrator prompt will appear on this machine and
                  you will complete the Tailscale setup window yourself.
                </div>
                <div className="flex gap-2">
                  <button onClick={startInstall}
                    className="px-3 py-1.5 rounded bg-[var(--accent)] text-white text-xs">
                    Yes, install it
                  </button>
                  <button onClick={() => setConfirmInstall(false)}
                    className="px-3 py-1.5 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                    Cancel
                  </button>
                </div>
              </div>
            )}
            {installError && (
              <div className="mt-2 text-xs text-[var(--danger)]">{installError}</div>
            )}
            {!installError && !installing && (
              <div className="mt-3 text-xs text-[var(--text-secondary)]">
                Prefer to do it yourself?{' '}
                <a href="https://tailscale.com/download/windows" target="_blank" rel="noreferrer"
                   className="text-[var(--accent)] hover:underline">Download Tailscale</a>
                {' '}and reopen this page.
              </div>
            )}
          </div>
        )}
      </Card>

      {/* ---- 4. sharing ---- */}
      <Card title="4. Share the dashboard">
        <Row label="Shared to your tailnet"
             description={serve.configured ? (serve.target || '') : 'Only devices on your tailnet can reach it'}>
          <button
            onClick={() => act('serve', serve.configured
              ? 'tailscale.disableServe' : 'tailscale.enableServe')}
            disabled={!!busy || !ts.logged_in}
            className={`w-10 h-5 rounded-full transition-colors ${serve.configured ? 'bg-[var(--accent)]' : 'bg-[var(--border)]'} ${!ts.logged_in ? 'opacity-40' : ''}`}>
            <div className={`w-4 h-4 bg-white rounded-full transition-transform ${serve.configured ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </Row>
        <Row label="Public internet (Funnel)"
             description="Anyone with the URL reaches the login page. Off unless you enable it in Settings → Remote.">
          <button
            onClick={() => act('funnel', serve.funnel
              ? 'tailscale.disableServe' : 'tailscale.enableServe', { funnel: true })}
            disabled={!!busy || !ts.logged_in}
            className={`w-10 h-5 rounded-full transition-colors ${serve.funnel ? 'bg-[var(--warning)]' : 'bg-[var(--border)]'} ${!ts.logged_in ? 'opacity-40' : ''}`}>
            <div className={`w-4 h-4 bg-white rounded-full transition-transform ${serve.funnel ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </Row>
      </Card>

      {/* ---- 5. sessions ---- */}
      <Card title="Signed-in devices" right={
        (status?.session_count ?? 0) > 0 ? (
          <button onClick={() => act('revokeAll', 'remote.revokeAll')}
            disabled={busy === 'revokeAll'}
            className="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
            {busy === 'revokeAll' ? '…' : 'Sign all out'}
          </button>
        ) : null
      }>
        {(status?.sessions || []).length === 0 && (
          <div className="py-3 text-xs text-[var(--text-secondary)]">No devices are signed in.</div>
        )}
        {(status?.sessions || []).map(s => (
          <div key={s.id} className="flex items-center justify-between py-2.5 border-b border-[var(--bg-tertiary)] last:border-0">
            <div className="min-w-0">
              <div className="text-sm text-[var(--text-primary)] truncate">
                {s.browser || 'Unknown browser'}
              </div>
              <div className="text-xs text-[var(--text-secondary)]">
                {s.tailscale_user || s.remote_addr || 'local'}
                {s.tailscale_device ? ` · ${s.tailscale_device}` : ''}
                {typeof s.idle_s === 'number' ? ` · idle ${Math.round(s.idle_s / 60)}m` : ''}
                {typeof s.expires_in_s === 'number' ? ` · ${Math.round(s.expires_in_s / 3600)}h left` : ''}
              </div>
            </div>
            <button onClick={() => act(s.id, 'remote.revoke', { id: s.id })}
              disabled={busy === s.id}
              className="shrink-0 px-2 py-1 rounded bg-[#3d1d1d] text-xs text-[var(--danger)]">
              {busy === s.id ? '…' : 'Revoke'}
            </button>
          </div>
        ))}
      </Card>

      <p className="text-xs text-[var(--text-secondary)] mt-4">
        Commands and mouse/keyboard control stay blocked for remote sessions by
        default. Change that in Settings → Remote on this machine.
      </p>
    </div>
  );
}
