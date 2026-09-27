"""
Language detection — identifies programming language from file extension and content.
"""

from __future__ import annotations

import os

EXTENSION_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
    ".bash": "shell",
    ".bat": "batch",
    ".ps1": "powershell",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".r": "r",
    ".lua": "lua",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".env": "env",
    ".txt": "text",
    ".xml": "xml",
    ".svg": "xml",
    ".vue": "vue",
    ".svelte": "svelte",
}

def detect(filepath: str) -> str:
    """Detect language from file path/extension."""
    ext = os.path.splitext(filepath)[1].lower()
    return EXTENSION_MAP.get(ext, ext.lstrip(".") if ext else "text")

def detect_from_content(content: str, filepath: str = "") -> str:
    """Detect language from file content heuristics (fallback for unknown extensions)."""
    # Try shebang
    first_line = content.split("\n")[0].strip() if content else ""
    if first_line.startswith("#!/usr/bin/env python") or first_line.startswith("#!/usr/bin/python"):
        return "python"
    if first_line.startswith("#!/usr/bin/env node") or first_line.startswith("#!/usr/bin/node"):
        return "javascript"
    if first_line.startswith("#!/bin/bash") or first_line.startswith("#!/bin/sh"):
        return "shell"

    # Try filename patterns
    filename = os.path.basename(filepath).lower()
    if filename in ("dockerfile",):
        return "dockerfile"
    if filename in ("makefile",):
        return "makefile"
    if filename in ("gemfile",):
        return "ruby"

    # Content heuristics
    if content.strip().startswith("<?xml") or content.strip().startswith("<!DOCTYPE html"):
        return "html"
    if content.strip().startswith("{"):
        return "json"
    if "import React" in content or "from react" in content:
        return "typescript" if ":" in content else "javascript"
    if "def " in content and ":" in content:
        return "python"
    if "fn " in content and "{" in content:
        return "rust"

    return detect(filepath)
