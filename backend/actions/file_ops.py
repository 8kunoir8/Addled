"""
File operations — read, write, copy, move, delete, list, search, archive.
"""

from __future__ import annotations

import logging
import os
import shutil
import glob as glob_mod

log = logging.getLogger("addled.file_ops")


class FileOps:
    """Safe file system operations.

    Every path goes through the workspace guard first: it resolves relative
    paths against the configured workspace and refuses anything outside it.
    This is the single seam all the file skills share, so the file-access mode
    is enforced in one place rather than trusted at each call site.
    """

    def _guard(self, path: str) -> tuple[str | None, dict | None]:
        """(usable path, refusal). One of the two is always None."""
        from backend.workspace import resolve
        resolved, reason = resolve(path)
        if reason:
            log.info("file access refused: %s", reason)
            return None, {"success": False, "error": reason, "blocked": True,
                          "path": str(path)}
        return str(resolved), None

    async def read(self, path: str, encoding: str = "utf-8") -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            if not os.path.isfile(path):
                return {"success": False, "error": f"Not a file: {path}"}
            with open(path, "r", encoding=encoding, errors="replace") as f:
                content = f.read()
            return {"success": True, "content": content, "size": len(content),
                    "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def write(self, path: str, content: str) -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path, "size": len(content)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def append(self, path: str, content: str) -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete(self, path: str) -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                return {"success": False, "error": f"Not found: {path}"}
            return {"success": True, "summary": f"Deleted {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def copy(self, source: str, dest: str,
                   overwrite: bool = False) -> dict:
        """Copy a file or folder, refusing to clobber by default.

        `shutil.copy2` overwrites silently and `shutil.copytree` raises — the
        two branches disagreed about what happens when the destination is
        occupied, and the file branch was the destructive one. A model that
        derived `dest` from a guess then destroyed whatever was already there
        and was told `success: True`. Overwriting is now something the caller
        has to ask for.
        """
        source, refused = self._guard(source)
        if refused:
            return refused
        dest, refused = self._guard(dest)
        if refused:
            return refused
        try:
            if not os.path.exists(source):
                return {"success": False, "error": f"Source not found: {source}"}

            if os.path.exists(dest) and not overwrite:
                kind = "folder" if os.path.isdir(dest) else "file"
                return {"success": False, "blocked": True, "path": dest,
                        "error": (f"The destination {kind} already exists: "
                                  f"{dest}. Pass overwrite=true to replace it, "
                                  f"or choose another name.")}

            if os.path.isfile(source):
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                if os.path.exists(dest):
                    # Keep the old contents beside the new, so an overwrite is
                    # still recoverable — this is the file the user may have
                    # been working on.
                    try:
                        shutil.copy2(dest, dest + ".bak")
                    except OSError as e:  # noqa: BLE001
                        log.debug("could not back up %s: %s", dest, e)
                shutil.copy2(source, dest)
            elif os.path.isdir(source):
                if os.path.isdir(dest):
                    # `copytree` nests the source INSIDE an existing directory,
                    # which is almost never what "copy to dest" means and is
                    # exactly the trap `Copy-Item -Recurse` has. Copy child by
                    # child into it instead, so dest means dest.
                    shutil.copytree(source, dest, dirs_exist_ok=True)
                else:
                    shutil.copytree(source, dest)
            return {"success": True, "summary": f"Copied to {dest}",
                    "path": dest}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def move(self, source: str, dest: str,
                   overwrite: bool = False) -> dict:
        """Move a file or folder, refusing to clobber by default.

        `shutil.move` has two behaviours the caller cannot tell apart from the
        result: into an existing FILE it overwrites, into an existing DIRECTORY
        it moves the source INSIDE it. Both returned `success: True`. The
        resolved destination is now reported so the caller knows which happened.
        """
        source, refused = self._guard(source)
        if refused:
            return refused
        dest, refused = self._guard(dest)
        if refused:
            return refused
        try:
            if not os.path.exists(source):
                return {"success": False, "error": f"Source not found: {source}"}
            if os.path.exists(dest) and not overwrite:
                kind = "folder" if os.path.isdir(dest) else "file"
                return {"success": False, "blocked": True, "path": dest,
                        "error": (f"The destination {kind} already exists: "
                                  f"{dest}. Pass overwrite=true to replace it, "
                                  f"or choose another path.")}
            if os.path.exists(dest) and os.path.isfile(dest):
                try:
                    shutil.copy2(dest, dest + ".bak")
                except OSError as e:  # noqa: BLE001
                    log.debug("could not back up %s: %s", dest, e)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            final = shutil.move(source, dest)
            return {"success": True, "summary": f"Moved to {final}",
                    "path": str(final)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def list_dir(self, path: str = ".", pattern: str = "*") -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            if not os.path.isdir(path):
                return {"success": False, "error": f"Not a directory: {path}"}
            items = []
            for entry in os.scandir(path):
                items.append({
                    "name": entry.name,
                    "path": entry.path,
                    "is_dir": entry.is_dir(),
                    "size": entry.stat().st_size if entry.is_file() else 0,
                })
            return {"success": True, "items": items, "count": len(items)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_dir(self, path: str) -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            os.makedirs(path, exist_ok=True)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search(self, directory: str, pattern: str, recursive: bool = True) -> dict:
        directory, refused = self._guard(directory)
        if refused:
            return refused
        try:
            search_path = os.path.join(directory, "**" if recursive else "", pattern)
            matches = glob_mod.glob(search_path, recursive=recursive)
            return {"success": True, "matches": matches[:500], "count": len(matches)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def info(self, path: str) -> dict:
        path, refused = self._guard(path)
        if refused:
            return refused
        try:
            st = os.stat(path)
            return {
                "success": True,
                "path": path,
                "size": st.st_size,
                "is_dir": os.path.isdir(path),
                "is_file": os.path.isfile(path),
                "modified": st.st_mtime,
                "created": st.st_ctime,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
