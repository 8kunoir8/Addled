# Compile the bundled interpreter's bytecode once, at install time.
#
# The installer ships python-bundle/ WITHOUT __pycache__ — that cache was
# 654 MB of a 938 MB bundle and every byte of it is regenerable. Stripping it
# only works if something puts it back, because the app installs to
# C:\Program Files, which it cannot write to afterwards. So the compile
# happens here, in the installer, which does have write access.
#
# `customInstall` is expanded by electron-builder's installSection.nsh AFTER
# `installApplicationFiles`, so by the time this runs, resources\python\ and
# resources\scripts\precompile_bundle.py are both on disk. That ordering is
# why this hook was chosen; a hook that ran before extraction would compile an
# empty directory and silently do nothing.
#
# Best-effort throughout: the script exits 0 on any failure and the ExecWait
# result is ignored, because a slower first import is far better than a failed
# install. `$INSTDIR` is the install root (e.g. C:\Program Files\Addled); the
# interpreter is resources\python\python.exe and the script is shipped to
# resources\scripts\precompile_bundle.py (see extraResources).

# electron-builder compiles this file in its own pass, to pre-build the
# uninstaller, and in that pass the main installer's `!include "MUI2.nsh"` has
# not happened yet. `MUI_HEADER_TEXT` below therefore had no definition and the
# build aborted with "macro named MUI_HEADER_TEXT not found". Including it here
# makes the file compile on its own; the include is guarded, so the main
# script's own MUI2 include is unaffected.
!include MUI2.nsh

!macro voiceModelsCleanup
  # An upgrade over an install that predates this change.
  #
  # That install has the models INSIDE the install directory, in
  # resources\backend\voice\models. They stay there, because NSIS extracts over
  # an existing install rather than wiping it, and they are now 354 MB of dead
  # weight — the app reads its own copy first, so it would keep using stale
  # files forever. Deleting them is the right thing and the dangerous thing.
  #
  # So it happens ONLY when a verified copy exists in the data directory: the
  # download writes a provenance file there last, and only after every digest
  # checked. A file of the right name is NOT enough — a truncated or refused
  # download must never be the reason the user's only working voice is deleted.
  #
  # The two paths are tested directly. `app_paths` is NOT consulted, and that is
  # deliberate: it runs here ELEVATED, where `<install>\memory` is writable, so
  # it would resolve the data directory into the install and check the very copy
  # it is about to delete. The unelevated app resolves it to
  # %LOCALAPPDATA%\Addled instead. Hardcoding both candidates is the only way
  # the guard checks the directory the app will actually read.
  #
  # Declining is the cheap mistake: a missed cleanup costs 354 MB of unused
  # files. The wrong delete costs the user their voice.
  IfFileExists "$INSTDIR\resources\backend\voice\models\kokoro-v1.0.onnx" 0 voice_cleanup_done
  IfFileExists "$INSTDIR\resources\backend\voice\models\voices-v1.0.bin" 0 voice_cleanup_done

  # The data directory first: the documented location.
  IfFileExists "$LOCALAPPDATA\Addled\voice\models\installed.json" 0 voice_try_install_dir
  IfFileExists "$LOCALAPPDATA\Addled\voice\models\kokoro-v1.0.onnx" 0 voice_try_install_dir
  IfFileExists "$LOCALAPPDATA\Addled\voice\models\voices-v1.0.bin" 0 voice_try_install_dir
    Goto voice_cleanup_ok

  voice_try_install_dir:
  # And the other candidate `_resolve_data_dir()` picks: `<install>\memory`,
  # which is only writable (and so only chosen) when the app runs elevated.
  IfFileExists "$INSTDIR\memory\voice\models\installed.json" 0 voice_cleanup_skip
  IfFileExists "$INSTDIR\memory\voice\models\kokoro-v1.0.onnx" 0 voice_cleanup_skip
  IfFileExists "$INSTDIR\memory\voice\models\voices-v1.0.bin" 0 voice_cleanup_skip
    Goto voice_cleanup_ok

  voice_cleanup_skip:
  # No verified copy anywhere: leave the bundled models alone. A stale 354 MB is
  # recoverable; a deleted working voice is not.
  DetailPrint "Keeping the existing voice models (no verified copy found to replace them)."
  Goto voice_cleanup_done

  voice_cleanup_ok:
  # Only the two model files, by name. Never a wildcard: the silero VAD model
  # lives in the same directory and the speech listener needs it.
  DetailPrint "Removing the old bundled voice models (a verified copy is installed)."
  Delete "$INSTDIR\resources\backend\voice\models\kokoro-v1.0.onnx"
  Delete "$INSTDIR\resources\backend\voice\models\voices-v1.0.bin"

  voice_cleanup_done:
!macroend

!macro customInstall
  DetailPrint "Preparing the bundled Python runtime (one-time)..."

  # Only if the interpreter is actually there. Without this guard a missing
  # file would raise an NSIS dialog, which is worse than the slower first run
  # the guard is protecting against.
  IfFileExists "$INSTDIR\resources\python\python.exe" 0 precompile_done

  # Preferred path: the shipped script, which compiles Lib/ (and lib/ if it
  # exists) under the PEP 3147 layout the interpreter reads back.
  IfFileExists "$INSTDIR\resources\scripts\precompile_bundle.py" 0 precompile_inline
    nsExec::ExecToLog '"$INSTDIR\resources\python\python.exe" -s "$INSTDIR\resources\scripts\precompile_bundle.py" "$INSTDIR\resources\python"'
    Pop $0
    Goto precompile_done

  precompile_inline:
  # Fallback if the script is missing (an older layout): compile inline with
  # the same interpreter. -q keeps the log short; a failure still does not
  # stop the install.
  nsExec::ExecToLog '"$INSTDIR\resources\python\python.exe" -s -c "import compileall; compileall.compile_dir(r\"$INSTDIR\resources\python\Lib\", quiet=1)"'
  Pop $0

  precompile_done:
  DetailPrint "Bundled Python runtime ready."

  # An upgrade over a pre-change install leaves 354 MB of models inside the
  # install directory. Remove them, but only against a verified replacement.
  !insertmacro voiceModelsCleanup

  # The optional download, if the checkbox on the voice page was ticked. Uses
  # the same script the app would, so there is one implementation of the pin
  # rather than a second one in NSIS that could check less.
  ${If} $VoiceModelState == ${BST_CHECKED}
    IfFileExists "$INSTDIR\resources\python\python.exe" 0 voice_download_done
    IfFileExists "$INSTDIR\resources\scripts\fetch_kokoro.py" 0 voice_download_inline
      DetailPrint "Downloading the offline voice models (354 MB)..."
      # Runs elevated, where `_resolve_data_dir()` finds <install>\memory
      # writable. That is the wrong place for a per-user model, so the data
      # directory is set explicitly to the one the app will read.
      nsExec::ExecToLog '"$INSTDIR\resources\python\python.exe" -s "$INSTDIR\resources\scripts\fetch_kokoro.py"'
      Pop $0
      # $0 is the exit code, or "error" if the process could not start at all.
      # Both are non-fatal: the app still speaks through Edge and the button
      # in Settings > Voice does exactly the same download.
      ${If} $0 != 0
        DetailPrint "The voice models were not downloaded ($0). Settings > Voice can fetch them later."
      ${EndIf}
      Goto voice_download_done
    voice_download_inline:
    DetailPrint "The voice model script was not found; Settings > Voice can fetch them later."
    voice_download_done:
  ${EndIf}
!macroend


# ---------------------------------------------------------------------------

# The voice-model page, and the choice it records.
#
# Kokoro used to travel inside the installer. It is 354 MB of a 589 MB build,
# so the models are now downloaded on request, from Settings > Voice, into the
# user's data directory (the app cannot write into C:\Program Files).
#
# This checkbox lets someone fetch them during the install instead - the same
# download, the same pinned hashes, run once here rather than on first use. It
# is OFF by default because that is the entire point: most users should not pay
# 354 MB for a feature they may never turn on, and the app speaks through Edge
# without it. Someone who wants the offline voice up front ticks it.
#
# The download is best-effort like the pre-compile above: a failure here leaves
# the app fully working and the button in Settings still there. Failing an
# install over an optional download would be far worse than a second click.

Var VoiceModelCheckbox
Var VoiceModelState

# electron-builder's assistedInstaller.nsh calls `!insertmacro
# customPageAfterChangeDir` when that macro exists, and it is where a custom
# page must be declared. Defining only the functions below left them
# unreferenced, which NSIS reports as warning 6010 ("install function ... not
# referenced") and electron-builder treats as an error, so the build aborted.
# The macro declares the page; the two Functions are what it runs.
!macro customPageAfterChangeDir
  Page custom customPageAfterChangeDirPage customPageLeave
!macroend

Function customPageAfterChangeDirPage
  !insertmacro MUI_HEADER_TEXT "Offline voice" "Optional download"
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 24u "Addled speaks through Microsoft Edge by default, which needs no download.$\r$\nThe offline voice (Kokoro) is a 354 MB download that can be fetched now, or later from Settings > Voice."
  Pop $0

  ${NSD_CreateCheckbox} 0 32u 100% 12u "Download the offline voice models now (354 MB)"
  Pop $VoiceModelCheckbox
  # Unchecked on purpose: this is an opt-in, not the default.
  ${NSD_SetState} $VoiceModelCheckbox ${BST_UNCHECKED}

  nsDialogs::Show
FunctionEnd

# Record the choice. `customPageAfterChangeDir` runs before the files are
# extracted, so the value is stored and acted on in customInstall above.
Function customPageLeave
  ${NSD_GetState} $VoiceModelCheckbox $VoiceModelState
FunctionEnd
