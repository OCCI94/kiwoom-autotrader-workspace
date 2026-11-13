#!/usr/bin/env python3
"""Export every worksheet of a nearby Excel workbook to tr_docs/*.md.

Install: python3 -m pip install -r requirements-tr-docs.txt
Run from any directory: python3 /path/to/ai-stock/export_tr_docs.py

The last complete, nonempty pair of ASCII/fullwidth parentheses supplies the
filename; otherwise the whole sheet title is used. Unsafe filename characters
are replaced with underscores. Case-insensitive collisions receive __2, __3,
etc. and are reported (use --collision error to refuse instead).

Only generated Markdown and a hash manifest are written. Existing unrelated or
edited files require --overwrite. Old generated files are never deleted.
The workbook is opened for reading only; formulas and examples are not executed
or corrected. Simple integer zero-padding formats retain displayed leading zeros.
Other values retain their stored value, rather than simulating Excel formatting.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata


GENERATOR = "export_tr_docs.py"
MANIFEST = ".export_tr_docs_manifest.json"
PREFERRED_WORKBOOK = "키움 REST API 문서.xlsx"
HEADINGS = {
    "API 정보", "기본정보", "개요", "Request", "Response",
    "Request Example", "Response Example", "요청", "응답",
}


class ExportError(Exception):
    """An input or output conflict the user can resolve."""


@dataclass(frozen=True)
class ExportResult:
    workbook: Path
    output_dir: Path
    files: tuple[Path, ...]
    changed: int
    warnings: tuple[str, ...]
    source_sha256: str


def executable_directory() -> Path:
    """Use the script/executable location, never the process working directory."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def filename_key(name: str) -> str:
    # NFC also detects names equivalent on the common macOS filesystem.
    return unicodedata.normalize("NFC", name).casefold()


def sheet_stem(title: str) -> str:
    """Use the final nonempty, innermost matched parentheses, then sanitize."""
    groups = re.findall(r"\(([^()（）]*)\)|（([^()（）]*)）", title)
    names = [left or right for left, right in groups if (left or right).strip()]
    name = unicodedata.normalize("NFC", names[-1] if names else title).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        name = "sheet"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    # Excel titles are short; keep direct function calls portable as well.
    if len(name.encode("utf-8")) > 180:
        name = name.encode("utf-8")[:160].decode("utf-8", errors="ignore") + "_" + sha256(name.encode())[:12]
    return name


def plan_filenames(titles: list[str], collision: str = "suffix") -> tuple[dict[str, str], list[str]]:
    if collision not in {"suffix", "error"}:
        raise ExportError("collision must be 'suffix' or 'error'")
    planned: dict[str, str] = {}
    used: dict[str, str] = {}
    warnings: list[str] = []
    # Reserve the natural names so a suffix cannot steal a later sheet's name.
    reserved = {filename_key(sheet_stem(title) + ".md") for title in titles}
    for title in titles:
        stem = sheet_stem(title)
        filename = stem + ".md"
        key = filename_key(filename)
        if key in used:
            if collision == "error":
                raise ExportError(f"파일명 충돌: {used[key]!r}, {title!r} → {filename}")
            counter = 2
            while True:
                filename = f"{stem}__{counter}.md"
                candidate = filename_key(filename)
                if candidate not in used and candidate not in reserved:
                    break
                counter += 1
            warnings.append(f"파일명 충돌: {title!r} → {filename} (기존: {used[key]!r})")
            key = filename_key(filename)
        used[key] = title
        planned[title] = filename
    return planned, warnings


def find_workbook(base_dir: Path) -> Path:
    preferred = base_dir / PREFERRED_WORKBOOK
    if preferred.is_file():
        return preferred
    candidates = sorted(
        (p for p in base_dir.iterdir() if p.is_file() and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$")),
        key=lambda p: p.name,
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ExportError(f"엑셀 파일이 없습니다: {base_dir} (.xlsx를 스크립트 옆에 두거나 --input 지정)")
    raise ExportError("엑셀 파일이 여러 개입니다. --input으로 지정하세요: " + ", ".join(p.name for p in candidates))


def value_text(cell) -> str:
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float)) and float(value).is_integer() and re.fullmatch(r"0+", cell.number_format or ""):
        number = int(value)
        return ("-" if number < 0 else "") + str(abs(number)).zfill(len(cell.number_format))
    if cell.data_type == "f" and not isinstance(value, str):
        # openpyxl represents array formulas as objects in newer releases.
        return str(getattr(value, "text", value))
    return str(value)


def markdown_text(value: str) -> str:
    """Render literal text, including pipes and HTML-like API type names."""
    escaped = html.escape(value, quote=False)
    # Intraword underscores are literal in Markdown and keeping them intact
    # makes API field names searchable in both source and rendered documents.
    for char in "\\`*[]|":
        escaped = escaped.replace(char, f"&#{ord(char)};")
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def fenced(value: str) -> str:
    # A source example may itself contain Markdown fences. Never repair JSON.
    longest = max((len(match) for match in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "text\n" + value + ("" if value.endswith("\n") else "\n") + fence + "\n"


def render_sheet(sheet, cached_sheet, workbook_name: str, source_hash: str) -> tuple[str, int]:
    from openpyxl.utils import get_column_letter

    lines = [
        f"<!-- Generated by {GENERATOR}; edit the workbook and rerun. -->",
        f"# {markdown_text(sheet.title)}", "",
        f"원본: {markdown_text(workbook_name)} · 시트: {markdown_text(sheet.title)}",
        f"<!-- source-sha256: {source_hash} -->", "",
        "원본 행·열 위치를 함께 표시합니다. 병합된 값은 좌상단 셀에 한 번만 표시합니다.", "",
    ]
    pending: list[list] = []
    links: list[tuple[str, str, str]] = []
    formulas: list[tuple[str, str, str]] = []
    comments: list[tuple[str, str]] = []
    count = 0

    def flush() -> None:
        if not pending:
            return
        columns = sorted({cell.column for row in pending for cell in row})
        # Keep the source field names as real table headers where identifiable.
        first = {cell.column: value_text(cell) for cell in pending[0]}
        recognized = {"Element", "Type"}.issubset(first.values()) or {"No.", "API ID"}.issubset(first.values())
        if recognized:
            labels = [f"{markdown_text(first.get(col, ''))} ({get_column_letter(col)})" for col in columns]
            rows = pending[1:]
            lines.append(f"<!-- 원본 표 머리글: {pending[0][0].row}행 -->")
        else:
            labels = [get_column_letter(col) for col in columns]
            rows = pending
        lines.append("| 행 | " + " | ".join(labels) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            values = {cell.column: markdown_text(value_text(cell)) for cell in row}
            lines.append(f"| {row[0].row} | " + " | ".join(values.get(col, "") for col in columns) + " |")
        lines.append("")
        pending.clear()

    for row in sheet.iter_rows():
        cells = [cell for cell in row if cell.value is not None and cell.value != ""]
        for cell in row:
            if cell.hyperlink:
                link = cell.hyperlink
                if link.target:
                    links.append((cell.coordinate, "target", link.target))
                if link.location:
                    links.append((cell.coordinate, "location", link.location))
            if cell.comment:
                comments.append((cell.coordinate, cell.comment.text))
        count += len(cells)
        for cell in cells:
            if cell.data_type == "f":
                cached = value_text(cached_sheet[cell.coordinate])
                formulas.append((cell.coordinate, value_text(cell), cached))
        if not cells:
            flush()
            continue
        if len(cells) == 1:
            flush()
            cell = cells[0]
            value = value_text(cell)
            lines.append(f"<!-- 원본 셀: {cell.coordinate} -->")
            if "\n" in value or "\r" in value or value.lstrip().startswith(("{", "[")):
                lines.append(fenced(value))
            elif value in HEADINGS:
                lines.extend([f"## {markdown_text(value)}", ""])
            else:
                lines.extend([markdown_text(value), ""])
        else:
            pending.append(cells)
    flush()
    if count == 0:
        lines.extend(["(비어 있는 시트)", ""])
    if formulas:
        lines.extend(["## 수식과 저장된 결과", "", "수식을 계산하지 않고 파일에 저장된 수식과 캐시만 표시합니다.", ""])
        for coordinate, formula, cached in formulas:
            lines.extend([f"### {coordinate}", "", fenced(formula), "저장된 결과: " + (markdown_text(cached) if cached else "(캐시 없음)"), ""])
    if comments:
        lines.extend(["## 셀 메모 원문", ""])
        for coordinate, comment in comments:
            lines.extend([f"### {coordinate}", "", fenced(comment)])
    if links:
        # Preserve raw destinations as text. Do not follow links or turn an
        # attachment-supplied javascript/file URL into an actionable link.
        lines.extend(["## 원본 하이퍼링크", "", "| 셀 | 종류 | 대상 |", "| --- | --- | --- |"])
        lines.extend(f"| {coord} | {kind} | {markdown_text(target)} |" for coord, kind, target in links)
        lines.append("")
    if sheet.merged_cells.ranges:
        merges = sorted(sheet.merged_cells.ranges, key=lambda item: (item.min_row, item.min_col))
        lines.extend(["## 원본 병합 범위", "", ", ".join(str(item) for item in merges), ""])
    return "\n".join(lines).rstrip() + "\n", count


def load_manifest(path: Path) -> dict:
    if path.is_symlink():
        raise ExportError(f"심볼릭 링크에는 쓰지 않습니다: {path}")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("generator") != GENERATOR or data.get("version") != 1 or not isinstance(data.get("sheets"), list):
            raise ValueError("unknown manifest")
        for item in data["sheets"]:
            if not isinstance(item.get("filename"), str) or not isinstance(item.get("sha256"), str):
                raise ValueError("invalid manifest entry")
        return data
    except (ValueError, TypeError, AttributeError) as exc:
        raise ExportError(f"기존 매니페스트를 확인할 수 없습니다: {path} ({exc})") from exc


def atomic_write(path: Path, content: bytes) -> bool:
    if path.exists() and path.read_bytes() == content:
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tr-docs-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def export_workbook(input_path: Path | str | None = None, output_dir: Path | str | None = None,
                    *, overwrite: bool = False, collision: str = "suffix", base_dir: Path | None = None) -> ExportResult:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ExportError("openpyxl이 필요합니다. python3 -m pip install -r requirements-tr-docs.txt") from exc
    base = (base_dir or executable_directory()).resolve()
    source = Path(input_path).expanduser().resolve() if input_path is not None else find_workbook(base)
    destination = Path(output_dir).expanduser().absolute() if output_dir is not None else base / "tr_docs"
    if not source.is_file():
        raise ExportError(f"입력 파일이 없습니다: {source}")
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ExportError(f"출력 경로는 실제 디렉터리여야 합니다: {destination}")
    source_hash = sha256(source.read_bytes())
    workbook = load_workbook(source, data_only=False, keep_links=False)
    cached = load_workbook(source, data_only=True, keep_links=False)
    try:
        filenames, warnings = plan_filenames(workbook.sheetnames, collision)
        outputs: dict[str, bytes] = {}
        entries = []
        for sheet in workbook.worksheets:
            markdown, count = render_sheet(sheet, cached[sheet.title], source.name, source_hash)
            content = markdown.encode("utf-8")
            filename = filenames[sheet.title]
            outputs[filename] = content
            entries.append({"sheet": sheet.title, "filename": filename, "nonempty_cells": count, "sha256": sha256(content)})
    finally:
        workbook.close()
        cached.close()
    old_manifest = load_manifest(destination / MANIFEST)
    previous = {item["filename"]: item["sha256"] for item in old_manifest.get("sheets", [])}
    existing = {filename_key(item.name): item for item in destination.iterdir()} if destination.exists() else {}
    # Preflight every target before creating or changing anything.
    for filename in outputs:
        target = existing.get(filename_key(filename), destination / filename)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ExportError(f"출력 대상이 일반 파일이 아닙니다: {target}")
        if target.exists():
            if target.name != filename:
                raise ExportError(f"기존 파일과 대소문자/유니코드 이름이 충돌합니다: {filename}, {target.name}")
            managed = previous.get(filename) == sha256(target.read_bytes())
            if not managed and not overwrite:
                raise ExportError(f"기존 수동 작성 또는 수정 파일을 보존합니다: {target}. 덮어쓰려면 --overwrite 지정")
    manifest = {"generator": GENERATOR, "version": 1, "workbook": source.name,
                "source_sha256": source_hash, "sheets": entries, "warnings": warnings}
    if sha256(source.read_bytes()) != source_hash:
        raise ExportError("변환 중 원본 파일이 변경되었습니다. 다시 실행하세요.")
    destination.mkdir(parents=True, exist_ok=True)
    changed = sum(atomic_write(destination / name, content) for name, content in outputs.items())
    atomic_write(destination / MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return ExportResult(source, destination, tuple(destination / name for name in outputs), changed, tuple(warnings), source_hash)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="입력 .xlsx 경로 (기본: 실행 파일 옆의 엑셀)")
    parser.add_argument("--output-dir", type=Path, help="출력 폴더 (기본: 실행 파일 옆의 tr_docs)")
    parser.add_argument("--collision", choices=("suffix", "error"), default="suffix", help="시트 이름 충돌 처리 (기본: suffix)")
    parser.add_argument("--overwrite", action="store_true", help="대상 이름과 같은 기존 수동 작성/수정 Markdown도 덮어쓰기")
    args = parser.parse_args(argv)
    try:
        result = export_workbook(args.input, args.output_dir, overwrite=args.overwrite, collision=args.collision)
    except (ExportError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"주의: {warning}", file=sys.stderr)
    print(f"완료: {len(result.files)}개 시트 → {result.output_dir} (새로 작성/갱신 {result.changed}개)")
    print(f"원본 SHA-256: {result.source_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
