'use client';

import { useState } from 'react';
import { useWS } from '@/lib/useWS';

interface CodeFile { name: string; path: string; language: string; size: number; }

export default function CodePage() {
  const { state: wsState, send } = useWS();
  const [workspacePath, setWorkspacePath] = useState('');
  const [files, setFiles] = useState<CodeFile[]>([]);
  const [selectedFile, setSelectedFile] = useState<CodeFile | null>(null);
  const [fileContent, setFileContent] = useState('');
  const [instruction, setInstruction] = useState('');
  const [bound, setBound] = useState(false);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<{editId:string; filePath:string; diff:any}|null>(null);
  const [diffError, setDiffError] = useState('');
  const [applied, setApplied] = useState('');

  const handleBind = async () => {
    if (!workspacePath.trim() || wsState !== 'connected') return;
    setLoading(true);
    try {
      const r = await send('code.bind', { folderPath: workspacePath.trim() });
      setFiles(r?.files || []);
      setBound(true);
    } catch { setBound(false); }
    setLoading(false);
  };

  const handleFileClick = async (file: CodeFile) => {
    setSelectedFile(file);
    try {
      const r = await send('code.read', { workspaceId: workspacePath, filePath: file.path });
      setFileContent(r?.content || '(Empty file)');
    } catch { setFileContent('// Error loading file'); }
  };

  // ---- edit → review → apply -------------------------------------------------
  // code.edit only creates something code.apply can use when it is given the
  // target file: without it the backend has no original to diff and stores no
  // pending edit. The page used to send no filePath, so nothing was ever
  // applicable, and it replaced the file view with a "coming in Phase 5" note.
  const handleEdit = async () => {
    if (!instruction.trim() || wsState !== 'connected' || !selectedFile) return;
    setLoading(true); setDiffError(''); setApplied(''); setPending(null);
    try {
      const r = await send('code.edit', {
        workspaceId: workspacePath,
        filePath: selectedFile.path,
        instruction: instruction.trim(),
      });
      if (r?.editId && r?.diffs?.length) {
        setPending({ editId: r.editId, filePath: selectedFile.path, diff: r.diffs[0] });
        setInstruction('');
      } else if (r?.diffs?.length) {
        setDiffError(r?.message || r?.diffs?.[0]?.message || 'The edit could not be prepared for this file.');
      } else {
        setDiffError('The model returned no change.');
      }
    } catch (e: any) { setDiffError(e?.message || 'Edit failed'); }
    finally { setLoading(false); }
  };

  const handleApply = async () => {
    if (!pending) return;
    setLoading(true); setDiffError('');
    try {
      const r = await send('code.apply', {
        workspaceId: workspacePath,
        filePath: pending.filePath,
        editId: pending.editId,
      });
      if (r?.success) {
        setApplied(`Applied${r.backup ? ` — backup kept at ${r.backup}` : ''}`);
        setPending(null);
        const fresh = await send('code.read', { workspaceId: workspacePath, filePath: pending.filePath });
        setFileContent(fresh?.content || '(Empty file)');
      } else {
        setDiffError(r?.error || 'Apply failed');
      }
    } catch (e: any) { setDiffError(e?.message || 'Apply failed'); }
    finally { setLoading(false); }
  };

  const renderDiff = (diff: any) => {
    const raw = String(diff?.raw_diff || '').split('\n').filter(Boolean);
    if (!raw.length) return <span className="text-[#8b949e]">{diff?.message || 'No diff available.'}</span>;
    return raw.map((line: string, i: number) => (
      <div key={i} className={
        line.startsWith('+') && !line.startsWith('+++') ? 'text-[#3fb950]'
        : line.startsWith('-') && !line.startsWith('---') ? 'text-[#f85149]'
        : line.startsWith('@@') ? 'text-[#58a6ff]' : 'text-[#8b949e]'}>{line}</div>
    ));
  };

  const getLanguageColor = (lang: string) => {
    const colors: Record<string,string> = { typescript:'#3178c6', javascript:'#f7df1e', python:'#3572A5', rust:'#dea584', go:'#00ADD8', java:'#b07219', csharp:'#178600', html:'#e34c26', css:'#563d7c', json:'#292929', markdown:'#083fa1' };
    return colors[lang] || '#8b949e';
  };

  return (
    <div className="flex h-full">
      {/* File tree */}
      <div className="w-56 border-r border-[#30363d] flex flex-col">
        <div className="p-3 border-b border-[#30363d]">
          {!bound ? (
            <div className="space-y-2">
              <input value={workspacePath} onChange={e=>setWorkspacePath(e.target.value)} placeholder="Folder path..."
                className="w-full bg-[#0d1117] border border-[#30363d] rounded px-2 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
              <button onClick={handleBind} disabled={!workspacePath.trim()||loading||wsState!=='connected'}
                className="w-full bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-2 py-1.5 text-xs font-medium">
                {loading?'Binding...':'Bind Workspace'}
              </button>
            </div>
          ) : (
            <div className="flex items-center justify-between text-xs">
              <span className="text-[#8b949e] truncate" title={workspacePath}>📁 {workspacePath.split('\\').pop()}</span>
              <button onClick={()=>{setBound(false);setFiles([]);setSelectedFile(null);}} className="text-[#8b949e] hover:text-[#f85149]">✕</button>
            </div>
          )}
        </div>
        <div className="flex-1 overflow-y-auto">
          {files.map(f => (
            <button key={f.path} onClick={()=>handleFileClick(f)}
              className={`w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 hover:bg-[#21262d] transition-colors ${selectedFile?.path===f.path ? 'bg-[#1f6feb22] text-[#58a6ff]' : 'text-[#e8eaed]'}`}>
              <span className="w-2 h-2 rounded-full" style={{background:getLanguageColor(f.language)}}/>
              <span className="truncate">{f.name}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Editor area */}
      <div className="flex-1 flex flex-col">
        {selectedFile ? (
          <>
            <div className="flex items-center justify-between px-4 py-2 border-b border-[#30363d] bg-[#161b22]">
              <div className="flex items-center gap-2 text-xs">
                <span className="text-[#8b949e]">{selectedFile.language}</span>
                <span className="text-[#e8eaed]">{selectedFile.path}</span>
              </div>
            </div>
            <div className="flex-1 overflow-auto">
              <pre className="p-4 text-xs font-mono text-[#e8eaed] whitespace-pre-wrap"><code>{fileContent}</code></pre>
            </div>
            {pending && (
              <div className="border-t border-[#30363d] bg-[#161b22] max-h-64 overflow-y-auto">
                <div className="flex items-center justify-between px-3 py-2 border-b border-[#21262d]">
                  <span className="text-xs text-[#8b949e]">
                    Proposed change to {pending.filePath}
                    <span className="text-[#3fb950]"> +{pending.diff?.added ?? 0}</span>
                    <span className="text-[#f85149]"> −{pending.diff?.removed ?? 0}</span>
                  </span>
                  <div className="flex gap-2">
                    <button onClick={handleApply} disabled={loading}
                      className="text-xs px-2 py-1 rounded bg-[#238636] hover:bg-[#2ea043] disabled:opacity-50 text-white">
                      {loading?'Applying…':'Apply'}
                    </button>
                    <button onClick={()=>{setPending(null); setDiffError('');}}
                      className="text-xs px-2 py-1 rounded border border-[#30363d] text-[#8b949e] hover:text-[#e8eaed]">
                      Discard
                    </button>
                  </div>
                </div>
                <pre className="p-3 text-xs font-mono whitespace-pre-wrap">{renderDiff(pending.diff)}</pre>
              </div>
            )}
            {(diffError||applied)&&(
              <p className={`px-3 py-2 text-xs ${diffError?'text-[#f85149]':'text-[#3fb950]'}`}>{diffError||applied}</p>
            )}
            <div className="border-t border-[#30363d] p-3 flex gap-2">
              <input value={instruction} onChange={e=>setInstruction(e.target.value)} placeholder="Ask Addled to edit this file..."
                className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
              <button
                onClick={handleEdit}
                disabled={!instruction.trim()||wsState!=='connected'||loading}
                className="bg-[#3380FF] hover:bg-[#4d94ff] disabled:opacity-50 text-white rounded px-3 py-1.5 text-xs font-medium">
                {loading?'Editing...':'Edit'}
              </button>
            </div>
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-[#8b949e]">
            <div className="text-center">
              <span className="text-4xl mb-3 block">💻</span>
              <p className="text-sm">{bound ? 'Select a file to view' : 'Bind a workspace to start coding'}</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
