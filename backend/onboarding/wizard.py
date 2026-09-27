"""
Onboarding wizard — first-run PyQt6 setup flow.

Walks the user through: welcome → provider → local model → appearance →
workspace → voice → wake-word → ready.

Every page here writes a setting that something actually reads. That is the
rule this file is held to: a step that collects a value nobody consumes is a
question asked for nothing, and it costs the user their first minute with the
app. `scripts/check_wizard.py` fails by name when a page and its config key
drift apart, which is how `character.name` was found being collected and
discarded while the real key, `agent_name`, sat unset.
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QLabel, QLineEdit,
    QComboBox, QCheckBox, QPushButton, QHBoxLayout,
    QRadioButton, QFrame, QApplication,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QFont, QPixmap, QIcon

log = logging.getLogger("addled.onboarding")


WELCOME_TEXT = """<h2>Welcome to Addled!</h2>
<p>Your AI desktop companion is ready to set up.</p>
<p>We'll guide you through a few quick steps to get started.</p>
<p style='color:#8b949e;font-size:12px;'>All settings can be changed later from the dashboard.</p>"""

PROVIDER_TEXT = """<h2>Choose Your AI Provider</h2>
<p>Addled works with local and cloud AI providers.</p>
<p>You can add more providers later in Settings.</p>"""

LOCAL_AI_TEXT = """<h2>Local AI Model</h2>
<p>Addled can run its own AI model on your PC — no account, no API key, and it
keeps working offline.</p>
<p style='color:#8b949e;font-size:12px;'>Nothing is downloaded until you agree.
The model is about 5.03 GB and is fetched after setup finishes.</p>"""

CHARACTER_TEXT = """<h2>Name Your Companion</h2>
<p>What should Addled be called in your chats?</p>"""

WORKSPACE_TEXT = """<h2>Your Workspace</h2>
<p>The folder Addled works in. Files it reads and writes are confined to this
folder, and the Code page opens here.</p>
<p style='color:#8b949e;font-size:12px;'>Leave it empty to decide later. You can
change it any time from Settings, or by right-clicking a folder on the Code
page.</p>"""

VOICE_TEXT = """<h2>Voice</h2>
<p>Configure how Addled speaks to you.</p>

<p style='color:#8b949e;font-size:12px;'>Speaking uses Microsoft Edge's online
voices — no key, but a connection.</p>"""

WAKE_TEXT = """<h2>Wake Word</h2>
<p>Choose how to get Addled's attention.</p>
<p style='color:#8b949e;font-size:12px;'>Listens through your microphone and can
be turned off at any time.</p>"""

READY_TEXT = """<h2>You're All Set</h2>
<p>Addled is configured and ready to help.</p>
<p style='color:#8b949e;font-size:12px;'>The companion appears on your screen
once you finish.</p>"""


class StyledPage(QWizardPage):
    """Base page with consistent dark styling."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QWizardPage {
                background-color: #0d1117;
                color: #e8eaed;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel { color: #e8eaed; font-size: 13px; }
            QLineEdit {
                background-color: #161b22;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 8px 12px;
                color: #e8eaed;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #3380FF; }
            QComboBox {
                background-color: #161b22;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 8px 12px;
                color: #e8eaed;
                font-size: 13px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #161b22;
                color: #e8eaed;
                selection-background-color: #1f6feb;
                border: 1px solid #30363d;
            }
            QCheckBox { color: #e8eaed; font-size: 13px; }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border: 1px solid #30363d;
                border-radius: 3px;
                background-color: #161b22;
            }
            QCheckBox::indicator:checked { background-color: #3380FF; }
            QRadioButton { color: #e8eaed; font-size: 13px; }
            QGroupBox {
                color: #e8eaed;
                border: 1px solid #30363d;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 16px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #30363d;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 16px; height: 16px;
                margin: -5px 0;
                background: #3380FF;
                border-radius: 8px;
            }
        """)


# ── Page 1: Welcome ──────────────────────────────────────────────────────────

class WelcomePage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome")
        layout = QVBoxLayout(self)
        label = QLabel(WELCOME_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch()


# ── Page 2: Provider Selection ───────────────────────────────────────────────

class ProviderPage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("AI Provider")
        layout = QVBoxLayout(self)

        label = QLabel(PROVIDER_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems([
            "Addled Local — no API key needed (recommended)",
            "OpenRouter (free models)",
            "DeepSeek",
            "OpenAI (GPT-4o)",
            "Anthropic Claude",
            "Google Gemini",
            "GitHub Copilot",
            "Ollama (local)",
            "LM Studio (local)",
        ])
        layout.addWidget(self.provider_combo)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("API Key (leave empty for local/Ollama)...")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.key_input)

        self.key_note = QLabel(
            "<span style='color:#8b949e;font-size:11px;'>"
            "Your key is stored locally and never sent to our servers.</span>"
        )
        self.key_note.setWordWrap(True)
        layout.addWidget(self.key_note)
        layout.addStretch()

    def selected_provider(self) -> str:
        return self.provider_combo.currentText()

    def api_key(self) -> str:
        return self.key_input.text().strip()


# ── Page 2b: Local AI model (ask before downloading) ─────────────────────────

class LocalAIPage(StyledPage):
    """Consent step for the local model download."""

    def __init__(self):
        super().__init__()
        self.setTitle("Local AI Model")
        layout = QVBoxLayout(self)

        label = QLabel(LOCAL_AI_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.download_radio = QRadioButton("Download now (recommended)")
        self.skip_radio = QRadioButton(
            "Not now — I'll use a cloud provider instead")
        self.download_radio.setChecked(True)
        layout.addWidget(self.download_radio)
        layout.addWidget(self.skip_radio)

        from backend.local_models import paths
        try:
            from backend.config import config as app_config
            size_mb = int(app_config.get("local_llm", "size_mb", default=2400) or 2400)
        except Exception:
            size_mb = 2400

        note = QLabel(
            "<span style='color:#8b949e;font-size:11px;'>"
            f"About {size_mb} MB (runtime + weights) · "
            f"{paths.disk_free_mb()} MB free on this drive · "
            f"saved to {paths.LLAMAFILE_DIR}</span>"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

    def download_approved(self) -> bool:
        return self.download_radio.isChecked()


# ── Page 3: Name ─────────────────────────────────────────────────────────────

class CharacterPage(StyledPage):
    """The companion's name, and nothing else about how it looks.

    Shape and Colour were asked here and are no longer offered anywhere: the
    avatar's appearance is fixed, so a question about it is a question whose
    answer changes nothing on screen. The name is different — it is written to
    the top-level `agent_name`, which the character, the dashboard and every bot
    all read, so it does change what the user sees on the next message.
    """

    def __init__(self):
        super().__init__()
        self.setTitle("Name")
        layout = QVBoxLayout(self)

        label = QLabel(CHARACTER_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        # Written to the top-level `agent_name`, never `character.name` — that
        # key is read by nothing, and writing it is how the name a user typed
        # used to be discarded.
        name_label = QLabel("Companion name:")
        layout.addWidget(name_label)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Addled")
        self.name_input.setText("Addled")
        self.name_input.setMaxLength(32)
        layout.addWidget(self.name_input)

        layout.addStretch()

    def companion_name(self) -> str:
        return self.name_input.text().strip() or "Addled"

# ── Page 3b: Workspace ───────────────────────────────────────────────────────

class WorkspacePage(StyledPage):
    """The folder Addled works in.

    Worth a step rather than a default: it is the boundary every file tool is
    confined to, the folder the Code page opens, and the one thing a coding
    agent cannot guess correctly. Skipping is a first-class choice — an empty
    root means "decide later", which the tools handle.
    """

    def __init__(self):
        super().__init__()
        self.setTitle("Workspace")
        layout = QVBoxLayout(self)

        label = QLabel(WORKSPACE_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("No folder chosen")
        row.addWidget(self.path_input)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        self.found_note = QLabel("")
        self.found_note.setWordWrap(True)
        layout.addWidget(self.found_note)

        layout.addStretch()
        self._suggest()

    def _suggest(self):
        """Offer a sensible folder rather than an empty box.

        Documents/Addled if it exists, else the home folder — a default the
        user can accept in one click beats a blank field they have to think
        about.
        """
        from pathlib import Path as _Path
        home = _Path.home()
        candidate = home / "Documents" / "Addled"
        try:
            if candidate.is_dir():
                self.path_input.setText(str(candidate))
                self.found_note.setText(
                    "<span style='color:#8b949e;font-size:11px;'>"
                    "This folder already exists.</span>")
                return
        except OSError:
            pass
        if home.is_dir():
            self.path_input.setText(str(home))

    def _browse(self):
        from PyQt6.QtWidgets import QFileDialog
        start = self.path_input.text().strip() or ""
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the folder Addled works in", start)
        if chosen:
            self.path_input.setText(chosen)
            self.found_note.setText("")

    def workspace_root(self) -> str:
        return self.path_input.text().strip()

    def validatePage(self) -> bool:  # noqa: N802  (Qt name)
        """Refuse a path that is not a real folder, rather than saving it.

        A typo saved here becomes a workspace nothing can open, and the failure
        would surface later as "the Code page is broken" rather than as this.
        """
        from pathlib import Path as _Path
        raw = self.path_input.text().strip()
        if not raw:
            self.found_note.setText("")
            return True
        if _Path(raw).is_dir():
            self.found_note.setText("")
            return True
        self.found_note.setText(
            "<span style='color:#f85149;font-size:11px;'>"
            "That folder does not exist. Pick one, or clear the box to decide "
            "later.</span>")
        return False


# ── Page 4: Voice ────────────────────────────────────────────────────────────

class VoicePage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Voice")
        layout = QVBoxLayout(self)

        label = QLabel(VOICE_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        # Voice selector
        voice_label = QLabel("Voice:")
        layout.addWidget(voice_label)
        self.voice_combo = QComboBox()
        self.voice_combo.addItem("Jenny — US female", "en-US-JennyNeural")
        self.voice_combo.addItem("Guy — US male", "en-US-GuyNeural")
        self.voice_combo.addItem("Sonia — British female", "en-GB-SoniaNeural")
        self.voice_combo.addItem("Ryan — British male", "en-GB-RyanNeural")
        self.voice_combo.addItem("Natasha — Australian",
                                 "en-AU-NatashaNeural")
        layout.addWidget(self.voice_combo)

        self.voice_enabled = QCheckBox("Speak replies out loud")
        self.voice_enabled.setChecked(True)
        layout.addWidget(self.voice_enabled)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Test this voice")
        self.test_btn.clicked.connect(self._test_voice)
        test_row.addWidget(self.test_btn)
        self.test_note = QLabel("")
        self.test_note.setWordWrap(True)
        test_row.addWidget(self.test_note)
        test_row.addStretch()
        layout.addLayout(test_row)

        layout.addStretch()

    def selected_voice(self) -> str:
        # The id is the item's data, not its label: the label is for a person
        # and the id is what the voice system reads.
        return self.voice_combo.currentData() or "en-US-JennyNeural"

    def voice_on(self) -> bool:
        return self.voice_enabled.isChecked()

    def _test_voice(self):
        """Say one line through the chosen voice, off the GUI thread.

        Best-effort and reported honestly: if the online voices are
        unreachable, the user finds out here rather than by wondering why
        Addled never speaks.
        """
        import threading
        voice = self.selected_voice()
        self.test_btn.setEnabled(False)
        self.test_note.setText(
            "<span style='color:#8b949e;font-size:11px;'>Speaking…</span>")

        def _speak():
            error = ""
            try:
                import asyncio as _aio
                from backend.voice.tts import speak
                # `speak` is async and this runs on a plain worker thread, so
                # it needs its own loop — the Qt loop is already running on the
                # main thread and cannot be entered from here.
                _aio.run(speak("This is how I will sound.", voice=voice))
            except Exception as e:  # noqa: BLE001
                error = str(e)
            try:
                from PyQt6.QtCore import QMetaObject, Qt as _Qt, Q_ARG
                QMetaObject.invokeMethod(
                    self, "_apply_test_result",
                    _Qt.ConnectionType.QueuedConnection, Q_ARG(str, error))
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_speak, daemon=True).start()

    def _apply_test_result(self, error: str):
        """Back on the GUI thread — Qt widgets may only be touched from it."""
        self.test_btn.setEnabled(True)
        if error:
            self.test_note.setText(
                "<span style='color:#f85149;font-size:11px;'>"
                f"Could not play: {error[:80]}</span>")
        else:
            self.test_note.setText(
                "<span style='color:#3fb950;font-size:11px;'>"
                "Heard it? That is the voice you will get.</span>")


# ── Page 5: Wake Word ────────────────────────────────────────────────────────

class WakePage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Wake Word")
        layout = QVBoxLayout(self)

        label = QLabel(WAKE_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.wake_enabled = QCheckBox("Listen for a wake word")
        self.wake_enabled.setChecked(True)
        layout.addWidget(self.wake_enabled)

        word_label = QLabel("Wake word:")
        layout.addWidget(word_label)
        self.wake_combo = QComboBox()
        self.wake_combo.addItems(["Hey Addled", "Addled", "Hey Buddy",
                                  "Computer", "Assistant"])
        self.wake_combo.setEditable(True)
        layout.addWidget(self.wake_combo)

        self.wake_note = QLabel("")
        self.wake_note.setWordWrap(True)
        layout.addWidget(self.wake_note)

        self.mic_ok = _microphone_available()
        if not self.mic_ok:
            # Turning listening on with no microphone is a promise the app
            # cannot keep, and the user would only find out from silence.
            self.wake_enabled.setChecked(False)
            self.wake_enabled.setEnabled(False)
            self.wake_note.setText(
                "<span style='color:#d29922;font-size:11px;'>"
                "No microphone was found, so this is off for now. Addled will "
                "ask again from Settings once you plug one in.</span>")
        else:
            self.wake_note.setText(
                "<span style='color:#8b949e;font-size:11px;'>"
                "You can type any phrase. Turning this on means Addled listens "
                "for it whenever it is running.</span>")

        layout.addStretch()

    def wake_word(self) -> str:
        return self.wake_combo.currentText().strip() or "Hey Addled"

    def wake_on(self) -> bool:
        return self.wake_enabled.isChecked() and self.mic_ok


# ── Page 6: Ready ────────────────────────────────────────────────────────────

class ReadyPage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Ready!")
        layout = QVBoxLayout(self)

        label = QLabel(READY_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.autostart = QCheckBox("Launch Addled when Windows starts")
        # Reflect reality if a shortcut already exists (a re-run of setup, or an
        # earlier install), rather than showing unchecked and then overwriting
        # a choice the user already made.
        try:
            self.autostart.setChecked(autostart_enabled())
        except Exception:  # noqa: BLE001
            self.autostart.setChecked(False)
        layout.addWidget(self.autostart)
        layout.addStretch()


# ── Main Wizard ──────────────────────────────────────────────────────────────

class OnboardingWizard(QWizard):
    """First-run setup wizard."""

    PROVIDER_MAP = {
        "Addled Local — no API key needed (recommended)": "local",
        "OpenRouter (free models)": "openrouter",
        "DeepSeek": "deepseek",
        "OpenAI (GPT-4o)": "openai",
        "Anthropic Claude": "claude",
        "Google Gemini": "gemini",
        "GitHub Copilot": "copilot",
        "Ollama (local)": "ollama",
        "LM Studio (local)": "lmstudio",
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Addled — Setup")
        self.setMinimumSize(520, 480)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setStyleSheet("""
            QWizard {
                background-color: #0d1117;
                color: #e8eaed;
                font-family: 'Segoe UI', sans-serif;
            }
            QWizard QPushButton {
                background-color: #21262d;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 8px 20px;
                color: #e8eaed;
                font-size: 13px;
            }
            QWizard QPushButton:hover { background-color: #30363d; }
            QWizard QPushButton[text="Finish"],
            QWizard QPushButton[text="&Finish"] {
                background-color: #3380FF;
                border-color: #3380FF;
                color: white;
            }
            QWizard QPushButton[text="Finish"]:hover,
            QWizard QPushButton[text="&Finish"]:hover {
                background-color: #4d94ff;
            }
            QWizard QPushButton:disabled { opacity: 0.5; }
        """)

        self._welcome = WelcomePage()
        self._provider = ProviderPage()
        self._local = LocalAIPage()
        self._character = CharacterPage()
        self._workspace = WorkspacePage()
        self._voice = VoicePage()
        self._wake = WakePage()
        self._ready = ReadyPage()

        self.addPage(self._welcome)
        self.addPage(self._provider)
        self.addPage(self._local)
        self.addPage(self._character)
        self.addPage(self._workspace)
        self.addPage(self._voice)
        self.addPage(self._wake)
        self.addPage(self._ready)

    def get_settings(self) -> dict:
        """Collect all user choices as a config dict.

        Every key here is one something reads. The map from a page to a key is
        the contract `scripts/check_wizard.py` holds this file to, because a
        step that saves nothing is indistinguishable from a working one until a
        user notices their choice never took.
        """
        approved = self._local.download_approved()
        return {
            # Top-level, not under `character`: this is what the character, the
            # dashboard and the bots read. The page used to write
            # `character.name`, which nothing consumed, so the name the user
            # typed was discarded.
            "agent_name": self._character.companion_name(),
            "providers": {
                "active": self.PROVIDER_MAP.get(
                    self._provider.selected_provider(), "local"),
            },
            "local_llm": {
                "asked": True,
                "download_approved": approved,
                "declined": not approved,
            },
            # Shape and colour are NOT written: nothing on screen changes with
            # them, so asking would be a question with no visible answer. The
            # avatar keeps whatever settings.json holds (`triangle`, the Addled
            # blue), and both stay editable there by anyone who wants to.
            "workspace": {
                # An empty root means "decide later"; every file tool already
                # treats that as "no workspace yet" rather than an error.
                "root": self._workspace.workspace_root(),
            },
            "voice": {
                "tts_voice": self._voice.selected_voice(),
                "auto_tts": self._voice.voice_on(),
                "wake_word": self._wake.wake_word(),
                "mic_enabled": self._wake.wake_on(),
            },
            "system": {
                "autostart": self._ready.autostart.isChecked(),
                "onboarded": True,
            },
            "first_run_complete": True,
        }

    def apply_settings(self):
        """Save settings to config and persist."""
        from backend.config import config
        settings = self.get_settings()
        for section, values in settings.items():
            # A top-level scalar (`agent_name`, `first_run_complete`) is not a
            # section. Treating it as one wrote `agent_name: {a: d, d: d, ...}`
            # — a dict where a string belonged — which is how the companion
            # name could be set and never seen again.
            if not isinstance(values, dict):
                config.set(section, value=values)
                continue
            for key, val in values.items():
                config.set(section, key, value=val)

        # Save API key if provided
        api_key = self._provider.api_key()
        if api_key:
            provider_id = self.PROVIDER_MAP.get(
                self._provider.selected_provider(), "deepseek")
            config.set("providers", "builtin", provider_id, "api_key",
                       value=api_key)

        # A workspace folder the user typed but that does not exist would save
        # as a dead path, and every file tool would then refuse to work in it
        # while reporting "folder not found". Create it here, where the intent
        # is clear, rather than leaving the user to work it out later.
        self._ensure_workspace()

        # Handle autostart. `set_autostart` applies it AND records the choice,
        # so the Startup shortcut and the setting cannot disagree.
        try:
            set_autostart(self._ready.autostart.isChecked())
        except Exception as e:  # noqa: BLE001
            log.warning("could not set autostart: %s", e)

        log.info("Onboarding complete. Settings saved.")

    def _ensure_workspace(self) -> None:
        from backend.config import config
        from pathlib import Path as _Path
        root = str(config.get("workspace", "root", default="") or "").strip()
        if not root:
            return
        try:
            _Path(root).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("could not create the workspace %s: %s", root, e)

def _microphone_available() -> bool:
    """Whether a recording device exists.

    Checked so the wake-word step can decline honestly rather than promise to
    listen on a machine with no microphone. Best-effort: a probe that fails is
    reported as "available", because refusing the feature over a probing error
    would be the worse mistake.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        return any(int(d.get("max_input_channels") or 0) > 0 for d in devices)
    except Exception as e:  # noqa: BLE001
        log.debug("microphone probe failed, assuming present: %s", e)
        return True


def _autostart_shortcut() -> str:
    import os
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
    return os.path.join(startup, "Addled.bat")

def _apply_autostart(enabled: bool) -> bool:
    """Add or remove Addled's entry in the Windows Startup folder.

    Reversible on purpose: the wizard could previously only ADD the shortcut,
    so turning autostart off left a file behind that kept launching the app.
    Returns whether the change was made; a machine without a Startup folder
    (not Windows) reports False rather than raising.
    """
    try:
        import os
        shortcut = _autostart_shortcut()
        if not enabled:
            if os.path.isfile(shortcut):
                os.remove(shortcut)
            return True
        startup = os.path.dirname(shortcut)
        if not os.path.isdir(startup):
            return False
        exe = sys.executable
        main_script = Path(__file__).parent.parent / "main.py"
        with open(shortcut, "w") as f:
            f.write(f'@echo off\n"{exe}" "{main_script}"\n')
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not apply autostart=%s: %s", enabled, e)
        return False

def autostart_enabled() -> bool:
    """Whether Addled currently starts with Windows."""
    try:
        import os
        return os.path.isfile(_autostart_shortcut())
    except Exception:  # noqa: BLE001
        return False

def set_autostart(enabled: bool) -> dict:
    """Set autostart and record it. Used by Settings and the wizard alike.

    One entry point so the file on disk and the stored setting cannot disagree —
    the state used to be applied but never recorded, which is what made it
    impossible to show or undo.
    """
    applied = _apply_autostart(bool(enabled))
    try:
        from backend.config import config
        config.set("system", "autostart", value=bool(enabled))
    except Exception as e:  # noqa: BLE001
        log.debug("could not record the autostart setting: %s", e)
    return {"success": applied, "autostart": bool(enabled),
            "error": "" if applied else "No Startup folder on this system."}


def show_onboarding():
    """Run the onboarding wizard. Returns True if completed."""
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    wizard = OnboardingWizard()
    # Two layouts to try: the repo keeps icons in electron/icons, while a
    # packaged build copies them to <resources>/icons. Only the first was
    # checked, so a packaged wizard silently fell back to no icon at all.
    root = Path(__file__).parent.parent.parent
    icon_path = root / "electron" / "icons" / "icon.png"
    if not icon_path.exists():
        icon_path = root / "icons" / "icon.png"
    wizard.setWindowIcon(QIcon(str(icon_path)) if icon_path.exists() else QIcon())

    if wizard.exec() == QWizard.DialogCode.Accepted:
        wizard.apply_settings()
        return True
    return False


def is_first_run() -> bool:
    """Check if this is the first time Addled is launched."""
    from backend.config import config
    return not config.get("system", "onboarded", default=False)


# ── CLI entry point for testing ──────────────────────────────────────────────

if __name__ == "__main__":
    show_onboarding()
