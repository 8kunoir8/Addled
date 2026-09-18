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
  tone === 'ok' ? 'bg-[#3fb950]' : tone === 'warn' ? 'bg-[#d29922]' : 'bg-[#484f58]';

function Row({ label, children, description }: any) {
  return (
    <div className="flex items-start justify-between gap-6 py-2.5 border-b border-[#21262d] last:border-0">
      <div className="min-w-0">
        <div className="text-sm text-[#e8eaed]">{label}</div>
        {description && <div className="text-xs text-[#8b949e] mt-0.5">{description}</div>}
      </div>
      <div className="text-sm text-[#8b949e] shrink-0 text-right">{children}</div>
    </div>
  );
}

function Card({ title, children, right }: any) {
  return (
    <div className="rounded-lg border border-[#30363d] bg-[#161b22] mb-4">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <h2 className="text-sm font-semibold text-[#e8eaed]">{title}</h2>
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

  const load = useCallback(async () => {
    if (wsState !== 'connected') return;
    try {
      const r = await send('remote.status', {});
      setStatus(r || null);
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

  return (
    <div className="h-full overflow-y-auto p-6 max-w-3xl">
      <div className="flex items-center gap-3 mb-1">
        <span className={`w-2.5 h-2.5 rounded-full ${DOT(stateTone)}`} />
        <h1 className="text-lg font-semibold">Remote access</h1>
      </div>
      <p className="text-sm text-[#8b949e] mb-6">
        Reach this Addled from your phone or another machine, in a browser, over
        Tailscale. Nothing is on the public internet unless you turn that on.
      </p>

      {error && <div className="mb-4 text-sm text-[#f85149] bg-[#2d1113] border border-[#5a1d1d] rounded px-3 py-2">{error}</div>}
      {note && <div className="mb-4 text-sm text-[#3fb950] bg-[#0f2a17] border border-[#1d5a2d] rounded px-3 py-2">{note}</div>}

      {blockers.length > 0 && (
        <div className="mb-4 rounded-lg border border-[#5a4a1d] bg-[#2a2410] px-4 py-3">
          <div className="text-xs font-semibold text-[#d29922] mb-1">Not reachable yet</div>
          <ul className="text-xs text-[#d29922] list-disc pl-4 space-y-0.5">
            {blockers.map((b, i) => <li key={i}>{b}</li>)}
          </ul>
        </div>
      )}

      {url && (
        <div className="mb-4 rounded-lg border border-[#1d5a2d] bg-[#0f2a17] px-4 py-3">
          <div className="text-xs text-[#3fb950] mb-1">Open this on any device on your tailnet</div>
          <div className="flex items-center gap-3">
            <a href={url} target="_blank" rel="noreferrer"
               className="text-sm text-[#3380FF] hover:underline break-all">{url}</a>
            <button onClick={() => navigator.clipboard?.writeText(url)}
                    className="shrink-0 px-2 py-1 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">Copy</button>
          </div>
        </div>
      )}

      {/* ---- 1. password ---- */}
      <Card title="1. Password">
        <Row label="Status" description="Remote access refuses to open without one">
          {status?.password_set
            ? <span className="text-[#3fb950]">Set</span>
            : <span className="text-[#d29922]">Not set</span>}
        </Row>
        <Row label="Generate a new one" description="Replaces the current password and signs every device out">
          <button onClick={generate} disabled={busy === 'gen'}
            className="px-3 py-1.5 rounded bg-[#1f6feb] text-white text-xs">
            {busy === 'gen' ? '…' : 'Generate'}
          </button>
        </Row>
        {freshPassword && (
          <div className="py-3">
            <div className="text-xs text-[#d29922] mb-1">
              Copy this now — it is stored only as a hash and cannot be shown again.
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-[#e8eaed] break-all">{freshPassword}</code>
              <button onClick={() => navigator.clipboard?.writeText(freshPassword)}
                className="px-2 py-2 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">Copy</button>
            </div>
          </div>
        )}
      </Card>

      {/* ---- 2. gateway ---- */}
      <Card title="2. Gateway">
        <Row label="Listening" description={status?.bind ? `Loopback only: ${status.bind}` : undefined}>
          {status?.running
            ? <span className="text-[#3fb950]">Yes</span>
            : status?.enabled ? <span className="text-[#d29922]">No</span>
              : <span className="text-[#8b949e]">Off</span>}
        </Row>
        <Row label="Dashboard build"
             description={status?.dashboard_found ? undefined : 'Run the dashboard build first'}>
          {status?.dashboard_found
            ? <span className="text-[#3fb950]">Found</span>
            : <span className="text-[#f85149]">Missing</span>}
        </Row>
        <Row label="Connected devices">
          <span>{status?.session_count ?? 0}</span>
        </Row>
        <Row label="Turn on remote access" description="Starts the gateway. Requires a password.">
          <button
            onClick={() => toggle('remote', 'enabled', !status?.enabled)}
            disabled={busy === 'remote.enabled'}
            className={`w-10 h-5 rounded-full transition-colors ${status?.enabled ? 'bg-[#3380FF]' : 'bg-[#30363d]'}`}>
            <div className={`w-4 h-4 bg-white rounded-full transition-transform ${status?.enabled ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </Row>
      </Card>

      {/* ---- 3. tailscale ---- */}
      <Card title="3. Tailscale" right={
        <button onClick={() => act('refresh', 'tailscale.status', { refresh: true })}
          disabled={busy === 'refresh'}
          className="px-2 py-1 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">
          {busy === 'refresh' ? '…' : 'Refresh'}
        </button>
      }>
        <Row label="Installed">
          {ts.installed
            ? <span className="text-[#3fb950]">{ts.version || 'Yes'}</span>
            : <span className="text-[#f85149]">Not installed</span>}
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
              <div className="text-xs text-[#8b949e] mb-2">
                Sign in to join this machine to your tailnet. Addled opens a
                sign-in URL; you finish it in a browser.
              </div>
              {loginUrl ? (
                <div className="text-xs text-[#8b949e]">
                  Waiting for sign-in.{' '}
                  <a href={loginUrl} target="_blank" rel="noreferrer"
                     className="text-[#3380FF] hover:underline break-all">{loginUrl}</a>
                </div>
              ) : (
                <>
                  <button onClick={() => act('login', 'tailscale.login')}
                    disabled={busy === 'login'}
                    className="px-3 py-1.5 rounded bg-[#1f6feb] text-white text-xs">
                    {busy === 'login' ? 'Starting…' : 'Sign in to Tailscale'}
                  </button>
                  <div className="mt-3">
                    <button onClick={() => setShowKey(!showKey)}
                      className="text-xs text-[#8b949e] hover:text-[#e8eaed]">
                      {showKey ? '− Hide' : '+ Use an auth key instead'}
                    </button>
                    {showKey && (
                      <div className="flex items-center gap-2 mt-2">
                        <input value={authKey} onChange={e => setAuthKey(e.target.value)}
                          placeholder="tskey-auth-…" type="password"
                          className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-xs text-[#e8eaed] font-mono" />
                        <button onClick={() => act('login', 'tailscale.login', { authKey })}
                          disabled={!authKey || busy === 'login'}
                          className="px-3 py-1.5 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">Connect</button>
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
            {ts.login?.error && <div className="pb-3 text-xs text-[#f85149]">{ts.login.error}</div>}
          </>
        )}

        {ts.installed && ts.logged_in && (
          <Row label="Sign out" description="Leaves the tailnet. Remote access stops working.">
            <button onClick={() => act('logout', 'tailscale.logout')}
              disabled={busy === 'logout'}
              className="px-3 py-1.5 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">
              {busy === 'logout' ? '…' : 'Sign out'}
            </button>
          </Row>
        )}

        {!ts.installed && (
          <div className="py-3">
            {installing ? (
              <>
                <div className="text-xs text-[#8b949e] mb-2">{installDetail || 'Working…'}</div>
                <div className="h-1.5 w-full bg-[#21262d] rounded overflow-hidden">
                  <div className="h-full bg-[#1f6feb] transition-all"
                       style={{ width: `${installPct}%` }} />
                </div>
                <div className="text-xs text-[#8b949e] mt-2">
                  {installPhase === 'launching'
                    ? 'A Windows administrator prompt has appeared on the machine — accept it to continue.'
                    : installPhase === 'waiting'
                      ? 'Finish the Tailscale setup window on the machine.'
                      : 'This can take a minute.'}
                </div>
              </>
            ) : !confirmInstall ? (
              <>
                <div className="text-xs text-[#8b949e] mb-2">
                  Tailscale is not installed on this machine, so there is nothing
                  for Addled to connect to yet.
                </div>
                {isRemoteSession ? (
                  <div className="text-xs text-[#d29922]">
                    Installing has to be done on the machine itself — it raises a
                    Windows administrator prompt there.
                  </div>
                ) : (
                  <button onClick={() => setConfirmInstall(true)}
                    className="px-3 py-1.5 rounded bg-[#1f6feb] text-white text-xs">
                    Install Tailscale
                  </button>
                )}
              </>
            ) : (
              <div className="rounded border border-[#30363d] bg-[#0d1117] p-3">
                <div className="text-xs text-[#e8eaed] mb-2">
                  Addled will {installUsesWinget ? (
                    <>run <code className="text-[#8b949e]">winget install Tailscale.Tailscale</code></>
                  ) : (
                    <>download the installer from <code className="text-[#8b949e]">pkgs.tailscale.com</code> and check its signature</>
                  )}, then hand it to Windows.
                </div>
                <div className="text-xs text-[#8b949e] mb-3">
                  A Windows administrator prompt will appear on this machine and
                  you will complete the Tailscale setup window yourself.
                </div>
                <div className="flex gap-2">
                  <button onClick={startInstall}
                    className="px-3 py-1.5 rounded bg-[#1f6feb] text-white text-xs">
                    Yes, install it
                  </button>
                  <button onClick={() => setConfirmInstall(false)}
                    className="px-3 py-1.5 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">
                    Cancel
                  </button>
                </div>
              </div>
            )}
            {installError && (
              <div className="mt-2 text-xs text-[#f85149]">{installError}</div>
            )}
            {!installError && !installing && (
              <div className="mt-3 text-xs text-[#8b949e]">
                Prefer to do it yourself?{' '}
                <a href="https://tailscale.com/download/windows" target="_blank" rel="noreferrer"
                   className="text-[#3380FF] hover:underline">Download Tailscale</a>
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
            className={`w-10 h-5 rounded-full transition-colors ${serve.configured ? 'bg-[#3380FF]' : 'bg-[#30363d]'} ${!ts.logged_in ? 'opacity-40' : ''}`}>
            <div className={`w-4 h-4 bg-white rounded-full transition-transform ${serve.configured ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </Row>
        <Row label="Public internet (Funnel)"
             description="Anyone with the URL reaches the login page. Off unless you enable it in Settings → Remote.">
          <button
            onClick={() => act('funnel', serve.funnel
              ? 'tailscale.disableServe' : 'tailscale.enableServe', { funnel: true })}
            disabled={!!busy || !ts.logged_in}
            className={`w-10 h-5 rounded-full transition-colors ${serve.funnel ? 'bg-[#d29922]' : 'bg-[#30363d]'} ${!ts.logged_in ? 'opacity-40' : ''}`}>
            <div className={`w-4 h-4 bg-white rounded-full transition-transform ${serve.funnel ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </Row>
      </Card>

      {/* ---- 5. sessions ---- */}
      <Card title="Signed-in devices" right={
        (status?.session_count ?? 0) > 0 ? (
          <button onClick={() => act('revokeAll', 'remote.revokeAll')}
            disabled={busy === 'revokeAll'}
            className="px-2 py-1 rounded bg-[#21262d] text-xs text-[#8b949e] hover:text-[#e8eaed]">
            {busy === 'revokeAll' ? '…' : 'Sign all out'}
          </button>
        ) : null
      }>
        {(status?.sessions || []).length === 0 && (
          <div className="py-3 text-xs text-[#8b949e]">No devices are signed in.</div>
        )}
        {(status?.sessions || []).map(s => (
          <div key={s.id} className="flex items-center justify-between py-2.5 border-b border-[#21262d] last:border-0">
            <div className="min-w-0">
              <div className="text-sm text-[#e8eaed] truncate">
                {s.browser || 'Unknown browser'}
              </div>
              <div className="text-xs text-[#8b949e]">
                {s.tailscale_user || s.remote_addr || 'local'}
                {s.tailscale_device ? ` · ${s.tailscale_device}` : ''}
                {typeof s.idle_s === 'number' ? ` · idle ${Math.round(s.idle_s / 60)}m` : ''}
                {typeof s.expires_in_s === 'number' ? ` · ${Math.round(s.expires_in_s / 3600)}h left` : ''}
              </div>
            </div>
            <button onClick={() => act(s.id, 'remote.revoke', { id: s.id })}
              disabled={busy === s.id}
              className="shrink-0 px-2 py-1 rounded bg-[#3d1d1d] text-xs text-[#f85149]">
              {busy === s.id ? '…' : 'Revoke'}
            </button>
          </div>
        ))}
      </Card>

      <p className="text-xs text-[#8b949e] mt-4">
        Commands and mouse/keyboard control stay blocked for remote sessions by
        default. Change that in Settings → Remote on this machine.
      </p>
    </div>
  );
}
