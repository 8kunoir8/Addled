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
            <div className="border-t border-[#30363d] p-3 flex gap-2">
              <input value={instruction} onChange={e=>setInstruction(e.target.value)} placeholder="Ask Addled to edit this file..."
                className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-1.5 text-xs text-[#e8eaed] placeholder-[#484f58]"/>
              <button
                onClick={async () => {
                  if (!instruction.trim() || wsState !== 'connected') return;
                  setLoading(true);
                  try {
                    const r = await send('code.edit', { workspaceId: workspacePath, instruction: instruction.trim() });
                    if (r?.diffs?.length) {
                      setFileContent(`// Diffs generated (${r.diffs.length} changes). Full diff viewer coming in Phase 5.\n// Original file: ${selectedFile?.path}\n`);
                    }
                    setInstruction('');
                  } catch (e: any) { setFileContent(`// Edit error: ${e.message}`); }
                  setLoading(false);
                }}
                disabled={!instruction.trim()||wsState!=='connected'}
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
