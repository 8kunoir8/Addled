"""Bot bridges must forward photos and voice notes, not just text.

Both bridges shipped with the media handlers stubbed:

    telegram: 'Image received. Vision analysis coming in Phase 3.'
    whatsapp: 'Image received. Vision in Phase 3.'

and the same for voice. Nothing failed — a user sending a photo got a polite
sentence back and no analysis, which reads as "Addled cannot see" rather than
"this is not wired up". That is the failure mode this guards: the stub is gone,
the real path exists, and it reaches the backend through the SAME `chat.send`
the text path uses, so there is one attachment shape to keep working.

Also asserts the backend half, because a bridge that sends media the server
ignores is the other way this silently does nothing.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bot_media.py
"""

import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

TELEGRAM = os.path.join(ROOT, "bots", "telegram-bot.js")
WHATSAPP = os.path.join(ROOT, "bots", "whatsapp-bot.js")
MEDIA = os.path.join(ROOT, "bots", "shared", "media.js")
SERVER = os.path.join(ROOT, "backend", "ws_server.py")

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    for path in (TELEGRAM, WHATSAPP, MEDIA, SERVER):
        if not os.path.isfile(path):
            print(f"missing file: {path}")
            return 2
    tg = read(TELEGRAM)
    wa = read(WHATSAPP)
    media = read(MEDIA)
    server = read(SERVER)

    # -- the stubs are gone ---------------------------------------------------
    for name, src in (("telegram", tg), ("whatsapp", wa)):
        check(f"{name}: no 'Phase 3' placeholder remains",
              "Phase 3" not in src,
              "a placeholder reply reads as 'cannot see', not 'not wired up'")
        check(f"{name}: no 'Image received' acknowledgement stub",
              "Image received" not in src)

    # -- both bridges use the shared helper -----------------------------------
    # One place decides what counts as audio. Two bridges deciding separately is
    # how a voice note reaches the model as an unreadable blob.
    for name, src in (("telegram", tg), ("whatsapp", wa)):
        check(f"{name}: imports the shared media helper",
              "shared/media" in src, "each bridge would re-implement the rules")
        check(f"{name}: uses toAttachment", "toAttachment(" in src)

    # -- each bridge registers the media handlers -----------------------------
    check("telegram: registers a photo handler",
          "message:photo" in tg, "a photo would fall through to no handler")
    check("telegram: registers a voice handler",
          "message:voice" in tg)
    check("telegram: also handles an audio file",
          "message:audio" in tg,
          "a sent audio file is not a voice note but carries speech too")
    check("telegram: downloads from the Telegram file API",
          "api.telegram.org/file/bot" in tg and "getFile(" in tg,
          "the bytes have to be fetched; the id alone is not the image")
    check("telegram: picks the LARGEST photo size",
          re.search(r"sizes\[sizes\.length\s*-\s*1\]", tg) is not None,
          "the default middle size blurs text in a screenshot")

    check("whatsapp: handles imageMessage", "imageMessage" in wa)
    check("whatsapp: handles audioMessage", "audioMessage" in wa)
    check("whatsapp: downloads the media",
          "downloadMediaMessage(" in wa,
          "the message body is encrypted; the bytes must be fetched")
    # It must come from the baileys import line itself: a name used without
    # being imported throws at runtime, which a syntax check will not catch.
    baileys_import = re.search(r"^const \{(.*?)\} = require\('@whiskeysockets/baileys'\)",
                               wa, re.M)
    check("whatsapp: imports downloadMediaMessage from baileys",
          baileys_import is not None
          and "downloadMediaMessage" in baileys_import.group(1),
          "an undefined downloader throws at runtime, not at import")

    # -- the media goes through the SAME chat.send ----------------------------
    for name, src in (("telegram", tg), ("whatsapp", wa)):
        check(f"{name}: sends attachments on chat.send",
              re.search(r"attachments:\s*\[", src) is not None,
              "the downloaded bytes must reach the backend")

    # -- the shared helper enforces the rules ---------------------------------
    check("the helper classifies by mime first",
          "mime" in media and "classify(" in media)
    check("the helper recognises ogg/opus/oga as audio",
          "opus" in media and "oga" in media,
          "WhatsApp voice notes arrive with no mime type and a .oga name")
    check("the helper enforces a size cap",
          "toAttachment" in media and "limit" in media)
    check("the helper refuses an empty payload",
          "arrived empty" in media)

    # -- the backend actually reads audio -------------------------------------
    check("the backend handles kind 'audio'",
          'kind == "audio"' in server,
          "a bridge that sends audio the server ignores does nothing")
    check("the backend base64-decodes the note",
          "b64decode" in server)
    check("the backend writes it with a real suffix",
          "_audio_suffix" in server,
          "whisper sniffs the container; .tmp fails to decode")
    check("the backend removes the temp file in a finally",
          re.search(r"finally:\s*\n(?:.*\n){0,6}.*unlink", server) is not None,
          "a failure mid-transcription must not leave audio on disk")
    check("a note that cannot be read says so",
          "could not be transcribed" in server,
          "answering as if no audio was sent is the confusing case")

    # -- and chat.send still accepts the shape the bridges send ---------------
    check("chat.send still reads `attachments`",
          'params.get("attachments")' in server)
    check("the analyser is still the single seam",
          "_analyze_attachments(" in server,
          "a second analyser would be a second set of rules")

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: both bridges forward photos and voice notes through one path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
