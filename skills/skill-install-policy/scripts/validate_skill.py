#!/usr/bin/env python3
"""Validate a skill directory against the agentskills.io SKILL.md conventions.

Approximates `skills-ref validate` without dependencies. Read-only. Python 3.9+.

Checks: frontmatter present and terminated; only the six spec keys; `name`
1-64 chars, lowercase letters/digits/hyphens, no leading/trailing/double hyphen,
equal to the directory name; `description` 1-1024 chars; `compatibility` <= 500;
`metadata` is a map of strings; body <= 500 lines (warning); relative links in
SKILL.md point at existing files; no Claude-only placeholders in the body.

Exit codes: 0 pass, 1 warnings only, 2 violations, 3 usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SPEC_KEYS = ["name", "description", "license", "compatibility", "metadata", "allowed-tools"]
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# `!`cmd`` must stay on one line; `$1`-style substitutions are not preceded by a
# letter or `$`, which keeps Excel absolute references such as `$B$2` out.
CLAUDE_ONLY = [r"\$ARGUMENTS", r"\$\{CLAUDE_SKILL_DIR\}", r"!`[^`\n]+`", r"(?<![A-Za-z$])\$\d\b"]
LINK_RE = re.compile(r"\]\(([^)#\s]+)\)|`((?:scripts|references|assets|evals)/[^`\s]+)`")


BLOCK_SCALAR_RE = re.compile(r"^[>|][+-]?$")


def _fold_block(lines, style):
    """Join the indented lines of a YAML block scalar. '|' keeps line breaks;
    '>' folds them into spaces and turns a blank line into a line break.
    Trailing newlines are dropped (chomping does not matter for validation)."""
    indent = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
    stripped = [l[indent:] if l.strip() else "" for l in lines]
    if style[0] == "|":
        return "\n".join(stripped).rstrip("\n")
    out, para = [], []
    for l in stripped:
        if l == "":
            out.append(" ".join(para)); para = []
        else:
            para.append(l.strip())
    out.append(" ".join(para))
    return "\n".join(out).rstrip("\n")


def parse_frontmatter(text: str):
    """Minimal YAML subset: `key: value` lines, block scalars (`>`/`|`, with
    optional `-`/`+`) for a top-level value, plus one nested map (metadata)."""
    if not text.startswith("---\n"):
        return None, None, "frontmatter must start with '---'"
    end = text.find("\n---", 4)
    if end < 0:
        return None, None, "frontmatter not terminated"
    block = text[4:end]
    body = text[end + 4:]
    data = {}
    current = None
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw.startswith((" ", "\t")):
            if current is None or not isinstance(data.get(current), dict):
                return None, None, f"unexpected indented line: {raw!r}"
            key, _, value = raw.strip().partition(":")
            data[current][key.strip()] = _unquote(value.strip())
            i += 1
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            return None, None, f"malformed line: {raw!r}"
        key = key.strip()
        value = value.strip()
        if BLOCK_SCALAR_RE.match(value):
            j = i + 1
            collected = []
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith((" ", "\t"))):
                collected.append(lines[j])
                j += 1
            if not any(l.strip() for l in collected):
                return None, None, f"empty block scalar for {key!r}"
            data[key] = _fold_block(collected, value)
            current = None
            i = j
            continue
        if value == "":
            data[key] = {}
            current = key
        else:
            data[key] = _unquote(value)
            current = None
        i += 1
    return data, body, None

def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def validate(skill_dir: Path):
    violations, warnings = [], []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return ["SKILL.md missing"], warnings
    text = skill_md.read_text(encoding="utf-8")
    data, body, error = parse_frontmatter(text)
    if error:
        return [error], warnings
    extra = [k for k in data if k not in SPEC_KEYS]
    if extra:
        violations.append(f"non-spec frontmatter keys: {extra}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        violations.append("name missing")
    else:
        if len(name) > 64:
            violations.append("name longer than 64 chars")
        if not NAME_RE.match(name):
            violations.append("name must be lowercase letters, digits and single hyphens, no leading/trailing hyphen")
        if name != skill_dir.resolve().name:
            violations.append(f"name {name!r} differs from directory {skill_dir.resolve().name!r}")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        violations.append("description missing")
    elif len(description) > 1024:
        violations.append(f"description {len(description)} chars > 1024")
    compatibility = data.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility) > 500:
        violations.append(f"compatibility {len(compatibility)} chars > 500")
    metadata = data.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
            violations.append("metadata must be a map of string values")
    body_lines = body.count("\n")
    if body_lines > 500:
        warnings.append(f"body has {body_lines} lines > 500 recommended")
    for pattern in CLAUDE_ONLY:
        if re.search(pattern, body):
            warnings.append(f"Claude-only construct found: /{pattern}/ (breaks Codex portability)")
    for match in LINK_RE.finditer(body):
        target = match.group(1) or match.group(2)
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (skill_dir / target).exists():
            warnings.append(f"referenced path does not exist: {target}")
    for sub in ("scripts", "references", "assets", "evals"):
        path = skill_dir / sub
        if path.exists() and not path.is_dir():
            violations.append(f"{sub} exists but is not a directory")
    return violations, warnings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate SKILL.md frontmatter and layout (spec approximation).")
    parser.add_argument("skill_dir", nargs="+")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    worst = 0
    results = []
    for raw in args.skill_dir:
        skill_dir = Path(raw).expanduser()
        if not skill_dir.is_dir():
            print(f"error: not a directory: {skill_dir}", file=sys.stderr)
            return 3
        violations, warnings = validate(skill_dir)
        status = "fail" if violations else ("warn" if warnings else "pass")
        worst = max(worst, {"pass": 0, "warn": 1, "fail": 2}[status])
        results.append({"skill_dir": str(skill_dir), "status": status, "violations": violations, "warnings": warnings})
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"[{r['status'].upper()}] {r['skill_dir']}")
            for v in r["violations"]:
                print(f"  violation: {v}")
            for w in r["warnings"]:
                print(f"  warning:   {w}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
