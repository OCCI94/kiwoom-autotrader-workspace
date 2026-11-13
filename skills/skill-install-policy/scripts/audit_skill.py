#!/usr/bin/env python3
"""Static audit of an agent skill directory (SKILL.md + bundled files).

Read-only. Standard library only. No network. Python 3.9+.

Exit codes: 0 no findings, 1 findings present, 3 usage or runtime error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

PATTERNS = [
    ("network_url", re.compile(r"https?://[^\s\"'<>)]+")),
    ("shell_pipe_exec", re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?|node)\b")),
    ("base64_decode", re.compile(r"base64\s*(-d|--decode)|b64decode\(|atob\(")),
    ("dynamic_exec", re.compile(r"\b(eval|exec)\s*\(|\bos\.system\(|subprocess\.(run|Popen|call|check_output)\(")),
    ("env_read", re.compile(r"os\.environ|os\.getenv\(|\$\{?[A-Z][A-Z0-9_]{2,}\}?")),
    ("secret_like", re.compile(r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|[0-9]{8,10}:[A-Za-z0-9_-]{35}|(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"'])", re.I)),
    ("credential_path", re.compile(r"\.aws/credentials|\.ssh/id_|\.netrc|keychain|Keychain|\.env\b")),
    ("disable_security", re.compile(r"(disable|turn off|bypass|skip)\s+(security|sandbox|permission|approval|warning)", re.I)),
    ("hidden_unicode", re.compile("[​‌‍⁠‪-‮⁦-⁩﻿]")),
    ("instruction_override", re.compile(r"(ignore (all )?(previous|prior) instructions|you are now|system prompt|administrator|test mode)", re.I)),
]

TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".xml", ".plist", ".html", ".csv"}


def read_frontmatter(skill_md: Path):
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return None, f"cannot read SKILL.md: {error}"
    if not text.startswith("---"):
        return None, "no frontmatter"
    end = text.find("\n---", 3)
    if end < 0:
        return None, "unterminated frontmatter"
    keys = []
    for line in text[3:end].splitlines():
        if line and not line.startswith((" ", "\t", "#")) and ":" in line:
            keys.append(line.split(":", 1)[0].strip())
    return keys, None


SELF_DIR = Path(__file__).resolve().parent  # this policy skill's own scanner scripts


def audit(root: Path, max_bytes: int):
    findings = []
    files = []
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file() and not path.is_symlink() and path.resolve().parent == SELF_DIR:
            files.append({"file": rel, "bytes": path.stat().st_size, "skipped": "scanner's own pattern definitions"})
            continue
        if path.is_symlink():
            target = os.path.realpath(path)
            escapes = not target.startswith(str(root.resolve()))
            findings.append({"file": rel, "kind": "symlink", "detail": target, "severity": "reject" if escapes else "review"})
            continue
        if path.is_dir():
            continue
        size = path.stat().st_size
        files.append({"file": rel, "bytes": size})
        if path.name.startswith(".") and path.name != ".gitignore":
            findings.append({"file": rel, "kind": "hidden_file", "detail": "", "severity": "review"})
        if path.suffix.lower() not in TEXT_SUFFIXES:
            if size > 0:
                findings.append({"file": rel, "kind": "non_text_file", "detail": path.suffix or "(no suffix)", "severity": "review"})
            continue
        if size > max_bytes:
            findings.append({"file": rel, "kind": "large_file", "detail": f"{size} bytes > {max_bytes}", "severity": "review"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                snippet = match.group(0)
                if kind == "secret_like":
                    snippet = snippet[:12] + "…"  # never echo a full secret-looking value
                severity = "reject" if kind in {"shell_pipe_exec", "credential_path", "disable_security", "hidden_unicode", "instruction_override"} else "review"
                if kind == "secret_like":
                    severity = "reject"
                if path.suffix.lower() in {".md", ".txt"} and kind in {"shell_pipe_exec", "credential_path"}:
                    severity = "review"  # prose describing a pattern is not an execution of it
                findings.append({"file": rel, "kind": kind, "line": line, "detail": snippet[:160], "severity": severity})
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        findings.append({"file": "SKILL.md", "kind": "missing_skill_md", "detail": "", "severity": "reject"})
    else:
        keys, error = read_frontmatter(skill_md)
        if error:
            findings.append({"file": "SKILL.md", "kind": "frontmatter", "detail": error, "severity": "reject"})
        else:
            extra = sorted(set(keys) - SPEC_KEYS)
            if extra:
                findings.append({"file": "SKILL.md", "kind": "non_spec_frontmatter_key", "detail": ", ".join(extra), "severity": "review"})
            if "allowed-tools" in keys:
                findings.append({"file": "SKILL.md", "kind": "allowed_tools_present", "detail": "pre-approves tools in Claude Code; strip for third-party skills", "severity": "review"})
    return files, findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Static audit of a skill directory. Read-only, no network.")
    parser.add_argument("skill_dir")
    parser.add_argument("--format", choices=["json", "table"], default="json")
    parser.add_argument("--max-bytes", type=int, default=2_000_000, help="skip scanning text files larger than this")
    args = parser.parse_args(argv)
    root = Path(args.skill_dir).expanduser()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 3
    try:
        files, findings = audit(root, args.max_bytes)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    summary = {}
    for finding in findings:
        summary[finding["kind"]] = summary.get(finding["kind"], 0) + 1
    rejects = sum(1 for f in findings if f["severity"] == "reject")
    result = {
        "skill_dir": str(root),
        "files": files,
        "findings": findings,
        "summary": summary,
        "reject_count": rejects,
        "verdict_hint": "reject" if rejects else ("review" if findings else "clean"),
    }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{'severity':8} {'kind':26} {'file':40} line  detail")
        for f in findings:
            print(f"{f['severity']:8} {f['kind']:26} {f['file'][:40]:40} {str(f.get('line', '')):5} {f['detail']}")
        print(f"\nfiles={len(files)} findings={len(findings)} reject={rejects} hint={result['verdict_hint']}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
