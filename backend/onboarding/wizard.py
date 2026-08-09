"""
Onboarding wizard — first-run PyQt6 setup flow.

Walks the user through: welcome → provider → character → voice → wake-word → ready.
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QLabel, QLineEdit,
    QComboBox, QCheckBox, QPushButton, QHBoxLayout, QSlider,
    QRadioButton, QButtonGroup, QGroupBox, QFrame, QApplication,
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

CHARACTER_TEXT = """<h2>Customize Your Companion</h2>
<p>Choose how Addled appears on your screen.</p>"""

VOICE_TEXT = """<h2>Voice Settings</h2>
<p>Configure how Addled speaks to you.</p>"""

WAKE_TEXT = """<h2>Wake Word</h2>
<p>Choose how to get Addled's attention.</p>
<p style='color:#8b949e;font-size:12px;'>Requires microphone access. Can be disabled later.</p>"""

READY_TEXT = """<h2>You're All Set! ✨</h2>
<p>Addled is configured and ready to help.</p>
<p style='color:#8b949e;font-size:12px;'>The companion will appear on your screen once you finish.</p>"""


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
            "DeepSeek (recommended)",
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


# ── Page 3: Character ────────────────────────────────────────────────────────

class CharacterPage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Appearance")
        layout = QVBoxLayout(self)

        label = QLabel(CHARACTER_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        # Shape
        shape_group = QGroupBox("Shape")
        shape_layout = QVBoxLayout(shape_group)
        self.shape_group = QButtonGroup(self)
        shapes = [("Triangle", "triangle"), ("Circle", "circle"), ("Diamond", "diamond"),
                   ("Hexagon", "hexagon"), ("Star", "star"), ("Square", "square")]
        for label_text, shape_id in shapes:
            rb = QRadioButton(label_text)
            self.shape_group.addButton(rb)
            shape_layout.addWidget(rb)
            if shape_id == "triangle":
                rb.setChecked(True)
        layout.addWidget(shape_group)

        # Name
        name_label = QLabel("Companion Name:")
        layout.addWidget(name_label)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Addled")
        self.name_input.setText("Addled")
        layout.addWidget(self.name_input)

        layout.addStretch()

    def selected_shape(self) -> str:
        btn = self.shape_group.checkedButton()
        return btn.text().lower() if btn else "triangle"

    def companion_name(self) -> str:
        return self.name_input.text().strip() or "Addled"


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
        voice_label = QLabel("TTS Voice:")
        layout.addWidget(voice_label)
        self.voice_combo = QComboBox()
        self.voice_combo.addItems([
            "en-US-JennyNeural (Female)",
            "en-US-GuyNeural (Male)",
            "en-GB-SoniaNeural (British Female)",
            "en-GB-RyanNeural (British Male)",
            "en-AU-NatashaNeural (Australian)",
        ])
        layout.addWidget(self.voice_combo)

        # Speed
        speed_label = QLabel("Speech Speed:")
        layout.addWidget(speed_label)
        speed_row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(-50, 50)
        self.speed_slider.setValue(0)
        self.speed_label = QLabel("Normal")
        self.speed_slider.valueChanged.connect(
            lambda v: self.speed_label.setText(f"{'+' if v > 0 else ''}{v}%"))
        speed_row.addWidget(self.speed_slider)
        speed_row.addWidget(self.speed_label)
        layout.addLayout(speed_row)

        # Enable/disable
        self.voice_enabled = QCheckBox("Enable voice responses")
        self.voice_enabled.setChecked(True)
        layout.addWidget(self.voice_enabled)

        layout.addStretch()

    def selected_voice(self) -> str:
        return self.voice_combo.currentText().split(" ")[0]

    def speech_rate(self) -> str:
        return f"{'+' if self.speed_slider.value() > 0 else ''}{self.speed_slider.value()}%"

    def voice_on(self) -> bool:
        return self.voice_enabled.isChecked()


# ── Page 5: Wake Word ────────────────────────────────────────────────────────

class WakePage(StyledPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Wake Word")
        layout = QVBoxLayout(self)

        label = QLabel(WAKE_TEXT)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.wake_combo = QComboBox()
        self.wake_combo.addItems(["Addled", "Hey Addled", "Hey Buddy", "Computer", "Assistant"])
        self.wake_combo.setEditable(True)
        layout.addWidget(self.wake_combo)

        self.wake_note = QLabel(
            "<span style='color:#8b949e;font-size:11px;'>"
            "You can type a custom wake word.</span>"
        )
        self.wake_note.setWordWrap(True)
        layout.addWidget(self.wake_note)

        self.wake_enabled = QCheckBox("Enable wake word detection")
        self.wake_enabled.setChecked(True)
        layout.addWidget(self.wake_enabled)

        layout.addStretch()

    def wake_word(self) -> str:
        return self.wake_combo.currentText().strip()

    def wake_on(self) -> bool:
        return self.wake_enabled.isChecked()


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
        self.autostart.setChecked(False)
        layout.addWidget(self.autostart)
        layout.addStretch()


# ── Main Wizard ──────────────────────────────────────────────────────────────

class OnboardingWizard(QWizard):
    """First-run setup wizard."""

    PROVIDER_MAP = {
        "DeepSeek (recommended)": "deepseek",
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
        self._character = CharacterPage()
        self._voice = VoicePage()
        self._wake = WakePage()
        self._ready = ReadyPage()

        self.addPage(self._welcome)
        self.addPage(self._provider)
        self.addPage(self._character)
        self.addPage(self._voice)
        self.addPage(self._wake)
        self.addPage(self._ready)

    def get_settings(self) -> dict:
        """Collect all user choices as a config dict."""
        return {
            "providers": {
                "active": self.PROVIDER_MAP.get(
                    self._provider.selected_provider(), "deepseek"),
            },
            "character": {
                "name": self._character.companion_name(),
                "shape": self._character.selected_shape(),
            },
            "voice": {
                "enabled": self._voice.voice_on(),
                "voice": self._voice.selected_voice(),
                "rate": self._voice.speech_rate(),
            },
            "wake": {
                "enabled": self._wake.wake_on(),
                "word": self._wake.wake_word(),
            },
            "system": {
                "autostart": self._ready.autostart.isChecked(),
                "onboarded": True,
            },
        }

    def apply_settings(self):
        """Save settings to config and persist."""
        from backend.config import config
        settings = self.get_settings()
        for section, values in settings.items():
            for key, val in values.items():
                config.set(section, key, value=val)

        # Save API key if provided
        api_key = self._provider.api_key()
        if api_key:
            provider_id = self.PROVIDER_MAP.get(
                self._provider.selected_provider(), "deepseek")
            config.set("providers", "builtin", provider_id, "api_key",
                       value=api_key)

        # Handle autostart
        if self._ready.autostart.isChecked():
            _enable_autostart()

        log.info("Onboarding complete. Settings saved.")


def _enable_autostart():
    """Add Addled to Windows startup."""
    try:
        import os
        startup = os.path.join(os.environ.get("APPDATA", ""),
                               r"Microsoft\Windows\Start Menu\Programs\Startup")
        if not os.path.isdir(startup):
            return
        exe = sys.executable
        main_script = Path(__file__).parent.parent / "main.py"
        shortcut = os.path.join(startup, "Addled.bat")
        with open(shortcut, "w") as f:
            f.write(f'@echo off\n"{exe}" "{main_script}"\n')
    except Exception as e:
        log.warning("Failed to set autostart: %s", e)


def show_onboarding():
    """Run the onboarding wizard. Returns True if completed."""
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    wizard = OnboardingWizard()
    # Use bundled icon if available, fall back to default
    icon_path = Path(__file__).parent.parent.parent / "electron" / "icons" / "icon.png"
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
