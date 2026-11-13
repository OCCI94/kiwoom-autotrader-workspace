"""Run the full isolated APP test suite for 2026-11-13 and archive provenance."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


STAGE = Path(__file__).resolve().parents[1]
WORKSPACE = STAGE.parents[1]
APP = STAGE / "outputs/ai-stock"
REPORTS_DIR = Path("/Users/tr-oh/Documents/Codex/reports/2026-11-13")

ADAPTER = r'''
def isolated_metadata(function):
    def checked(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = os.path.abspath(os.fsdecode(path))
            if name == '/Volumes' or name.startswith('/Volumes/'):
                raise FileNotFoundError(2, 'external volume outside test fixture')
            if any(name == root or name.startswith(root + '/') for root in (
                '/Users/tr-oh/Library/Application Support/KiwoomAutotrader',
                '/Users/tr-oh/Library/Keychains',
                '/Library/Keychains',
            )):
                raise PermissionError('protected filesystem metadata')
        return function(path, *args, **kwargs)
    return checked
os.stat = isolated_metadata(os.stat)
os.lstat = isolated_metadata(os.lstat)
'''


def main(modules: list[str]) -> int:
    # Clean any stray bytecode in candidate tree before computing manifest
    for pycache in list(APP.rglob("__pycache__")):
        shutil.rmtree(pycache, ignore_errors=True)
    for pyc in list(APP.rglob("*.pyc")):
        pyc.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="kw-maint-20261113-", dir="/private/tmp") as name:
        root = Path(name)
        shutil.copytree(APP, root / "outputs/ai-stock")
        for folder in ("home", "tmp", "work"):
            (root / folder).mkdir()
        controller = root / "work/isolated_app_tests.py"
        template = (WORKSPACE / "work/isolated_app_tests.py").read_text(encoding="utf-8")
        target = "(guard/'sitecustomize.py').write_text(GUARD)"
        assert template.count(target) == 1
        controller.write_text(
            template.replace(target, f"(guard/'sitecustomize.py').write_text(GUARD + {ADAPTER!r})"),
            encoding="utf-8",
        )
        env = {
            "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        run = subprocess.run(
            [sys.executable, "-B", str(controller), *modules],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        outputs = root / "outputs/ai-stock-research/reports"
        results = list(outputs.glob("*/*-provenance-*.json"))
        result_paths = [p for p in results if not p.name.endswith("-source-manifest.json")]
        if len(result_paths) != 1:
            print(run.stdout, run.stderr, end="")
            raise RuntimeError("isolated controller did not write one suite result")
        result_path = result_paths[0]
        result = json.loads(result_path.read_bytes())
        manifest_path = Path(result["source_manifest"]["path"])
        manifest_obj = json.loads(manifest_path.read_bytes())
        manifest_obj["source_root"] = str(APP)
        manifest_raw = (json.dumps(manifest_obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

        saved_verif = STAGE / "verification"
        saved_verif.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        verif_manifest = saved_verif / manifest_path.name
        report_manifest = REPORTS_DIR / manifest_path.name
        verif_manifest.write_bytes(manifest_raw)
        verif_manifest.chmod(0o600)
        report_manifest.write_bytes(manifest_raw)
        report_manifest.chmod(0o600)

        import hashlib
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        result["candidate_source_root"] = str(APP)
        result["source_manifest"] = {
            "path": str(report_manifest),
            "verification_copy_path": str(verif_manifest),
            "sha256": manifest_sha,
        }
        result["errors"] = sum(
            len(re.findall(r"^ERROR: ", item["error_tail"] or "", re.M))
            for item in result["results"]
        )
        result["controller_exit_code"] = run.returncode

        verif_result = saved_verif / result_path.name
        report_result = REPORTS_DIR / result_path.name
        payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        verif_result.write_bytes(payload)
        verif_result.chmod(0o600)
        report_result.write_bytes(payload)
        report_result.chmod(0o600)

        print(f"REPORT_RESULT={report_result}")
        print(f"REPORT_MANIFEST={report_manifest}")
        print(run.stdout, run.stderr, end="")
        return run.returncode or int(bool(result["failures"]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
