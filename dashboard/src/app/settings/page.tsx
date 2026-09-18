'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

type SettingsData = Record<string, any>;

const SECTION_ICONS: Record<string, string> = {
  providers: '🔌', character: '🎭', voice: '🎤', safety: '🛡️',
  notifications: '🔔', memory: '🧠', tools: '🔧', integrations: '🔗', appearance: '🎨',  observation: '👁', browser: '🌐', desktop: '🖱', about: 'ℹ️', guidelines: '📐',
  mcp: '🧰', wiki: '📖',
};

export default function SettingsPage() {
  const { state: wsState, send } = useWS();
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [activeSection, setActiveSection] = useState('providers');
  const [saving, setSaving] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<{ key: string; ok: boolean } | null>(null);

  useEffect(() => {
    if (wsState === 'connected') {
      send('settings.get', {}).then(r => setSettings(r?.settings || {})).catch(() => {});
    }
  }, [wsState, send]);

  const updateSetting = async (section: string, key: string, value: any) => {
    if (wsState !== 'connected') return;
    setSaving(`${section}.${key}`);
    try {
      await send('settings.set', { section, key, value });
      setSettings((prev: any) => {
        if (!key) return { ...prev, [section]: value };  // top-level setting
        return { ...prev, [section]: { ...prev?.[section], [key]: value } };
      });
      setSaveStatus({ key: `${section}.${key}`, ok: true });
    } catch { setSaveStatus({ key: `${section}.${key}`, ok: false }); }
    setSaving(null);
    setTimeout(() => setSaveStatus(null), 2000);
  };

  const sections = ['providers','character','voice','safety','notifications','observation','memory','wiki','tools','guidelines','mcp','browser','desktop','integrations','appearance','about'];

  if (!settings) return (
    <div className="flex items-center justify-center h-full text-[#8b949e]">
      <div className="text-center"><div className="animate-spin text-2xl mb-3">⟳</div><p>{wsState === 'connected' ? 'Loading...' : 'Connecting...'}</p></div>
    </div>
  );

  return (
    <div className="flex h-full">
      <div className="w-48 border-r border-[#30363d] p-2 space-y-0.5 overflow-y-auto">
        {sections.map(s => (
          <button key={s} onClick={() => setActiveSection(s)}
            className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors ${activeSection === s ? 'bg-[#1f6feb] text-white' : 'text-[#8b949e] hover:bg-[#21262d] hover:text-[#e8eaed]'}`}>
            <span className="mr-2">{SECTION_ICONS[s]}</span>{s.charAt(0).toUpperCase()+s.slice(1)}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-6">
        <h2 className="text-lg font-semibold mb-6">{SECTION_ICONS[activeSection]} {activeSection.charAt(0).toUpperCase()+activeSection.slice(1)}</h2>
        {activeSection==='providers'&&<ProvidersSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='character'&&<CharacterSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='character'&&<SkinsSection send={send} connected={wsState==='connected'}/>}
        {activeSection==='voice'&&<VoiceSection settings={settings} update={updateSetting} saving={saving} status={saveStatus} send={send} connected={wsState==='connected'}/>}
        {activeSection==='safety'&&<SafetySection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='notifications'&&<NotificationsSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='observation'&&<ObservationSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='memory'&&<MemorySection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='wiki'&&<WikiSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='tools'&&<ToolsSection settings={settings} update={updateSetting} saving={saving} status={saveStatus} send={send} connected={wsState==='connected'}/>}
        {activeSection==='guidelines'&&<GuidelinesSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='mcp'&&<McpSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='browser'&&<BrowserSection settings={settings} update={updateSetting} saving={saving} status={saveStatus} send={send} connected={wsState==='connected'}/>}
        {activeSection==='desktop'&&<DesktopSection settings={settings} update={updateSetting} saving={saving} status={saveStatus} send={send} connected={wsState==='connected'}/>}
        {activeSection==='integrations'&&<IntegrationsSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='appearance'&&<AppearanceSection/>}
        {activeSection==='about'&&<AboutSection/>}
      </div>
    </div>
  );
}

function SettingRow({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="py-3 border-b border-[#21262d]">
      <div className="flex items-center justify-between">
        <div className="flex-1"><label className="text-sm font-medium text-[#e8eaed]">{label}</label>
          {description && <p className="text-xs text-[#8b949e] mt-0.5">{description}</p>}</div>
        <div className="ml-4">{children}</div>
      </div>
    </div>
  );
}

function SaveIndicator({ settingKey, saving, status }: { settingKey: string; saving: string|null; status: {key:string;ok:boolean}|null }) {
  if (saving===settingKey) return <span className="text-xs text-[#d29922]">Saving...</span>;
  if (status?.key===settingKey) return <span className={`text-xs ${status.ok?'text-[#3fb950]':'text-[#f85149]'}`}>{status.ok?'✓ Saved':'✗ Failed'}</span>;
  return null;
}

function SkinsSection({ send, connected }: { send: (m: string, p?: any) => Promise<any>; connected: boolean }) {
  const [skins, setSkins] = useState<any[]>([]);
  const [active, setActive] = useState<string>('');
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) return;
    try {
      const r = await send('character.skinsList', {});
      setSkins(r?.skins || []);
      setActive(r?.active || '');
    } catch { /* backend offline */ }
  }, [send, connected]);

  useEffect(() => { refresh(); }, [refresh]);

  const onFile = async (file: File) => {
    if (!file || !connected) return;
    const isZip = /\.zip$/i.test(file.name);
    const isGif = /\.gif$/i.test(file.name);
    if (!isZip && !isGif) { setMsg({ ok: false, text: 'Only .gif or .zip files are supported' }); return; }
    setUploading(true); setMsg(null);
    try {
      const dataUrl: string = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result as string);
        fr.onerror = () => rej(new Error('Could not read file'));
        fr.readAsDataURL(file);
      });
      const b64 = dataUrl.split(',')[1] || '';
      const name = file.name.replace(/\.[^.]+$/, '');
      const r = await send('character.uploadSkin', { name, filename: file.name, data: b64 });
      if (r?.success) {
        const skin = r.skin || {};
        const mapped = skin.mapped || [];
        if (isZip && mapped.length) {
          setMsg({ ok: true, text: `"${name}" applied — ${mapped.length} states mapped (${mapped.slice(0, 6).join(', ')}${mapped.length > 6 ? '…' : ''}) ✓` });
        } else if (isZip) {
          setMsg({ ok: true, text: `"${name}" applied — no state-named GIFs found, using the first clip for all states ✓` });
        } else {
          setMsg({ ok: true, text: `"${name}" applied — Fox is wearing it now ✓` });
        }
        refresh();
      } else setMsg({ ok: false, text: r?.error || 'Upload failed' });
    } catch (e: any) { setMsg({ ok: false, text: e?.message || 'Upload failed' }); }
    setUploading(false);
  };

  const setSkin = async (id: string) => {
    if (!connected) return;
    const r = await send('character.setSkin', { id });
    if (r?.success) { setActive(id); refresh(); }
  };

  const removeSkin = async (id: string) => {
    if (!connected) return;
    await send('character.deleteSkin', { id });
    refresh();
  };

  return (
    <div className="mt-6 pt-4 border-t border-[#21262d]">
      <h3 className="text-sm font-semibold text-[#e8eaed] mb-1">🦊 Sprite skin <span className="text-xs font-normal text-[#8b949e]">(codex-pet style)</span></h3>
      <p className="text-xs text-[#8b949e] mb-3">Upload an animated GIF and Fox becomes that pet — all agent states, movement and effects stay active on top.</p>

      {/* Upload options */}
      <div className="flex items-center gap-2 mb-3">
        <label className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md border text-sm cursor-pointer transition-colors ${uploading ? 'opacity-50 pointer-events-none' : 'border-[#3380FF] text-[#3380FF] hover:bg-[#1f6feb22]'}`}>
          {uploading ? '⟳ Uploading...' : '⬆ Upload GIF'}
          <input type="file" accept="image/gif,.gif" className="hidden" disabled={uploading}
            onChange={e => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ''; }} />
        </label>
        <span className="text-xs text-[#484f58]">or</span>
        <label className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md border text-sm cursor-pointer transition-colors ${uploading ? 'opacity-50 pointer-events-none' : 'border-[#3380FF] text-[#3380FF] hover:bg-[#1f6feb22]'}`}>
          {uploading ? '⟳ Uploading...' : '📦 Upload ZIP (multi-state)'}
          <input type="file" accept=".zip,application/zip" className="hidden" disabled={uploading}
            onChange={e => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ''; }} />
        </label>
      </div>

      {/* What's needed for multi-state ZIPs */}
      <div className="bg-[#0d1117] border border-[#21262d] rounded-lg p-3 mb-3">
        <p className="text-xs font-semibold text-[#e8eaed] mb-1.5">📦 ZIP with one GIF per agent state</p>
        <p className="text-[11px] leading-relaxed text-[#8b949e]">
          Name each GIF after the state it should play. Any GIFs you skip fall back to <span className="text-[#e8eaed] font-mono">idle.gif</span>.
        </p>
        <p className="text-[11px] leading-relaxed text-[#8b949e] mt-1.5 font-mono break-all">
          idle.gif · listening.gif · observing.gif · thinking.gif<br/>
          has_suggestion.gif · acting.gif · speaking.gif · sleeping.gif<br/>
          blocked.gif · error.gif · working.gif · dreaming.gif
        </p>
      </div>

      {msg && <p className={`text-xs mt-2 ${msg.ok ? 'text-[#3fb950]' : 'text-[#f85149]'}`}>{msg.text}</p>}

      <div className="mt-4 space-y-1">
        <button onClick={() => setSkin('none')}
          className={`w-full text-left px-3 py-2 rounded-md text-sm border transition-colors ${active === '' ? 'border-[#3380FF] bg-[#1f6feb22] text-[#3380FF]' : 'border-[#30363d] text-[#8b949e] hover:border-[#484f58]'}`}>
          🔷 Procedural shape (default)
        </button>
        {skins.map(s => (
          <div key={s.id} className={`flex items-center justify-between px-3 py-2 rounded-md border ${s.active ? 'border-[#3380FF] bg-[#1f6feb22]' : 'border-[#30363d] hover:border-[#484f58]'}`}>
            <button onClick={() => setSkin(s.id)} className="text-left flex-1 text-sm text-[#e8eaed]">
              🎞 {s.name} <span className="text-xs text-[#8b949e]">({s.files.length} clip{s.files.length !== 1 ? 's' : ''}{s.mapped && s.mapped.length ? ` · ${s.mapped.length} states mapped` : ''})</span>
              {s.active && <span className="ml-2 text-xs text-[#3fb950]">● active</span>}
            </button>
            <button onClick={() => removeSkin(s.id)} title="Delete skin"
              className="text-[#8b949e] hover:text-[#f85149] text-sm px-2">✕</button>
          </div>
        ))}
        {skins.length === 0 && <p className="text-xs text-[#8b949e] px-1 py-1">No skins yet — upload a GIF or ZIP to get started.</p>}
      </div>
    </div>
  );
}

const ROUTE_ROLES:[string,string,string][]=[
  ['chat','Chat','quick conversational replies'],
  ['reasoning','Reasoning','analysis, debugging, code changes'],
  ['vision','Vision','images and screenshots'],
  ['long','Long context','whole files and documents'],
];

function relTime(iso?:string){
  if(!iso)return 'never';
  const t=Date.parse(iso);
  if(!isFinite(t))return 'never';
  const s=Math.max(0,Math.floor((Date.now()-t)/1000));
  if(s<60)return 'just now';
  const m=Math.floor(s/60); if(m<60)return `${m} min ago`;
  const h=Math.floor(m/60); if(h<24)return `${h} h ago`;
  return `${Math.floor(h/24)} d ago`;
}

const MCP_STATES: Record<string,string> = {
  ready: 'bg-[#238636] text-white',
  connecting: 'bg-[#d29922] text-black',
  error: 'bg-[#f85149] text-white',
  disconnected: 'bg-[#21262d] text-[#8b949e]',
};

function McpSection({settings,update,saving,status}: any){
  const m=settings?.mcp||{};
  const {send,state:wsState}=useWS();
  const [live,setLive]=useState<any>(null);
  const [busy,setBusy]=useState<string|null>(null);
  const [err,setErr]=useState<string|null>(null);
  const [form,setForm]=useState<any>({name:'',transport:'stdio',command:'',url:'',trusted:false});

  const load=useCallback(async()=>{
    if(wsState!=='connected')return;
    try{ setLive(await send('mcp.list',{})); }catch{}
  },[send,wsState]);

  useEffect(()=>{ load(); },[load]);

  const act=async(method:string,params:any,label:string)=>{
    if(wsState!=='connected')return;
    setBusy(label); setErr(null);
    try{
      const out=await send(method,params);
      if(out?.status)setLive(out.status); else await load();
      if(out&&out.success===false)setErr(out.error||'Request failed');
    }catch(e:any){ setErr(String(e?.message||e)); }
    setBusy(null);
  };

  const addServer=async()=>{
    if(!form.name.trim()){ setErr('Give the server a name.'); return; }
    const server:any={name:form.name.trim(),transport:form.transport,enabled:true,trusted:!!form.trusted};
    if(form.transport==='stdio'){
      const parts=form.command.trim().split(/\s+/).filter(Boolean);
      if(!parts.length){ setErr('A stdio server needs a command.'); return; }
      server.command=parts;
      server.args=[];
    }else{
      if(!form.url.trim()){ setErr('An HTTP server needs a URL.'); return; }
      server.url=form.url.trim();
    }
    await act('mcp.add',{server},'add');
    setForm({name:'',transport:form.transport,command:'',url:'',trusted:false});
  };

  const btn='px-3 py-1.5 rounded text-xs font-medium border border-[#30363d] hover:border-[#484f58] text-[#e8eaed] disabled:opacity-40';
  const inp='bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]';
  const servers:any[]=live?.servers||[];

  return <div className="space-y-1 max-w-3xl">
    <p className="text-xs text-[#d29922] px-1 pb-2">
      ⚠ MCP servers are third-party programs. They run with your permissions and can
      read, change and delete things. Only add servers you trust.
    </p>
    <p className="text-xs text-[#8b949e] px-1 pb-2">
      Their tools become Addled skills in the <span className="font-mono">MCP</span> category.
      Tools from an untrusted server ask for your approval the first time they run.
    </p>
    <SettingRow label="Enable MCP">
      <input type="checkbox" checked={m.enabled!==false}
        onChange={e=>update('mcp','enabled',e.target.checked)}/>
    </SettingRow>
    <SettingRow label="Connect at startup">
      <input type="checkbox" checked={m.autoconnect!==false}
        onChange={e=>update('mcp','autoconnect',e.target.checked)}/>
    </SettingRow>
    <div className="flex items-center gap-2 px-1 pt-1 text-[11px]">
      <button onClick={()=>act('mcp.reload',{},'reload')}
        disabled={busy==='reload'||wsState!=='connected'} className={btn}>
        {busy==='reload'?'Reconnecting…':'↻ Reconnect all'}
      </button>
      <span className="text-[#8b949e]">
        {live?`${servers.filter((s:any)=>s.state==='ready').length}/${servers.length} connected · ${live.tool_count||0} tool(s)`:'loading…'}
      </span>
    </div>
    {err&&<p className="px-1 pt-1 text-xs text-[#f85149]">⚠ {err}</p>}

    {servers.map((s:any)=><div key={s.id} className="mt-3 rounded-lg border border-[#30363d] p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-[#e8eaed] truncate">
            {s.name} <span className="ml-1 text-[10px] text-[#8b949e]">{s.transport}</span>
          </p>
          <p className="truncate text-[10px] text-[#8b949e]"
            title={s.transport==='http'?s.url:(Array.isArray(s.command)?s.command.join(' '):String(s.command||''))}>
            {s.transport==='http'?s.url:(Array.isArray(s.command)?s.command.join(' '):String(s.command||''))}
          </p>
        </div>
        <span className={`shrink-0 rounded px-2 py-0.5 text-[10px] ${MCP_STATES[s.state]||MCP_STATES.disconnected}`}>
          {s.state}{s.tool_count?` · ${s.tool_count} tools`:''}
        </span>
      </div>
      {s.last_error&&<p className="mt-1 text-[10px] text-[#d29922]">⚠ {s.last_error}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer">
          <input type="checkbox" checked={s.enabled}
            onChange={e=>act('mcp.update',{id:s.id,patch:{enabled:e.target.checked}},'toggle')}/>
          Enabled
        </label>
        <label className="flex items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer"
          title="Skip the approval prompt for this server's tools">
          <input type="checkbox" checked={s.trusted}
            onChange={e=>act('mcp.update',{id:s.id,patch:{trusted:e.target.checked}},'toggle')}/>
          Trusted
        </label>
        <button onClick={()=>act('mcp.connect',{id:s.id},'connect')}
          disabled={!!busy||wsState!=='connected'} className={btn}>Connect</button>
        <button onClick={()=>act('mcp.disconnect',{id:s.id},'disconnect')}
          disabled={!!busy||wsState!=='connected'} className={btn}>Disconnect</button>
        <button onClick={()=>{ if(window.confirm(`Remove ${s.name}?`)) act('mcp.remove',{id:s.id},'remove'); }}
          disabled={!!busy||wsState!=='connected'}
          className={`${btn} text-[#f85149]`}>Remove</button>
      </div>
      {s.tools?.length?<div className="mt-2 text-[10px] text-[#8b949e]">
        {s.tools.slice(0,14).map((t:any)=><span key={t.name} className="mr-2 font-mono">{t.name}</span>)}
        {s.tools.length>14?<span>+{s.tools.length-14} more</span>:null}
      </div>:null}
    </div>)}

    {!servers.length&&<p className="px-1 pt-3 text-xs text-[#8b949e]">No MCP servers configured yet.</p>}

    <div className="mt-4 rounded-lg border border-[#30363d] p-3">
      <p className="text-sm font-medium text-[#e8eaed]">Add a server</p>
      <div className="mt-2 space-y-2">
        <div className="flex items-center gap-2">
          <span className="w-24 text-[11px] text-[#8b949e]">Name</span>
          <input value={form.name} onChange={e=>setForm({...form,name:e.target.value})}
            placeholder="Filesystem" className={`${inp} flex-1`}/>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-24 text-[11px] text-[#8b949e]">Transport</span>
          <select value={form.transport} onChange={e=>setForm({...form,transport:e.target.value})} className={inp}>
            <option value="stdio">stdio (local command)</option>
            <option value="http">HTTP (remote URL)</option>
          </select>
        </div>
        {form.transport==='stdio'
          ?<div className="flex items-center gap-2">
            <span className="w-24 text-[11px] text-[#8b949e]">Command</span>
            <input value={form.command} onChange={e=>setForm({...form,command:e.target.value})}
              placeholder="npx -y @modelcontextprotocol/server-filesystem C:\\Users"
              className={`${inp} flex-1`}/>
          </div>
          :<div className="flex items-center gap-2">
            <span className="w-24 text-[11px] text-[#8b949e]">URL</span>
            <input value={form.url} onChange={e=>setForm({...form,url:e.target.value})}
              placeholder="https://example.com/mcp" className={`${inp} flex-1`}/>
          </div>}
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer">
            <input type="checkbox" checked={!!form.trusted}
              onChange={e=>setForm({...form,trusted:e.target.checked})}/>
            Trust this server (skip approval prompts)
          </label>
          <button onClick={addServer} disabled={!!busy||wsState!=='connected'}
            className="px-3 py-1.5 rounded text-xs font-medium bg-[#3380FF] hover:bg-[#4d94ff] text-white disabled:opacity-40">
            Add server
          </button>
        </div>
        <p className="text-[10px] text-[#8b949e]">
          To change a server's command or URL, remove it and add it again.
          Header and environment values are stored in settings.json, never sent to the dashboard.
        </p>
      </div>
    </div>
  </div>;
}

function GuidelinesSection({settings,update,saving,status}: any){
  const g=settings?.guidelines||{};
  const packs:any=g.packs||{};
  const {send,state:wsState}=useWS();
  const [docs,setDocs]=useState<any>(null);
  const [busy,setBusy]=useState(false);

  const load=useCallback(async()=>{
    if(wsState!=='connected')return;
    try{ setDocs(await send('guidelines.state',{})); }catch{}
  },[send,wsState]);

  useEffect(()=>{ load(); },[load]);

  const refreshAll=async()=>{
    if(wsState!=='connected')return;
    setBusy(true);
    try{
      const out=await send('guidelines.refresh',{});
      if(out?.state)setDocs(out.state);
      else await load();
    }catch{}
    setBusy(false);
  };

  const setPack=(id:string,patch:any)=>{
    update('guidelines','packs',{...packs,[id]:{...(packs[id]||{}),...patch}});
  };

  const btn='px-3 py-1.5 rounded text-xs font-medium border border-[#30363d] hover:border-[#484f58] text-[#e8eaed] disabled:opacity-40';
  const sel='bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed]';
  const packDocs:any=docs?.packs||{};
  // Prefer the live state, but fall back to the configured packs so the panel
  // still renders if the status call fails.
  const ids:string[]=Object.keys(packDocs).length?Object.keys(packDocs):Object.keys(packs);

  return <div className="space-y-1 max-w-3xl">
    <p className="text-xs text-[#8b949e] px-1 pb-2">
      Addled can follow external coding rulesets. The text is downloaded from the
      upstream repository on first use, cached locally, and refreshed weekly —
      nothing is bundled with Addled, so upstream updates arrive on their own.
    </p>
    <SettingRow label="Follow guidelines">
      <input type="checkbox" checked={g.enabled!==false}
        onChange={e=>update('guidelines','enabled',e.target.checked)}/>
    </SettingRow>
    <SettingRow label="Apply to">
      <select value={g.scope||'code'} onChange={e=>update('guidelines','scope',e.target.value)} className={sel}>
        <option value="code">Code tasks only</option>
        <option value="always">Every chat</option>
      </select>
    </SettingRow>
    <SettingRow label="Max characters per pack">
      <input type="number" min={200} step={200} value={g.max_chars??6000}
        onChange={e=>update('guidelines','max_chars',Number(e.target.value))}
        className={`${sel} w-28`}/>
    </SettingRow>
    <div className="flex items-center gap-2 px-1 pt-1 text-[11px]">
      <button onClick={refreshAll} disabled={busy||wsState!=='connected'} className={btn}>
        {busy?'Refreshing…':'↻ Refresh from upstream'}
      </button>
      <span className="text-[#8b949e]">
        {g.enabled!==false
          ? `${ids.length} pack(s) · ${(g.scope||'code')==='always'?'every chat':'code tasks only'}`
          : 'guidelines are off'}
      </span>
    </div>

    {ids.map((id:string)=>{
      const d:any=packDocs[id]||{};
      const pc:any=packs[id]||{};
      const level=pc.level||d.default_level||'full';
      const levels:string[]=d.levels?.length?d.levels:['lite','full','ultra'];
      return <div key={id} className="mt-3 rounded-lg border border-[#30363d] p-3">
        <div className="flex items-start justify-between gap-3">
          <p className="text-sm font-medium text-[#e8eaed]">
            {d.title||id}
            {d.subtitle?<span className="font-normal text-[#8b949e]"> — {d.subtitle}</span>:null}
          </p>
          <label className="flex shrink-0 items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer">
            <input type="checkbox" checked={pc.enabled!==false}
              onChange={e=>setPack(id,{enabled:e.target.checked})}/>
            Enabled
          </label>
        </div>
        {d.summary&&<p className="mt-1 text-[11px] text-[#8b949e]">{d.summary}</p>}
        <div className="mt-2 flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-[11px] text-[#8b949e]">Level
            <select value={level} onChange={e=>setPack(id,{level:e.target.value})} className={sel}>
              {levels.map((l:string)=><option key={l} value={l}>{l}</option>)}
              <option value="off">off</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-[11px] text-[#8b949e]">Scope
            <select value={pc.scope||g.scope||'code'}
              onChange={e=>setPack(id,{scope:e.target.value})} className={sel}>
              <option value="code">code tasks</option>
              <option value="always">every chat</option>
            </select>
          </label>
        </div>
        <div className="mt-2 space-y-0.5 text-[10px] text-[#8b949e]">
          <p>{d.cached?`${d.chars} characters · updated ${relTime(d.fetched_at)}`
                     :'not downloaded yet'}</p>
          {d.source_url&&<p className="truncate" title={d.source_url}>source: {d.source_url}</p>}
          {d.home&&<p className="truncate">
            <a href={d.home} target="_blank" rel="noreferrer"
              className="text-[#4d94ff] hover:underline">{d.home}</a>
            {d.license?` · ${d.license}`:''}
          </p>}
          {d.path&&<p className="truncate" title={d.path}>cached at {d.path}</p>}
          {d.error&&<p className="text-[#d29922]">⚠ {d.error}</p>}
          {!d.cached&&!d.error&&<p>Press Refresh to download it.</p>}
        </div>
      </div>;
    })}
  </div>;
}

function ProvidersSection({ settings, update, saving, status }: any) {
  const p=settings?.providers||{}, builtin=p.builtin||{}, active=p.active||'local';
  const llm=settings?.local_llm||{};
  const { send, state: wsState }=useWS();
  const [llmStatus,setLlmStatus]=useState<any>(null);
  const [busy,setBusy]=useState<string|null>(null);
  const [catalog,setCatalog]=useState<any>(null);
  const [refreshing,setRefreshing]=useState(false);

  const refresh=useCallback(async()=>{
    if(wsState!=='connected')return;
    try{ setLlmStatus(await send('localLlm.status',{})); }catch{}
    try{ setCatalog(await send('models.catalog',{})); }catch{}
  },[send,wsState]);

  const refreshModels=async()=>{
    if(wsState!=='connected')return;
    setRefreshing(true);
    try{
      const out=await send('models.refresh',{provider:active});
      if(out?.catalog)setCatalog(out.catalog);
      else await refresh();
    }catch{}
    setRefreshing(false);
  };

  useEffect(()=>{ refresh(); },[refresh]);

  const act=async(method:string,label:string)=>{
    if(wsState!=='connected')return;
    setBusy(label);
    try{ await send(method,{}); }catch{}
    await refresh();
    setBusy(null);
  };

  const cat:any=catalog?.providers?.[active]||null;
  const models:string[]=((cat?.models&&cat.models.length)
    ?cat.models
    :builtin[active]?.models)||[];
  const hf=llmStatus?.hf||null;
  const llmPct=typeof llmStatus?.progress==='number'?llmStatus.progress:0;
  const sizeGb=((llm.size_mb||2400)/1024).toFixed(1);
  const btn='px-3 py-1.5 rounded text-xs font-medium border border-[#30363d] hover:border-[#484f58] text-[#e8eaed] disabled:opacity-40';
  const btnPrimary='px-3 py-1.5 rounded text-xs font-medium bg-[#3380FF] hover:bg-[#4d94ff] text-white disabled:opacity-40';

  return <div className="space-y-1">
    <SettingRow label="Active Provider">
      <select value={active} onChange={e=>update('providers','active',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]">
        {Object.keys(builtin).map(id=><option key={id} value={id}>{builtin[id]?.name||id}</option>)}
      </select>
    </SettingRow>
    <SettingRow label="Default Model">
      {models.length>1?(
        <select value={builtin[active]?.default_model||''} onChange={e=>{const u={...builtin,[active]:{...builtin[active],default_model:e.target.value}};update('providers','builtin',u)}} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-64">
          {!!builtin[active]?.default_model&&!models.includes(builtin[active].default_model)&&
            <option value={builtin[active].default_model}>{builtin[active].default_model}</option>}
          {models.map(m=><option key={m} value={m}>{m}</option>)}
        </select>
      ):(
        <input type="text" value={builtin[active]?.default_model||''} readOnly className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#8b949e] w-64"/>
      )}
    </SettingRow>
    <div className="flex items-center gap-2 px-1 text-[11px]">
      <button onClick={refreshModels} disabled={refreshing||wsState!=='connected'} className={btn}>
        {refreshing?'Refreshing…':'↻ Refresh models'}
      </button>
      <span className="text-[#8b949e]">
        {cat?.fetched_at?`updated ${relTime(cat.fetched_at)}`
          :cat?.checked_at?`checked ${relTime(cat.checked_at)}`
          :'not checked yet'}
        {cat?.discovered?.length?` · ${cat.discovered.length} from the provider`:''}
      </span>
    </div>
    {cat?.status==='error'&&cat.error&&
      <p className="text-xs text-[#d29922] px-1">⚠ Model list unavailable — {cat.error}</p>}
    {cat?.status==='skipped'&&cat.error&&
      <p className="text-[11px] text-[#8b949e] px-1">{cat.error}; showing the built-in list.</p>}
    <div className="mt-3 rounded-lg border border-[#30363d] p-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-[#e8eaed]">🎯 Model routing</p>
        <label className="flex items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer">
          <input type="checkbox" checked={p.auto_route!==false}
            onChange={e=>update('providers','auto_route',e.target.checked)}/>
          Auto-switch per task
        </label>
      </div>
      <p className="mt-1 text-[11px] text-[#8b949e]">
        Addled picks a model based on what the task needs. Any role left on
        <span className="font-mono"> default </span>
        uses <span className="font-mono">{builtin[active]?.default_model||'the provider default'}</span>.
      </p>
      {p.auto_route!==false&&
        <div className="mt-2 space-y-1">
          {ROUTE_ROLES.map(([role,label,hint])=>{
            const rcfg=builtin[active]?.roles||{};
            const cur=rcfg[role]||'';
            const fb=role==='vision'
              ?(builtin[active]?.vision_model||builtin[active]?.default_model||'')
              :(builtin[active]?.default_model||'');
            const opts=Array.from(new Set(
              [...(models||[]), fb, cur,
               ...(role==='vision'&&builtin[active]?.vision_model?[builtin[active].vision_model]:[])]
            )).filter(Boolean) as string[];
            return <div key={role} className="flex items-center gap-2">
              <span className="w-24 shrink-0 text-[11px] text-[#8b949e]" title={hint}>{label}</span>
              <select value={cur}
                onChange={e=>{const u={...builtin,[active]:{...builtin[active],roles:{...(builtin[active]?.roles||{}),[role]:e.target.value}}};update('providers','builtin',u)}}
                className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] flex-1 min-w-0">
                <option value="">Use default — {fb||'provider default'}</option>
                {opts.map(m=><option key={m} value={m}>{m}</option>)}
              </select>
              <span className="w-44 shrink-0 truncate text-[10px] text-[#8b949e]" title={`Effective model: ${cur||fb}`}>{cur||fb||'—'}</span>
            </div>;
          })}
        </div>}
    </div>
    {active==='openrouter'&&!builtin?.openrouter?.api_key&&
      <p className="text-xs text-[#d29922] px-1">⚠ OpenRouter needs an API key — paste it below (openrouter.ai/keys).</p>}

    <div className="mt-3 rounded-lg border border-[#30363d] p-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-[#e8eaed]">🧠 Local AI (llamafile)</p>
        <span className="text-[10px] text-[#8b949e]">
          {llmStatus?.running
            ? (llmStatus?.run_reason?`● running (${llmStatus.run_reason})`:'● running (on demand)')
            : llmStatus?.installed?'○ stopped':'— not installed'}
        </span>
      </div>
      <p className="mt-1 text-[11px] text-[#8b949e]">
        <span className="font-mono">{llm.model_file||'model'}</span> · about {sizeGb} GB
        {llmStatus?.installed_mb?` · on disk ${(llmStatus.installed_mb/1024).toFixed(1)} GB`:''}
        {llmStatus?` · ${llmStatus.free_mb} MB free`:''}
      </p>
      {llmStatus?.installed&&(
        <div className="mt-2 space-y-1">
          <label className="flex items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer">
            <input type="checkbox" checked={!!llmStatus?.keep_running}
              onChange={e=>{ send('localLlm.setOption',{key:'enabled',value:e.target.checked}).then(refresh).catch(()=>{}); }}/>
            Keep local AI running (always loaded)
          </label>
          <label className="flex items-center gap-2 text-[11px] text-[#8b949e] cursor-pointer">
            <input type="checkbox" checked={llmStatus?.autostart!==false}
              onChange={e=>{ send('localLlm.setOption',{key:'autostart',value:e.target.checked}).then(refresh).catch(()=>{}); }}/>
            Start automatically when selected as provider
          </label>
        </div>
      )}
      {llmStatus?.downloading&&(
        <div className="mt-2">
          <div className="h-1.5 w-full rounded bg-[#21262d] overflow-hidden">
            <div className="h-full bg-[#3380FF] transition-all" style={{width:`${Math.min(100,llmPct)}%`}}/>
          </div>
          <p className="mt-1 text-[10px] text-[#8b949e] truncate">{llmStatus?.detail} · {Math.round(llmPct)}%</p>
        </div>
      )}
      {!!llmStatus?.error&&<p className="mt-1 text-[10px] text-[#f85149] break-words">{llmStatus.error}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {!llmStatus?.installed&&
          <button disabled={busy!==null||wsState!=='connected'} onClick={()=>act('localLlm.installApprove','install')} className={btnPrimary}>
            {busy==='install'?'⏳ starting…':`⬇ Download (${sizeGb} GB)`}
          </button>}
        {llmStatus?.installed&&!llmStatus?.running&&
          <button disabled={busy!==null} onClick={()=>act('localLlm.start','start')} className={btn}>▶ Start</button>}
        {llmStatus?.running&&
          <button disabled={busy!==null} onClick={()=>act('localLlm.stop','stop')} className={btn}>■ Stop</button>}
        {llmStatus?.installed&&
          <button disabled={busy!==null} onClick={()=>act('localLlm.remove','remove')} className={btn}>🗑 Remove</button>}
        <button disabled={busy!==null} onClick={refresh} className={btn}>↻ Refresh</button>
      </div>
      <p className="mt-2 text-[10px] text-[#484f58] break-all">{llmStatus?.dir||''}</p>
    </div>

    <div className="mt-3 rounded-lg border border-[#30363d] p-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-[#e8eaed]">🤗 Hugging Face (Local)</p>
        <span className="text-[10px] text-[#8b949e]">{hf?.deps_ready?'● ready':'— needs torch'}</span>
      </div>
      <p className="mt-1 text-[11px] text-[#8b949e]">
        Runs <span className="font-mono break-all">{hf?.model_id||builtin?.huggingface?.default_model||''}</span> in-process.
        {hf?.downloaded?' Model is downloaded.':' Model downloads on first use.'}
      </p>
      {hf&&!hf.deps_ready&&<p className="mt-1 text-[10px] text-[#d29922] break-words">{hf.error||'torch + transformers are required.'}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {hf&&!hf.deps_ready&&
          <button disabled={busy!==null||wsState!=='connected'} onClick={()=>act('localLlm.installHfDeps','hf')} className={btnPrimary}>
            {busy==='hf'?'⏳ installing…':'⬇ Install torch + transformers'}
          </button>}
        <button disabled={busy!==null} onClick={()=>act('hf.unload','unload')} className={btn}>Free RAM</button>
      </div>
    </div>

    {Object.entries(builtin).filter(([,cfg]:[string,any])=>!cfg.local).map(([id,cfg]:[string,any])=>
      <SettingRow key={id} label={`${cfg.name} API Key`}>
        <div className="flex items-center gap-2">
          <input type="password" value={cfg.api_key||''} onChange={e=>{const u={...builtin,[id]:{...cfg,api_key:e.target.value}};update('providers','builtin',u)}}
            placeholder="sk-..." className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-56 font-mono"/>
          {cfg.api_key&&<span className="text-xs text-[#3fb950]">✓ Set</span>}
        </div>
      </SettingRow>)}
  </div>;
}

function CharacterSection({ settings, update, saving, status }: any) {
  const c=settings?.character||{}, shapes=['triangle','circle','diamond','hexagon','star','square'];
  return <div className="space-y-1">
    <SettingRow label="Agent Name"><input type="text" value={settings?.agent_name||'Addled'} onChange={e=>update('agent_name','',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-40"/></SettingRow>
    <SettingRow label="Shape"><div className="flex gap-1.5">{shapes.map(s=><button key={s} onClick={()=>update('character','shape',s)} className={`px-2.5 py-1 rounded text-xs border transition-colors ${c.shape===s?'border-[#3380FF] bg-[#1f6feb22] text-[#3380FF]':'border-[#30363d] text-[#8b949e] hover:border-[#484f58]'}`}>{s}</button>)}</div></SettingRow>
    <SettingRow label="Color"><div className="flex items-center gap-2"><input type="color" value={c.color||'#3380FF'} onChange={e=>update('character','color',e.target.value)} className="w-8 h-8 rounded cursor-pointer border-0"/><input type="text" value={c.color||'#3380FF'} onChange={e=>update('character','color',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-sm text-[#e8eaed] w-24 font-mono"/></div></SettingRow>
    <SettingRow label="Size" description={`${c.size||64}px`}><input type="range" min={32} max={128} value={c.size||64} onChange={e=>update('character','size',parseInt(e.target.value))} className="w-32"/></SettingRow>
    <SettingRow label="Glow" description={`${Math.round((c.glow_intensity||0.6)*100)}%`}><input type="range" min={0} max={100} value={Math.round((c.glow_intensity||0.6)*100)} onChange={e=>update('character','glow_intensity',parseInt(e.target.value)/100)} className="w-32"/></SettingRow>
    <SettingRow label="Eyes"><button onClick={()=>update('character','eyes',c.eyes===false)} className={`w-10 h-5 rounded-full transition-colors ${c.eyes!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${c.eyes!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>
    <SettingRow label="Speed"><select value={c.movement_speed||'medium'} onChange={e=>update('character','movement_speed',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="slow">Slow</option><option value="medium">Medium</option><option value="fast">Fast</option></select></SettingRow>
  </div>;
}

const LANGUAGES: [string, string][] = [
  ['en','English'],['id','Indonesian'],['zh','Chinese'],['ja','Japanese'],
  ['ko','Korean'],['es','Spanish'],['fr','French'],['hi','Hindi'],
  ['pt','Portuguese'],['auto','Auto (detect)'],
];

function VoiceSection({ settings, update, saving, status, send, connected }: any) {
  const v=settings?.voice||{};
  const engine=v.tts_engine||'edge';
  const language=v.language||'auto';
  const [catalogue,setCatalogue]=useState<any>(null);
  const [loadError,setLoadError]=useState('');
  const [busy,setBusy]=useState(false);
  const [showAllEdge,setShowAllEdge]=useState(false);
  const [customEdge,setCustomEdge]=useState(false);
  const [customKokoro,setCustomKokoro]=useState(false);

  const load=useCallback(async(force?:boolean)=>{
    if(!connected) return;
    setBusy(true);
    try{
      const r=await send('voice.voices', force?{force:true}:{});
      if(r?.success){setCatalogue(r); setLoadError('');}
      else setLoadError(r?.error||'Voice list unavailable');
    }catch(e:any){setLoadError(e?.message||'Voice list unavailable');}
    finally{setBusy(false);}
  },[connected,send]);

  useEffect(()=>{load();},[load]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectCls="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] max-w-[320px]";
  const edgeVoices:any[]=catalogue?.edge||[];
  const kokoroVoices:any[]=catalogue?.kokoro||[];
  const kokoroLanguages:string[]=catalogue?.kokoro_languages||[];

  // A few hundred Edge voices is unusable as a flat list, so group by locale
  // and keep the configured language in view unless asked for everything.
  const edgeFiltered=(showAllEdge||language==='auto')
    ? edgeVoices
    : edgeVoices.filter(x=>x.language===language);
  const edgeGroups:Record<string,any[]>={};
  for(const x of edgeFiltered){
    if(!edgeGroups[x.locale]) edgeGroups[x.locale]=[];
    edgeGroups[x.locale].push(x);
  }
  const kokoroGroups:Record<string,any[]>={};
  for(const x of kokoroVoices){
    const key=x.language_label||x.language;
    if(!kokoroGroups[key]) kokoroGroups[key]=[];
    kokoroGroups[key].push(x);
  }
  // Kokoro v1.0 covers 9 languages and has no Indonesian voice at all.
  const kokoroMissing=kokoroVoices.length>0&&language!=='auto'
    &&!kokoroLanguages.includes(language);

  const edgeValue=v.tts_voice||'en-US-JennyNeural';
  const kokoroValue=v.kokoro_voice||'af_heart';
  const edgeKnown=edgeVoices.some(x=>x.id===edgeValue);
  const kokoroKnown=kokoroVoices.some(x=>x.id===kokoroValue);
  const edgeCount=edgeVoices.length;
  const sourceNote=catalogue&&catalogue.edge_source!=='live'
    ? ` · ${catalogue.edge_source}` : '';

  const toggleCls=(on:boolean)=>`w-10 h-5 rounded-full transition-colors ${on?'bg-[#3380FF]':'bg-[#30363d]'}`;
  const knobCls=(on:boolean)=>`w-4 h-4 bg-white rounded-full transition-transform ${on?'translate-x-5':'translate-x-0.5'}`;

  return <div className="space-y-1">
    <SettingRow label="TTS Engine"><select value={engine} onChange={e=>update('voice','tts_engine',e.target.value)} className={selectCls}><option value="kokoro">Kokoro (Local, offline)</option><option value="edge">Edge TTS (Free, online)</option></select></SettingRow>
    <SettingRow label="Language" description="What to speak — the voice lists follow this"><select value={language} onChange={e=>update('voice','language',e.target.value)} className={selectCls}>{LANGUAGES.map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></SettingRow>

    <SettingRow label="Edge Voice" description={busy?'Loading voices…':(edgeCount?`${edgeCount} available${sourceNote}`:undefined)}>
      {customEdge
        ? <div className="flex items-center gap-2">
            <input type="text" value={edgeValue} onChange={e=>update('voice','tts_voice',e.target.value)} className={`${selectCls} w-56`}/>
            <button onClick={()=>setCustomEdge(false)} className="text-xs text-[#8b949e] hover:text-[#e8eaed]">done</button>
          </div>
        : <select value={edgeKnown?edgeValue:'__custom__'} onChange={e=>{const val=e.target.value; if(val==='__custom__') setCustomEdge(true); else update('voice','tts_voice',val);}} className={`${selectCls} w-56`}>
            {!edgeKnown&&<option value="__custom__">{edgeValue} (current)</option>}
            {Object.keys(edgeGroups).sort().map(locale=>(
              <optgroup key={locale} label={locale}>
                {edgeGroups[locale].map(x=><option key={x.id} value={x.id}>{x.label}{x.gender?` — ${x.gender}`:''}</option>)}
              </optgroup>
            ))}
            <option value="__custom__">Custom…</option>
          </select>}
    </SettingRow>

    {language!=='auto'&&edgeCount>0&&(
      <SettingRow label="Show every language" description="Edge voices for all locales, not just this one">
        <button onClick={()=>setShowAllEdge(s=>!s)} className={toggleCls(showAllEdge)}><div className={knobCls(showAllEdge)}/></button>
      </SettingRow>
    )}

    <SettingRow label="Kokoro Voice" description={busy?'Loading voices…':(kokoroVoices.length?`${kokoroVoices.length} installed`:'Not installed')}>
      {customKokoro
        ? <div className="flex items-center gap-2">
            <input type="text" value={kokoroValue} onChange={e=>update('voice','kokoro_voice',e.target.value)} className={`${selectCls} w-44`}/>
            <button onClick={()=>setCustomKokoro(false)} className="text-xs text-[#8b949e] hover:text-[#e8eaed]">done</button>
          </div>
        : <select value={kokoroKnown?kokoroValue:'__custom__'} onChange={e=>{const val=e.target.value; if(val==='__custom__') setCustomKokoro(true); else update('voice','kokoro_voice',val);}} className={`${selectCls} w-44`}>
            {!kokoroKnown&&<option value="__custom__">{kokoroValue} (current)</option>}
            {Object.keys(kokoroGroups).sort().map(lang=>(
              <optgroup key={lang} label={lang}>
                {kokoroGroups[lang].map(x=><option key={x.id} value={x.id}>{x.label} — {x.gender}</option>)}
              </optgroup>
            ))}
            <option value="__custom__">Custom…</option>
          </select>}
    </SettingRow>

    {kokoroMissing&&(
      <p className="text-xs text-[#d29922] py-2">
        Kokoro has no {language} voice — its {kokoroVoices.length} voices cover {
          kokoroLanguages.join(', ')}. Pick an Edge voice to speak {language}, or
        change the language above.
      </p>
    )}
    {!busy&&catalogue&&edgeCount===0&&(
      <p className="text-xs text-[#d29922] py-2">
        Could not list Edge voices{loadError?`: ${loadError}`:''}. Showing nothing rather than a guess — try Refresh.
      </p>
    )}
    {loadError&&edgeCount>0&&(
      <p className="text-xs text-[#8b949e] py-2">
        Edge list could not be refreshed ({loadError}); showing the last known set.
      </p>
    )}
    <SettingRow label="Voice list"><button onClick={()=>load(true)} disabled={busy} className="text-xs px-2 py-1 rounded border border-[#30363d] hover:border-[#8b949e] text-[#8b949e] hover:text-[#e8eaed] disabled:opacity-50">{busy?'Refreshing…':'Refresh'}</button></SettingRow>

    <SettingRow label="STT Engine"><select value={v.stt_engine||'sensevoice'} onChange={e=>update('voice','stt_engine',e.target.value)} className={selectCls}><option value="sensevoice">SenseVoice (falls back to Whisper)</option><option value="whisper">Faster-Whisper</option></select></SettingRow>
    <SettingRow label="STT Model"><select value={v.stt_model||'small'} onChange={e=>update('voice','stt_model',e.target.value)} className={selectCls}><option value="tiny">tiny (fast)</option><option value="small">small (accurate)</option></select></SettingRow>
    <SettingRow label="Wake Word"><input type="text" value={v.wake_word||'hey addled'} onChange={e=>update('voice','wake_word',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-40"/></SettingRow>
    <SettingRow label="Auto TTS"><button onClick={()=>update('voice','auto_tts',v.auto_tts===false)} className={toggleCls(v.auto_tts!==false)}><div className={knobCls(v.auto_tts!==false)}/></button></SettingRow>
    <SettingRow label="VAD (Silero)"><button onClick={()=>update('voice','vad_enabled',v.vad_enabled===false)} className={toggleCls(v.vad_enabled!==false)}><div className={knobCls(v.vad_enabled!==false)}/></button></SettingRow>
  </div>;
}

function SafetySection({ settings, update, saving, status }: any) {
  const s=settings?.safety||{};
  const Toggle=({label,desc,skey}:{label:string;desc?:string;skey:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('safety',skey,!s[skey])} className={`w-10 h-5 rounded-full transition-colors ${s[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${s[skey]?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  return <div className="space-y-1">
    <SettingRow label="File Access"><select value={s.file_access_mode||'workspace_only'} onChange={e=>update('safety','file_access_mode',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="workspace_only">Workspace Only</option><option value="custom">Custom</option><option value="unrestricted">Full</option></select></SettingRow>
    <SettingRow label="Kill Switch"><input type="text" value={s.kill_switch_hotkey||'ctrl+shift+alt+k'} onChange={e=>update('safety','kill_switch_hotkey',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-44 font-mono"/></SettingRow>
    <Toggle label="Clipboard Filter" desc="Redact secrets from clipboard" skey="clipboard_filter"/>
    <Toggle label="Prompt Guard" desc="Block injection attempts" skey="prompt_guard"/>
    <SettingRow label="Quiet Hours"><div className="flex items-center gap-2 text-sm text-[#e8eaed]"><input type="time" value={s.quiet_hours_start||'22:00'} onChange={e=>update('safety','quiet_hours_start',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-sm text-[#e8eaed]"/><span className="text-[#8b949e]">to</span><input type="time" value={s.quiet_hours_end||'07:00'} onChange={e=>update('safety','quiet_hours_end',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-2 py-1 text-sm text-[#e8eaed]"/></div></SettingRow>
    <Toggle label="Meeting Auto-Sleep" skey="meeting_auto_sleep"/>
    <Toggle label="Gaming Auto-Sleep" skey="gaming_auto_sleep"/>
  </div>;
}

function NotificationsSection({ settings, update, saving, status }: any) {
  const n=settings?.notifications||{};
  return <div className="space-y-1">
    <SettingRow label="Bubble Duration" description={`${n.bubble_duration_s||8}s`}><input type="range" min={3} max={30} value={n.bubble_duration_s||8} onChange={e=>update('notifications','bubble_duration_s',parseInt(e.target.value))} className="w-32"/></SettingRow>
    <SettingRow label="Max Bubbles"><input type="number" min={1} max={10} value={n.max_bubbles||3} onChange={e=>update('notifications','max_bubbles',parseInt(e.target.value))} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-20"/></SettingRow>
    <SettingRow label="Show State"><button onClick={()=>update('notifications','show_character_state',n.show_character_state===false)} className={`w-10 h-5 rounded-full transition-colors ${n.show_character_state!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${n.show_character_state!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>
  </div>;
}

function ObservationSection({ settings, update, saving, status }: any) {
  const o=settings?.observation||{};
  const Toggle=({label,desc,skey}:{label:string;desc?:string;skey:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('observation',skey,!o[skey])} className={`w-10 h-5 rounded-full transition-colors ${o[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${o[skey]?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  return <div className="space-y-1">
    <Toggle label="Deep vision" desc="Periodically screenshot your screen and analyze it with the visual model (respects privacy zones)" skey="deep_vision"/>
    <SettingRow label="Deep vision interval" description={`Every ${o.deep_interval_s||300}s`}><select value={o.deep_interval_s||300} onChange={e=>update('observation','deep_interval_s',parseInt(e.target.value))} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value={60}>1 min</option><option value={120}>2 min</option><option value={300}>5 min</option><option value={600}>10 min</option><option value={1800}>30 min</option></select></SettingRow>
    <Toggle label="Speak insights" desc="Read proactive insights out loud (Auto TTS must be on)" skey="voice_insights"/>
    <SettingRow label="Snapshot memory"><span className="text-xs text-[#8b949e]">{o.snapshot_store_enabled===false?'Disabled':'Enabled'} · max {o.snapshot_max||30} · every {o.snapshot_min_interval_s||60}s</span></SettingRow>
  </div>;
}

function MemorySection({ settings, update, saving, status }: any) {
  const m=settings?.memory||{};
  const Toggle=({label,desc,skey}:{label:string;desc?:string;skey:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('memory',skey,!m[skey])} className={`w-10 h-5 rounded-full transition-colors ${m[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${m[skey]?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  return <div className="space-y-1">
    <Toggle label="Semantic recall" desc="Local MiniLM embeddings for meaning-based memory search (falls back to hashed keywords when the model is missing)" skey="semantic_embeddings"/>
    <Toggle label="Hybrid search" desc="Fuse semantic + exact-keyword matching for stronger recall" skey="hybrid_search"/>
    <Toggle label="Auto memory notes" desc="Let the agent occasionally write durable facts about you in the background (uses provider credits)" skey="auto_facts"/>
    <Toggle label="Knowledge graph" desc="Extract fact triples (subject → relation → object) from conversations in the background (uses provider credits)" skey="graph_extract"/>
    <SettingRow label="Vector Store"><span className="text-xs text-[#8b949e]">SQLite + numpy (384-dim)</span></SettingRow>
  </div>;
}

function WikiSection({ settings, update, saving, status }: any) {
  const l=settings?.links||{};
  const w=settings?.wiki||{};
  const Toggle=(group:'links'|'wiki', cfg:any)=>{
    const T=({label,desc,skey}:{label:string;desc?:string;skey:string})=>(
      <SettingRow label={label} description={desc}>
        <button onClick={()=>update(group,skey,!cfg[skey])} className={`w-10 h-5 rounded-full transition-colors ${cfg[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}>
          <div className={`w-4 h-4 bg-white rounded-full transition-transform ${cfg[skey]?'translate-x-5':'translate-x-0.5'}`}/>
        </button>
      </SettingRow>
    );
    return T;
  };
  const Num=(group:'links'|'wiki', cfg:any, min:number, max:number)=>{
    return ({label,desc,skey}:{label:string;desc?:string;skey:string})=>(
      <SettingRow label={label} description={desc}>
        <input type="number" min={min} max={max} value={cfg[skey] ?? ''}
          onChange={(e)=>update(group,skey,Number(e.target.value))}
          className="w-20 bg-[#161b22] border border-[#30363d] rounded px-2 py-1 text-xs text-[#e8eaed] focus:outline-none focus:border-[#3380FF]"/>
      </SettingRow>
    );
  };
  const LinkToggle=Toggle('links', l);
  const LinkNum=Num('links', l, 0, 50);
  const WikiToggle=Toggle('wiki', w);
  const WikiNum=Num('wiki', w, 1, 20);
  return <div className="space-y-1">
    <p className="text-xs text-[#8b949e] mb-2">One graph connects every memory store (facts, triples, memories, summaries, journal days, wiki pages) to each other and to files on disk.</p>
    <LinkToggle label="Memory relations" desc="Record and use relations between memories, and between memories and files" skey="enabled"/>
    <LinkToggle label="Link files automatically" desc="Scan new memories for file paths and link them to the file" skey="auto_file_links"/>
    <LinkToggle label="Record provenance" desc="Link a memory back to whatever produced it (a conversation turn, a journal day, a file)" skey="auto_provenance"/>
    <LinkToggle label="Flag rephrased duplicates" desc="Daily scan for facts that say the same thing in different words, linked as same_as" skey="dedup_links"/>
    <LinkNum label="Relation hops to follow" desc="How far to walk the graph when recalling related items (1-4)" skey="related_depth"/>
    <LinkNum label="Related items to inject" desc="Maximum related items added to a chat prompt" skey="related_inject"/>

    <div className="border-t border-[#30363d] my-3"/>
    <p className="text-xs text-[#8b949e] mb-2">The wiki is a set of markdown pages Addled maintains from your own documents, so its pages can be cited as fact instead of guessed at.</p>
    <WikiToggle label="Wiki" desc="Maintain and use knowledge pages" skey="enabled"/>
    <WikiToggle label="Ingest files automatically" desc="Off by default — Addled never reads your files into the wiki unless you ask it to" skey="auto_ingest"/>
    <WikiNum label="Pages to inject" desc="How many matching pages to give the model per message" skey="inject_top_k"/>
    <WikiNum label="Prompt budget (chars)" desc="Upper bound on wiki text added to one message" skey="max_chars"/>
    <SettingRow label="Wiki folder"><span className="text-xs text-[#8b949e] break-all">{w.dir || 'default (backend/memory/wiki)'}</span></SettingRow>
  </div>;
}

function ToolsSection({ settings, update, saving, status, send, connected }: any) {
  const t=settings?.tools||{};
  const [rtk, setRtk] = useState<any>(null);
  useEffect(() => {
    if (connected) send('system.rtkStatus', {}).then((r: any) => setRtk(r || null)).catch(() => {});
  }, [connected, send]);
  const Toggle=({label,desc,skey}:{label:string;desc?:string;skey:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('tools',skey,!t[skey])} className={`w-10 h-5 rounded-full transition-colors ${t[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${t[skey]?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  return <div className="space-y-1">
    <Toggle label="Token saver (RTK)" desc="Auto-compress git / pip / pytest / npm / gh / docker command output before it reaches the model — saves provider credits" skey="rtk_enabled"/>
    <SettingRow label="RTK binary">
      <span className={`text-xs ${rtk?.available ? 'text-[#3fb950]' : 'text-[#d29922]'}`}>
        {rtk ? (rtk.available ? '✓ bundled — compression active' : 'Not bundled — commands run uncompressed') : 'Checking…'}
      </span>
    </SettingRow>
  </div>;
}

function BrowserSection({ settings, update, saving, status, send, connected }: any) {
  const b=settings?.browser||{};
  const [bst, setBst] = useState<any>(null);
  useEffect(() => {
    if (connected) send('browser.status', {}).then((r: any) => setBst(r || null)).catch(() => {});
  }, [connected, send]);
  const Toggle=({label,desc,skey}:{label:string;desc?:string;skey:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('browser',skey,!b[skey])} className={`w-10 h-5 rounded-full transition-colors ${b[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${b[skey]?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  const Status=({label, ok}:{label:string;ok?:boolean})=><p className="text-xs mb-0.5">{ok?'✓':'·'} {label}</p>;
  return <div className="space-y-1">
    <Toggle label="Attach to my browser" desc="Use your running Chrome/Edge via its debug port instead of Addled's own browser. Requires launching the browser with --remote-debugging-port." skey="user_browser"/>
    <Toggle label="Read-only" desc="Only read pages and take screenshots — block clicking, typing and navigation in your browser" skey="readonly"/>
    <SettingRow label="Debug port"><input type="number" value={b.cdp_port||9222} onChange={e=>update('browser','cdp_port',parseInt(e.target.value)||9222)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-24"/></SettingRow>
    <SettingRow label="Engine"><select value={b.engine||'auto'} onChange={e=>update('browser','engine',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="auto">Auto</option><option value="cdp">My browser (CDP)</option><option value="playwright">Playwright</option><option value="http">HTTP only</option></select></SettingRow>
    <SettingRow label="Framework mode" description="browser-use handles open-ended multi-step tasks — used only when installed AND an LLM is available"><select value={b.task_mode||'auto'} onChange={e=>update('browser','task_mode',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="auto">Auto (conditional)</option><option value="off">Off</option><option value="always">Always</option></select></SettingRow>
    <SettingRow label="Auto-install backends" description="When a task needs Playwright or browser-use and it's missing: ask first (recommended), install silently, or never"><select value={b.auto_install||'ask'} onChange={e=>update('browser','auto_install',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="ask">Ask first</option><option value="off">Never</option><option value="auto">Auto</option></select></SettingRow>
    <SettingRow label="Backend status">
      <div>
        <Status label="Playwright installed" ok={bst?.playwright_available}/>
        {bst?.installing_playwright && <p className="text-xs text-[#d29922]">⟳ downloading Playwright + Chromium…</p>}
        <Status label="Browser attach available" ok={bst?.cdp_available}/>
        <Status label="browser-use installed" ok={bst?.framework_available}/>
        {bst?.installing_framework && <p className="text-xs text-[#d29922]">⟳ installing browser-use…</p>}
        <Status label="LLM available" ok={bst?.llm_available}/>
      </div>
    </SettingRow>
    <SettingRow label="Optional installs" description="Playwright and browser-use are optional — install via scripts/fetch_playwright.py and scripts/fetch_browser_use.py (see README)."><span className="text-xs text-[#8b949e]">See README</span></SettingRow>
  </div>;
}

function DesktopSection({ settings, update, saving, status, send, connected }: any) {
  const d=settings?.desktop||{};
  const [state, setState] = useState<any>(null);
  useEffect(() => {
    if (connected) send('desktop.status', {}).then((r: any) => setState(r || null)).catch(() => {});
  }, [connected, send]);
  const Toggle=({label,desc,skey}:{label:string;desc?:string;skey:string})=><SettingRow label={label} description={desc}><button onClick={()=>update('desktop',skey,!d[skey])} className={`w-10 h-5 rounded-full transition-colors ${d[skey]?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${d[skey]?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>;
  return <div className="space-y-1">
    <Toggle label="Desktop control" desc="Let the agent move the mouse, click, type and scroll on your desktop (OFF by default — the safest choice)" skey="allow_input"/>
    <Toggle label="Ask before each session" desc="Require a dashboard approval once per session (expires after the timeout)" skey="require_session_approval"/>
    <Toggle label="Extended hotkeys" desc="Also allow alt+tab, win+d, alt+f4 (riskier — keep off unless you need it)" skey="allow_extended_hotkeys"/>
    <SettingRow label="Max typed characters"><input type="number" min={50} max={5000} value={d.max_type_chars||500} onChange={e=>update('desktop','max_type_chars',parseInt(e.target.value)||500)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-24"/></SettingRow>
    <SettingRow label="Permission status">
      <span className={`text-xs ${state?.granted ? 'text-[#3fb950]' : 'text-[#8b949e]'}`}>
        {state?.granted ? '✓ granted this session' : 'not granted'}
      </span>
    </SettingRow>
    <SettingRow label="Emergency stop">
      <span className="text-xs text-[#8b949e]">Mouse to the top-left screen corner aborts input (pyautogui FAILSAFE) · Kill switch: Ctrl+Shift+Alt+K</span>
    </SettingRow>
  </div>;
}

function IntegrationsSection({ settings, update, saving, status }: any) {
  const i=settings?.integrations||{};
  return <div className="space-y-1">
    <SettingRow label="Calendar" description={i.calendar_provider?'Connected':'Not connected'}><button className={`px-3 py-1.5 text-sm rounded ${i.calendar_provider?'bg-[#21262d] hover:bg-[#30363d] text-[#e8eaed]':'bg-[#3380FF] hover:bg-[#4d94ff] text-white'}`}>{i.calendar_provider?'Disconnect':'Connect'}</button></SettingRow>
    <SettingRow label="IMAP"><input type="text" value={i.email_imap||''} placeholder="imap.gmail.com" onChange={e=>update('integrations','email_imap',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-48"/></SettingRow>
    <SettingRow label="SMTP"><input type="text" value={i.email_smtp||''} placeholder="smtp.gmail.com" onChange={e=>update('integrations','email_smtp',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-48"/></SettingRow>
  </div>;
}

function AppearanceSection() {
  return <div className="space-y-1">
    <SettingRow label="Theme"><select className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option>Dark</option><option>Light</option><option>System</option></select></SettingRow>
    <SettingRow label="Accent"><input type="color" defaultValue="#3380FF" className="w-8 h-8 rounded cursor-pointer border-0"/></SettingRow>
  </div>;
}

function AboutSection() {
  return <div className="space-y-4">
    <div className="text-center py-8"><div className="text-4xl mb-3">△</div><h3 className="text-lg font-semibold">Addled</h3><p className="text-sm text-[#8b949e] mt-1">AI Desktop Companion</p><p className="text-xs text-[#484f58] mt-1">Version 1.0.0</p></div>
    <div className="space-y-1">
      <SettingRow label="Repository"><a href="https://github.com/8kunoir8/Addled" target="_blank" rel="noopener noreferrer" className="text-sm text-[#3380FF] hover:underline">github.com/8kunoir8/Addled</a></SettingRow>
      <SettingRow label="License"><span className="text-sm text-[#8b949e]">MIT</span></SettingRow>
      <SettingRow label="Stack"><span className="text-sm text-[#8b949e]">Python + PyQt6 + Electron + Next.js</span></SettingRow>
      <SettingRow label="Author"><span className="text-sm text-[#8b949e]">Kunoir</span></SettingRow>
    </div>
  </div>;
}
