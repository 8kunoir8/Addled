"""Tools Addled downloads on request rather than carrying in the installer.

The rule for anything here: the app works without it, and the user asks for it
from the dashboard. `backend/tools/rtk.py` holds the token saver, which is the
first of them — 13 MB of third-party binaries that used to be shipped inside
every build even though the repository never contained them.
"""
