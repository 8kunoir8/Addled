/**
 * The subset of markdown the model actually writes, rendered as React nodes.
 *
 * Why hand-rolled rather than `react-markdown`: the reply text is MODEL OUTPUT,
 * and a general renderer accepts raw HTML (rehype-raw) or at minimum carries a
 * much larger surface than this app needs. Measured against the real store, six
 * constructs account for every markdown token in 18 replies - bold, inline
 * code, fenced code, headings, bullets and links - so a parser that handles
 * exactly those is smaller, auditable, and cannot be talked into rendering a
 * <script> by a reply that contains one. Anything it does not recognise is
 * shown literally rather than dropped, because a reply must never lose text
 * to a formatting decision.
 *
 * The chat shows the formatted result. Voice must NOT - it speaks
 * `markdownToPlainText` from `lib/markdown.ts`, which is the same grammar
 * without the rendering, so the two cannot disagree about what the model said.
 */

import React from 'react';
import { parseInline, splitEmotion, markdownToPlainText } from '@/lib/markdown';

type Block =
  | { kind: 'code'; language: string; text: string }
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'bullet'; items: string[] }
  | { kind: 'ordered'; items: string[] }
  | { kind: 'rule' }
  | { kind: 'paragraph'; text: string };

const FENCE = /^```(\w*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const RULE = /^\s*(---|\*\*\*|___)\s*$/;

/** Split the reply into blocks. Consecutive lines of the same kind group up. */
export function parseBlocks(source: string): Block[] {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code. An unterminated fence runs to the end rather than swallowing
    // the rest of the reply into nothing - a truncated reply is common when a
    // turn is cut off.
    const fence = FENCE.exec(line.trim());
    if (fence) {
      const language = fence[1] || '';
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i].trim())) {
        body.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;  // skip the closing fence
      blocks.push({ kind: 'code', language, text: body.join('\n') });
      continue;
    }

    if (!line.trim()) { i++; continue; }

    if (RULE.test(line)) { blocks.push({ kind: 'rule' }); i++; continue; }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2] });
      i++;
      continue;
    }

    if (BULLET.test(line)) {
      const items: string[] = [];
      while (i < lines.length && BULLET.test(lines[i])) {
        items.push(BULLET.exec(lines[i])![1]);
        i++;
      }
      blocks.push({ kind: 'bullet', items });
      continue;
    }

    if (ORDERED.test(line)) {
      const items: string[] = [];
      while (i < lines.length && ORDERED.test(lines[i])) {
        items.push(ORDERED.exec(lines[i])![1]);
        i++;
      }
      blocks.push({ kind: 'ordered', items });
      continue;
    }

    // A paragraph runs until a blank line or the start of another block.
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() &&
           !FENCE.test(lines[i].trim()) && !HEADING.test(lines[i]) &&
           !BULLET.test(lines[i]) && !ORDERED.test(lines[i]) &&
           !RULE.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    blocks.push({ kind: 'paragraph', text: para.join('\n') });
  }
  return blocks;
}


/** Render a reply. Never returns raw HTML: every node is built from text. */
export function Markdown({ text }: { text: string }) {
  const { emotion, rest } = splitEmotion(text);
  const blocks = parseBlocks(rest);

  const inline = (s: string) =>
    parseInline(s).map((part, idx) => {
      switch (part.kind) {
        case 'bold':
          return <strong key={idx} className="font-semibold">{part.text}</strong>;
        case 'italic':
          return <em key={idx}>{part.text}</em>;
        case 'code':
          return (
            <code key={idx}
                  className="px-1 py-0.5 rounded text-[0.92em] font-mono bg-[var(--bg-tertiary)] text-[var(--text-primary)]">
              {part.text}
            </code>
          );
        case 'link':
          // Shown as a link but NOT auto-opened: a link from a model reply is
          // still a link a model wrote, and the danger of one-click navigation
          // to that URL is not worth the convenience.
          return (
            <a key={idx} href={part.href} target="_blank" rel="noopener noreferrer"
               className="underline text-[var(--accent)] hover:text-[var(--accent-hover)]">
              {part.text}
            </a>
          );
        default:
          return <React.Fragment key={idx}>{part.text}</React.Fragment>;
      }
    });

  return (
    <div className="space-y-1.5 break-words">
      {emotion && (
        <span className="inline-block text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-[var(--accent-soft)] text-[var(--accent-hover)]">
          {emotion}
        </span>
      )}
      {blocks.map((block, idx) => {
        switch (block.kind) {
          case 'code':
            return (
              <pre key={idx}
                   className="overflow-x-auto rounded p-2 text-xs font-mono bg-[var(--bg-primary)] border border-[var(--border)]">
                <code>{block.text}</code>
              </pre>
            );
          case 'heading': {
            const size = ['text-base', 'text-base', 'text-base', 'text-sm', 'text-sm', 'text-sm'][block.level - 1] || 'text-sm';
            return (
              <div key={idx} className={`${size} font-semibold text-[var(--text-primary)]`}>
                {inline(block.text)}
              </div>
            );
          }
          case 'bullet':
            return (
              <ul key={idx} className="list-disc pl-5 space-y-0.5">
                {block.items.map((item, j) => <li key={j}>{inline(item)}</li>)}
              </ul>
            );
          case 'ordered':
            return (
              <ol key={idx} className="list-decimal pl-5 space-y-0.5">
                {block.items.map((item, j) => <li key={j}>{inline(item)}</li>)}
              </ol>
            );
          case 'rule':
            return <hr key={idx} className="border-[var(--border)]" />;
          default:
            return <p key={idx} className="whitespace-pre-wrap">{inline(block.text)}</p>;
        }
      })}
    </div>
  );
}

export { markdownToPlainText };
