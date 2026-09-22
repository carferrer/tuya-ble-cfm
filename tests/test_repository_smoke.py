from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = ROOT / "custom_components" / "tuya_ble"
MANIFEST = INTEGRATION_DIR / "manifest.json"
HACS = ROOT / "hacs.json"
BRAND_ICON = INTEGRATION_DIR / "brand" / "icon.png"


def test_python_sources_parse() -> None:
    """All integration Python modules must be valid for the CI Python version."""
    failures: list[str] = []

    for path in sorted(INTEGRATION_DIR.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError) as err:
            failures.append(f"{path.relative_to(ROOT)}: {err}")

    assert not failures, "\n".join(failures)


def test_manifest_basics() -> None:
    """Keep the custom integration manifest structurally sane."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["domain"] == "tuya_ble"
    assert manifest["name"]
    assert manifest["version"]
    assert manifest["config_flow"] is True
    assert manifest["documentation"].startswith("https://")
    assert manifest["issue_tracker"].startswith("https://")
    assert "@carferrer" in manifest["codeowners"]
    assert isinstance(manifest.get("requirements"), list)


def test_hacs_repository_basics() -> None:
    """Check the repository files HACS expects for an integration."""
    hacs = json.loads(HACS.read_text(encoding="utf-8"))

    assert hacs["name"] == "Tuya BLE CFM"
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "tuya_ble.zip"
    assert (ROOT / "README.md").is_file()
    assert BRAND_ICON.is_file()
