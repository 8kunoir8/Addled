// Attachment handling, shared by the Chat and Code composers.
//
// This logic existed only inside the Chat page. The Code composer needs the
// same three behaviours — downscale an image so a 4 MB screenshot does not
// become a 5 MB base64 string, read a text file as text, and otherwise send the
// file's metadata — and copying it would have meant two places to fix when one
// of them is wrong. The Chat page keeps its own copy for now; this module is
// what new callers use.

export type AttachmentKind = 'image' | 'text' | 'file';

export type Attachment = {
  name: string;
  kind: AttachmentKind;
  /** base64 (no data: prefix) for images, the text itself for text. */
  data?: string;
  /** An object URL for a thumbnail. Call `releaseAttachment` when done. */
  preview?: string;
  size?: number;
};

/** Extensions treated as text. Kept in step with the Chat page's list. */
export const TEXT_EXTS =
  /\.(txt|md|json|csv|log|py|js|ts|tsx|jsx|html|css|xml|yaml|yml|ini|cfg|sh|bat|ps1|toml|sql|rs|go|java|c|h|cpp|hpp|rb|php|swift|kt|dart|r|lua|vue|svelte|dockerfile|gitignore|env)$/i;

/** Documents the backend can extract text from. */
export const DOC_EXTS = /\.(pdf|docx|xlsx|pptx|doc|xls|ppt|odt|ods|rtf)$/i;

export const MAX_ATTACHMENTS = 5;
/** A text file larger than this is attached by name rather than inline. */
export const MAX_TEXT_BYTES = 1024 * 1024;

/**
 * Downscale an image to a sane size and return base64 without the prefix.
 *
 * A screenshot pasted straight into the composer can be several megapixels,
 * which as base64 is both slow to send and more than the model needs. 1024px on
 * the long side keeps text legible while cutting the payload by an order of
 * magnitude. The canvas path can fail (a rare decode issue), in which case the
 * original bytes are sent rather than losing the attachment.
 */
export async function downscaleImage(file: File, maxSide = 1024): Promise<string> {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => rej(new Error('Could not read image'));
      i.src = url;
    });
    let { width, height } = img;
    const scale = Math.min(1, maxSide / Math.max(width, height));
    width = Math.round(width * scale);
    height = Math.round(height * scale);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d')!.drawImage(img, 0, 0, width, height);
    return canvas.toDataURL('image/jpeg', 0.85).split(',')[1] || '';
  } catch {
    return await new Promise<string>((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res((fr.result as string).split(',')[1] || '');
      fr.onerror = () => rej(new Error('Could not read image'));
      fr.readAsDataURL(file);
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Turn a picked/dropped/pasted File into an attachment. */
export async function fileToAttachment(file: File): Promise<Attachment> {
  if (file.type.startsWith('image/')) {
    const preview = URL.createObjectURL(file);
    const data = await downscaleImage(file);
    return { name: file.name || 'pasted-image.png', kind: 'image', data, preview };
  }
  if (TEXT_EXTS.test(file.name) || file.type.startsWith('text/')) {
    if (file.size <= MAX_TEXT_BYTES) {
      const text = await file.text();
      return { name: file.name, kind: 'text', data: text.slice(0, 100000) };
    }
    // Too large to inline, but still worth naming so the model can read it
    // from the workspace if it is there.
    return { name: file.name, kind: 'file', size: file.size };
  }
  // Documents and anything else. The backend extracts text from known
  // document types; either way the name and size travel.
  return { name: file.name, kind: 'file', size: file.size };
}

/** Read a FileList (or DataTransfer/Clipboard files) into attachments. */
export async function filesToAttachments(
  files: FileList | File[] | null,
  existing: number,
): Promise<Attachment[]> {
  if (!files) return [];
  const room = Math.max(0, MAX_ATTACHMENTS - existing);
  if (!room) return [];
  const list = Array.from(files).slice(0, room);
  const out: Attachment[] = [];
  for (const f of list) {
    try {
      out.push(await fileToAttachment(f));
    } catch {
      // An unreadable file must not lose the rest of the batch.
    }
  }
  return out;
}

/**
 * Release an attachment's thumbnail URL.
 *
 * Object URLs are held by the browser until revoked, so a long session that
 * pasted many screenshots would leak every one of them.
 */
export function releaseAttachment(a?: Attachment | null): void {
  if (a?.preview) {
    try { URL.revokeObjectURL(a.preview); } catch { /* already gone */ }
  }
}

export function releaseAttachments(list: Attachment[]): void {
  list.forEach(releaseAttachment);
}

/**
 * Pull files out of a paste event.
 *
 * Handles two cases that look the same to the user but arrive differently:
 * a copied image is in `items` as a file, while a screenshot taken with the
 * snipping tool can arrive as a `files` entry. Text pastes on their own are
 * left alone — they belong in the textarea, not as an attachment.
 */
export function filesFromPaste(e: ClipboardEvent | React.ClipboardEvent): {
  files: File[];
  hasText: boolean;
} {
  const dt = (e as any).clipboardData as DataTransfer | undefined;
  if (!dt) return { files: [], hasText: false };
  const files: File[] = [];
  if (dt.items && dt.items.length) {
    for (const item of Array.from(dt.items)) {
      if (item.kind === 'file') {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
  }
  if (!files.length && dt.files && dt.files.length) {
    files.push(...Array.from(dt.files));
  }
  return { files, hasText: Boolean(dt.getData && dt.getData('text/plain')) };
}
