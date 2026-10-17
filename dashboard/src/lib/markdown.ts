/**
 * The inline grammar, shared by the renderer and the voice path.
 *
 * Written once so the chat and the voice cannot disagree about what the model
 * said. If the page rendered bold and the voice read the asterisks, the app
 * would be telling the user two different things - and this file exists
 * because that was exactly the bug: the reply was markdown, the chat printed
 * it raw, and TTS spoke it raw, so both surfaces showed the markup instead of
 * the meaning.
 */

/**
 * Remove inline markup, keeping the words.
 *
 * The same algorithm as `_strip_inline` in `backend/markdown_speech.py`, and
 * deliberately so: code spans are held aside first, because their contents are
 * literal - a `*` in them is an operator, not emphasis - and put back after,
 * so `Length` and `35%` still read inside the sentence. Writing it as a
 * transform on raw strings rather than by walking `parseInline` is what makes
 * `**bold with `code` inside**` come out clean instead of leaving backticks.
 */
function stripInline(text: string): string {
  const held: string[] = [];
  let protectedText = text.replace(/`([^`]+)`/g, (_m, inner: string) => {
    held.push(inner);
    return `\u0000${held.length - 1}\u0000`;
  });
  protectedText = protectedText
    .replace(/\[([^\]]+)\]\([^)\s]+\)/g, '$1')   // link text, not the URL
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1$2');
  return protectedText.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => held[Number(i)]);
}

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'bold'; text: string }
  | { kind: 'italic'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'link'; text: string; href: string };

/**
 * Split one line into inline runs.
 *
 * Order matters: code first, because `` `**not bold**` `` is code containing
 * asterisks and must not be parsed as bold. Bold before italic, because `**`
 * is longer than `*` and a lazy italic rule would match the first asterisk of
 * a bold pair and leave a stray one behind.
 *
 * Anything with no matching closer is emitted as literal text. A model that
 * writes a single `**` should produce a visible `**`, not silently swallow the
 * rest of the line.
 */
export function parseInline(line: string): Inline[] {
  const out: Inline[] = [];
  let i = 0;
  let buffer = '';
  const flush = () => {
    if (buffer) { out.push({ kind: 'text', text: buffer }); buffer = ''; }
  };

  while (i < line.length) {
    const rest = line.slice(i);

    // `code`
    if (line[i] === '`') {
      const close = line.indexOf('`', i + 1);
      if (close > i + 1) {
        flush();
        out.push({ kind: 'code', text: line.slice(i + 1, close) });
        i = close + 1;
        continue;
      }
    }

    // [text](href)
    if (line[i] === '[') {
      const m = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest);
      if (m) {
        flush();
        out.push({ kind: 'link', text: m[1], href: m[2] });
        i += m[0].length;
        continue;
      }
    }

    // **bold** - the inner text is parsed on its own, so `**a `b` c**` keeps
    // its code span instead of handing back the backticks as literal text.
    // Without the recursion the bold run swallowed the backticks whole and the
    // spoken text contained them, which the parity check caught.
    if (rest.startsWith('**')) {
      const close = line.indexOf('**', i + 2);
      if (close > i + 2) {
        flush();
        out.push({ kind: 'bold', text: line.slice(i + 2, close) });
        i = close + 2;
        continue;
      }
    }

    // __bold__ (the other common spelling)
    if (rest.startsWith('__')) {
      const close = line.indexOf('__', i + 2);
      if (close > i + 2) {
        flush();
        out.push({ kind: 'bold', text: line.slice(i + 2, close) });
        i = close + 2;
        continue;
      }
    }

    // *italic* - only when it is not the first half of a `**`
    if (line[i] === '*' && line[i + 1] !== '*') {
      const close = line.indexOf('*', i + 1);
      if (close > i + 1 && line[close + 1] !== '*') {
        flush();
        out.push({ kind: 'italic', text: line.slice(i + 1, close) });
        i = close + 1;
        continue;
      }
    }

    buffer += line[i];
    i++;
  }
  flush();
  return out;
}

/**
 * The tone tag the character opens a reply with - `[happy]`.
 *
 * The app's own protocol, not markdown, and it belongs to neither surface
 * alone: the page renders it as a small label, and a voice must not say
 * "open bracket happy close bracket". It lived only in the renderer at first,
 * and the parity check caught the voice path leaving it in the spoken text.
 */
export const EMOTION = /^\s*\[(neutral|happy|concern|confusion|thoughtful|content|excited|sad|angry|curious|calm|playful)\]\s*/i;

export function splitEmotion(source: string): { emotion: string | null; rest: string } {
  const text = String(source ?? '');
  const m = EMOTION.exec(text);
  if (!m) return { emotion: null, rest: text };
  return { emotion: m[1].toLowerCase(), rest: text.slice(m[0].length) };
}

/**
 * Plain text for a voice engine, from the same grammar the page renders.
 *
 * Code FENCES are dropped entirely rather than read aloud: a shell command
 * read out character by character is noise, and the user can see it on screen.
 * Inline code is kept as words, because `Length` or `35%` in the middle of a
 * sentence is part of the sentence.
 */
export function markdownToPlainText(source: string): string {
  const lines = splitEmotion(source).rest.replace(/\r\n?/g, '\n').split('\n');
  const kept: string[] = [];
  let inFence = false;

  for (const line of lines) {
    if (/^\s*```/.test(line)) { inFence = !inFence; continue; }
    if (inFence) continue;

    // Headings and list markers: the words matter, the marker does not.
    let text = line
      .replace(/^\s*#{1,6}\s+/, '')
      .replace(/^\s*[-*+]\s+/, '')
      .replace(/^\s*\d+[.)]\s+/, '')
      .replace(/^\s*(---|\*\*\*|___)\s*$/, '');

    text = stripInline(text).replace(/\s+/g, ' ').trim();

    if (text) kept.push(text);
  }
  // Each line is a fragment; a stop between them gives the engine a breath.
  //
  // Only join with a stop when the line does not already end in punctuation,
  // and only tidy up the ". ." that joining can create - never a "..." the
  // author wrote. Collapsing every run of stops turned `ReadAllBytes(...)` into
  // `ReadAllBytes(..)` and `sebentar...` into `sebentar.`, which is editing the
  // reply rather than formatting it.
  const joined = kept
    .map((line) => (/[.!?…,;:—-]$/.test(line) ? line : `${line}.`))
    .join(' ');
  return joined.replace(/\.\s+\./g, '.');
}
