// Shared media handling for the bot bridges.
//
// Telegram and WhatsApp each receive photos and voice notes in their own way,
// but what the BACKEND needs is the same shape either way: an attachment with
// a name, a kind, and base64 data. Deciding that in one place is the point —
// two bridges deciding independently is how "what counts as audio" drifts, and
// a voice note that one bridge calls `file` reaches the model as an unreadable
// blob instead of a transcript.
//
// The kinds match `_analyze_attachments` in backend/ws_server.py:
//   image -> visual model (provider vision or local Florence-2) -> description
//   text  -> inlined as content
//   audio -> local whisper transcription -> the transcript
//   file  -> name and size only

// Caps, matching the backend's own limits so a bridge refuses what the server
// would refuse anyway — with a message the user can act on, rather than a
// silent drop or a slow failure after a long upload.
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_AUDIO_BYTES = 12 * 1024 * 1024;
const MAX_FILE_BYTES = 10 * 1024 * 1024;

/** Extensions that are really audio, whatever the mime type claims. */
const AUDIO_EXT = /\.(ogg|oga|opus|m4a|mp3|wav|webm|flac|aac|amr)$/i;
/** Extensions we can usefully inline as text. */
const TEXT_EXT = /\.(txt|md|json|csv|log|py|js|ts|tsx|jsx|html|css|xml|yaml|yml|ini|cfg|sh|bat|ps1|toml|sql)$/i;

/**
 * Which kind does this media belong to?
 *
 * Mime type first, because it is what the platform actually told us, then the
 * extension as a fallback — Telegram reports `audio/ogg` for a voice note while
 * WhatsApp hands over a bare `.oga` with no type at all.
 */
function classify(name, mime) {
  const m = String(mime || '').toLowerCase();
  const n = String(name || '');
  if (m.startsWith('image/')) return 'image';
  if (m.startsWith('audio/')) return 'audio';
  if (m.startsWith('video/')) return 'audio';   // a video note transcribes fine
  if (m.startsWith('text/')) return 'text';
  if (AUDIO_EXT.test(n)) return 'audio';
  if (TEXT_EXT.test(n)) return 'text';
  return 'file';
}

function humanSize(bytes) {
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
}

function capFor(kind) {
  if (kind === 'image') return MAX_IMAGE_BYTES;
  if (kind === 'audio') return MAX_AUDIO_BYTES;
  return MAX_FILE_BYTES;
}

/**
 * Turn downloaded media into a `chat.send` attachment.
 *
 * Returns `{attachment}` on success or `{error}` with a sentence naming what is
 * wrong — the limit and the actual size — because "that did not work" gives the
 * user nothing to do next.
 *
 * `buffer` is a Node Buffer (or anything Buffer.from accepts) as fetched from
 * the platform; the base64 conversion happens here so no bridge has to remember
 * to do it.
 */
function toAttachment(buffer, name, mime) {
  if (!buffer || !buffer.length) {
    return { error: 'The file arrived empty — nothing to send.' };
  }
  const safeName = String(name || 'attachment').slice(0, 120);
  const kind = classify(safeName, mime);
  const cap = capFor(kind);
  if (buffer.length > cap) {
    const what = kind === 'image' ? 'Image'
               : kind === 'audio' ? 'Voice note'
               : 'File';
    return { error: `${what} is too large (${humanSize(buffer.length)}, limit `
                    + `${humanSize(cap)}).` };
  }
  return {
    attachment: {
      name: safeName,
      kind,
      data: Buffer.from(buffer).toString('base64'),
    },
    kind,
  };
}

/**
 * A short label for a log line, without dumping the payload.
 *
 * Base64 of a photo is megabytes of text; printing it burying the log is how a
 * bridge looks like it has stopped working when it has not.
 */
function describe(attachment) {
  if (!attachment) return '(none)';
  const bytes = attachment.data ? Math.floor(attachment.data.length * 0.75) : 0;
  return `${attachment.kind} "${attachment.name}" ~${humanSize(bytes)}`;
}

module.exports = { toAttachment, classify, describe, humanSize,
                   MAX_IMAGE_BYTES, MAX_AUDIO_BYTES, MAX_FILE_BYTES };
