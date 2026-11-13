"""Offline tests for the standalone workbook documentation exporter."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

from export_tr_docs import (
    ExportError, PREFERRED_WORKBOOK, executable_directory,
    export_workbook, find_workbook, plan_filenames, sheet_stem,
)


class ExportTrDocsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / PREFERRED_WORKBOOK

    def save(self, names=("au10001",)):
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name in names:
            workbook.create_sheet(name)
        workbook.save(self.source)
        workbook.close()
        return load_workbook(self.source)

    def test_filename_parentheses_fullwidth_sanitization(self):
        cases = {
            "접근토큰발급(au10001)": "au10001", "요청（ka10080）": "ka10080",
            "앞(first) 뒤(last)": "last", "값()": "값()", "API 리스트": "API 리스트",
            "시트(../../escape)": "_.._escape", "(CON)": "_CON", "(..)": "sheet",
            "원본(가(나))": "나",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(sheet_stem(title), expected)
                self.assertEqual(Path(sheet_stem(title)).name, sheet_stem(title))

    def test_case_collision_suffixes_are_explicit_and_do_not_steal_names(self):
        mapping, warnings = plan_filenames(["ETF(0G)", "주식(0g)", "별도(0g__2)"])
        self.assertEqual(mapping, {"ETF(0G)": "0G.md", "주식(0g)": "0g__3.md", "별도(0g__2)": "0g__2.md"})
        self.assertEqual(len(warnings), 1)
        with self.assertRaises(ExportError):
            plan_filenames(["첫째(a)", "둘째(A)"], "error")

    def test_all_sheets_content_merges_empty_and_idempotency(self):
        book = self.save(("접근토큰(au10001)", "API 리스트", "빈 시트"))
        sheet = book.worksheets[0]
        sheet.append(["Request"])
        sheet.append(["구분", "Element", "한글명", "Type", "Required", "Length", "Description"])
        sheet.append(["Header", "api-id", "TR명", "String", "Y", "10", "a|b <List<Map>>\n둘째 줄"])
        sheet.append([None, "authorization", "접근토큰", "String", "Y", "1000", 'Bearer "token"'])
        sheet.merge_cells("A3:A4")
        sheet.append(["Response Example"])
        example = '{\n  "code": "005930"\n  "missing_comma": true,\n  "fence": "```"\n}'
        sheet.append([example])
        sheet.merge_cells("A6:G6")
        sheet["A7"] = "005930"
        sheet["B7"] = 5930
        sheet["B7"].number_format = "000000"
        sheet["C7"] = 0
        sheet["D7"] = False
        sheet["A8"] = "이동"
        sheet["A8"].hyperlink = "#'API 리스트'!A1"
        sheet["A8"].comment = Comment("원본 메모\n변경하지 않음", "source")
        book.worksheets[1].append(["No.", "API ID", "API 명"])
        book.worksheets[1].append(["1", "au10001", "접근토큰발급"])
        book.save(self.source)
        book.close()
        original = self.source.read_bytes()
        result = export_workbook(base_dir=self.root)
        self.assertEqual(len(result.files), 3)
        self.assertEqual(result.changed, 3)
        text = (result.output_dir / "au10001.md").read_text()
        self.assertIn(example, text)
        self.assertIn("````text", text)
        self.assertIn("a&#124;b &lt;List&lt;Map&gt;&gt;<br>둘째 줄", text)
        self.assertIn('"missing_comma": true,', text)
        self.assertIn("| 7 | 005930 | 005930 | 0 | FALSE |", text)
        self.assertIn("| 3 | Header |", text)
        self.assertIn("| 4 |  | authorization |", text)
        self.assertIn("A3:A4", text)
        self.assertIn("원본 메모\n변경하지 않음", text)
        self.assertIn("#'API 리스트'!A1", text)
        self.assertIn("(비어 있는 시트)", (result.output_dir / "빈 시트.md").read_text())
        before_mtime = {path: path.stat().st_mtime_ns for path in result.files}
        again = export_workbook(base_dir=self.root)
        self.assertEqual(again.changed, 0)
        self.assertEqual(before_mtime, {path: path.stat().st_mtime_ns for path in result.files})
        self.assertEqual(self.source.read_bytes(), original)

    def test_formulas_keep_source_and_cached_result_without_evaluation(self):
        book = self.save()
        book.active["A1"] = "=1+2"
        book.active["B1"] = '=HYPERLINK("https://invalid.example/", "do not open")'
        book.save(self.source)
        book.close()
        # Set an intentionally stale cache. The exporter must report 99, not 3.
        with ZipFile(self.source) as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist()}
        members["xl/worksheets/sheet1.xml"] = members["xl/worksheets/sheet1.xml"].replace(b"<f>1+2</f><v></v>", b"<f>1+2</f><v>99</v>")
        with ZipFile(self.source, "w", ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        export_workbook(base_dir=self.root)
        text = (self.root / "tr_docs/au10001.md").read_text()
        self.assertIn("=1+2", text)
        self.assertIn("저장된 결과: 99", text)
        self.assertIn("(캐시 없음)", text)

    def test_existing_handwritten_and_edited_files_are_protected_before_any_write(self):
        book = self.save(("first", "second"))
        book.close()
        output = self.root / "tr_docs"
        output.mkdir()
        (output / "README.md").write_text("handwritten readme")
        (output / "second.md").write_text("handwritten spec")
        with self.assertRaises(ExportError):
            export_workbook(base_dir=self.root)
        self.assertFalse((output / "first.md").exists())
        self.assertEqual((output / "second.md").read_text(), "handwritten spec")
        export_workbook(base_dir=self.root, overwrite=True)
        (output / "first.md").write_text("manual correction")
        with self.assertRaises(ExportError):
            export_workbook(base_dir=self.root)
        self.assertEqual((output / "README.md").read_text(), "handwritten readme")

    def test_symlink_target_cannot_escape_output_directory(self):
        book = self.save()
        book.close()
        output = self.root / "tr_docs"
        output.mkdir()
        outside = self.root / "keep.txt"
        outside.write_text("preserve")
        (output / "au10001.md").symlink_to(outside)
        with self.assertRaises(ExportError):
            export_workbook(base_dir=self.root, overwrite=True)
        self.assertEqual(outside.read_text(), "preserve")

    def test_defaults_work_from_other_cwd_and_frozen_executable(self):
        book = self.save()
        book.close()
        script_source = Path(__file__).resolve().parents[1] / "export_tr_docs.py"
        script = self.root / script_source.name
        shutil.copy2(script_source, script)
        other_cwd = self.root / "elsewhere"
        other_cwd.mkdir()
        process = subprocess.run([sys.executable, str(script)], cwd=other_cwd, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue((self.root / "tr_docs/au10001.md").is_file())
        self.assertFalse((other_cwd / "tr_docs").exists())
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(self.root / "exporter")):
            self.assertEqual(executable_directory(), self.root.resolve())

    def test_discovery_refuses_ambiguous_inputs_and_ignores_excel_lock(self):
        (self.root / "~$locked.xlsx").touch()
        with self.assertRaises(ExportError):
            find_workbook(self.root)
        (self.root / "one.XLSX").touch()
        self.assertEqual(find_workbook(self.root).name, "one.XLSX")
        (self.root / "two.xlsx").touch()
        with self.assertRaises(ExportError):
            find_workbook(self.root)


if __name__ == "__main__":
    unittest.main()
