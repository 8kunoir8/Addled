"""
Which language to answer in.

"Always reply in the same language the user writes in" was one sentence in the
system prompt, and for the local model it lost. `_call_prompt_tools` appends the
tool catalogue to the user's *last* message, and that catalogue measured ~4,700
tokens — English. So the final thing the model read before generating was a wall
of English tool documentation, and asked in Indonesian to "explain what you can
do with the tools you have", it answered in English. A rule that far from the
question does not survive that.

So the language is now named in the last position instead — after the catalogue,
in the turn being answered (`reply_directive`, appended by `tool_loop`).

`detect()` is a heuristic, not a language detector product. It has to be right
about Indonesian, the languages this app routes voices for, and to say **None**
rather than guess — an unknown answer falls back to wording that states the rule
without naming a language, which is still better than the old system-prompt line
because of where it sits. Non-Latin scripts are read off the characters, which is
exact; Latin-script languages are scored on function words that are distinctive
between them, and need two hits, because one word is a coincidence ("no" is
English, Spanish and Italian).
"""

from __future__ import annotations

import re

# Scripts, which are unambiguous — one character is enough to know.
_SCRIPTS = (
    ("ja", re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")),      # kana
    ("ko", re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")),      # hangul
    ("zh", re.compile(r"[\u4e00-\u9fff]")),                   # han
    ("ar", re.compile(r"[\u0600-\u06ff]")),                   # arabic
    ("hi", re.compile(r"[\u0900-\u097f]")),                   # devanagari
    ("ru", re.compile(r"[\u0400-\u04ff]")),                   # cyrillic
    ("th", re.compile(r"[\u0e00-\u0e7f]")),                   # thai
)

# Function words that separate the Latin-script languages from each other. Only
# tokens that are distinctive between them are listed — "de" is French, Spanish
# and Portuguese, so it proves nothing and is left out.
_WORDS: dict[str, set[str]] = {
    "id": {"apa", "apakabar", "kabar", "bisa", "kamu", "kmu", "saya", "aku",
           "tidak", "nggak", "gak", "gk", "yang", "yg", "dan", "untuk", "dengan",
           "ini", "itu", "kenapa", "gimana", "gmn", "bagaimana", "tolong",
           "bantu", "jelaskan", "sekarang", "sudah", "udah", "belum", "mau",
           "kita", "mereka", "atau", "juga", "ada", "aja", "saja", "kalau",
           "tapi", "karena", "mungkin", "harus", "ingin", "cari", "buat",
           "pakai", "pake", "mana", "siapa", "kapan", "halo", "hai", "banget",
           "bgt", "terima", "kasih", "mohon", "silakan", "sedang", "dari"},
    "es": {"hola", "gracias", "porque", "cuando", "dónde", "donde", "cómo",
           "como", "quiero", "puedes", "hacer", "tengo", "muy", "también",
           "tambien", "esto", "eso", "archivo", "ahora", "está", "esta",
           "nada", "algo", "bien", "pero", "para", "con", "los", "las",
           "una", "del", "qué", "que", "mi", "tu", "sí"},
    "fr": {"bonjour", "merci", "pourquoi", "comment", "fichier", "veux",
           "pouvez", "faire", "très", "tres", "aussi", "maintenant", "où",
           "avec", "dans", "vous", "nous", "mes", "mais", "est", "pas",
           "plus", "qui", "que", "pour", "je", "tu"},
    "de": {"nicht", "und", "für", "fur", "ist", "ich", "du", "wir", "unser",
           "auch", "bitte", "danke", "möchte", "moechte", "können", "koennen",
           "jetzt", "schon", "noch", "sehr", "diese", "welche", "warum",
           "datei", "machen"},
    "pt": {"você", "voce", "vocês", "não", "nao", "obrigado", "obrigada",
           "arquivo", "agora", "isso", "muito", "também", "tambem", "porque",
           "quando", "onde", "quero", "posso", "fazer", "tenho", "está",
           "são", "sao", "olá", "ola", "para", "com", "uma"},
    "it": {"ciao", "grazie", "perché", "perche", "quando", "dove", "voglio",
           "puoi", "fare", "molto", "anche", "adesso", "questo", "questa",
           "sono", "gli", "non", "per", "come", "una", "che"},
    "en": {"the", "you", "your", "what", "how", "why", "where", "when", "can",
           "could", "would", "please", "thanks", "file", "want", "need",
           "help", "with", "this", "that", "and", "not", "have", "does"},
}

# Two hits, because one is a coincidence between related languages.
_MIN_HITS = 2

_NAMES = {
    "id": "Indonesian (Bahasa Indonesia)", "en": "English", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ar": "Arabic",
    "hi": "Hindi", "ru": "Russian", "th": "Thai",
}

_WORD = re.compile(r"[a-zà-öø-ÿ]+")


def detect(text: str) -> str | None:
    """Best guess at the language of `text`, or None when it is not clear."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    for code, pattern in _SCRIPTS:
        if pattern.search(lowered):
            return code
    tokens = set(_WORD.findall(lowered))
    if not tokens:
        return None
    best: str | None = None
    best_score = 0
    for code, words in _WORDS.items():
        score = len(tokens & words)
        if score > best_score:
            best, best_score = code, score
    return best if best_score >= _MIN_HITS else None


def reply_directive(text: str) -> str:
    """The instruction that pins the reply language, or "" for English.

    English needs nothing said: it is what the model falls back to, and the local
    model has only 8192 tokens to spend. Everything else gets a line that names
    the language and spells out the trap, because the text immediately around it
    is a list of English tools.
    """
    code = detect(text)
    if code == "en":
        return ""
    if code and code in _NAMES:
        name = _NAMES[code]
        return (f"[Reply language] The user's message is in {name}. Write your "
                f"reply in {name}. The tool descriptions and any memory notes "
                "in this prompt are written in English — they are not the "
                "conversation, and they are not the language to answer in.")
    return ("[Reply language] Write your reply in the same language as the "
            "user's message — not the language of the tool descriptions or the "
            "memory notes above.")
