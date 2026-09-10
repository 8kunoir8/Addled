'use client';

import { useState, useEffect, useCallback } from 'react';
import { useWS } from '@/lib/useWS';

type SettingsData = Record<string, any>;

const SECTION_ICONS: Record<string, string> = {
  providers: '🔌', character: '🎭', voice: '🎤', safety: '🛡️',
  notifications: '🔔', memory: '🧠', integrations: '🔗', appearance: '🎨',
  observation: '👁', about: 'ℹ️',
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

  const sections = ['providers','character','voice','safety','notifications','observation','memory','integrations','appearance','about'];

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
        {activeSection==='voice'&&<VoiceSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='safety'&&<SafetySection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='notifications'&&<NotificationsSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='observation'&&<ObservationSection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
        {activeSection==='memory'&&<MemorySection settings={settings} update={updateSetting} saving={saving} status={saveStatus}/>}
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

function ProvidersSection({ settings, update, saving, status }: any) {
  const p=settings?.providers||{}, builtin=p.builtin||{}, active=p.active||'deepseek';
  return <div className="space-y-1">
    <SettingRow label="Active Provider">
      <select value={active} onChange={e=>update('providers','active',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]">
        {Object.keys(builtin).map(id=><option key={id} value={id}>{builtin[id]?.name||id}</option>)}
      </select>
    </SettingRow>
    {Object.entries(builtin).map(([id,cfg]:[string,any])=>
      <SettingRow key={id} label={`${cfg.name} API Key`}>
        <div className="flex items-center gap-2">
          <input type="password" value={cfg.api_key||''} onChange={e=>{const u={...builtin,[id]:{...cfg,api_key:e.target.value}};update('providers','builtin',u)}}
            placeholder="sk-..." className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-56 font-mono"/>
          {cfg.api_key&&<span className="text-xs text-[#3fb950]">✓ Set</span>}
        </div>
      </SettingRow>)}
    <SettingRow label="Default Model"><input type="text" value={builtin[active]?.default_model||''} readOnly className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#8b949e] w-48"/></SettingRow>
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

function VoiceSection({ settings, update, saving, status }: any) {
  const v=settings?.voice||{};
  return <div className="space-y-1">
    <SettingRow label="TTS Engine"><select value={v.tts_engine||'edge'} onChange={e=>update('voice','tts_engine',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="edge">Edge TTS (Free)</option><option value="elevenlabs">ElevenLabs</option><option value="openai">OpenAI TTS</option><option value="cosyvoice">CosyVoice (Local)</option></select></SettingRow>
    <SettingRow label="Wake Word"><input type="text" value={v.wake_word||'hey addled'} onChange={e=>update('voice','wake_word',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed] w-40"/></SettingRow>
    <SettingRow label="Auto TTS"><button onClick={()=>update('voice','auto_tts',v.auto_tts===false)} className={`w-10 h-5 rounded-full transition-colors ${v.auto_tts!==false?'bg-[#3380FF]':'bg-[#30363d]'}`}><div className={`w-4 h-4 bg-white rounded-full transition-transform ${v.auto_tts!==false?'translate-x-5':'translate-x-0.5'}`}/></button></SettingRow>
    <SettingRow label="Language"><select value={v.language||'en'} onChange={e=>update('voice','language',e.target.value)} className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-sm text-[#e8eaed]"><option value="en">English</option><option value="zh">Chinese</option><option value="ja">Japanese</option><option value="ko">Korean</option><option value="auto">Auto</option></select></SettingRow>
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
    <SettingRow label="Vector Store"><span className="text-xs text-[#8b949e]">SQLite + numpy (384-dim)</span></SettingRow>
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
