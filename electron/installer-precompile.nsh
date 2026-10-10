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
!macroend
