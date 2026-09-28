"""Package the open-code-review-delegate skill into a Bee marketplace plugin ZIP.

Bundle layout (what the Bee server expects):
    .codebuddy-plugin/plugin.json          <- manifest (required, exactly one)
    skills/open-code-review-delegate/SKILL.md
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL_DIR = ROOT / ".codebuddy" / "skills" / "open-code-review-delegate"
SKILL_MD = SKILL_DIR / "SKILL.md"
OUT_DIR = ROOT / "dist"
OUT_ZIP = OUT_DIR / "open-code-review-delegate.zip"

PLUGIN_NAME = "open-code-review-delegate"
VERSION = "1.0.0"

MANIFEST = {
    "name": PLUGIN_NAME,
    "version": VERSION,
    "description": (
        "Delegation mode for open-code-review (OCR). Instead of OCR calling an "
        "LLM endpoint, this skill instructs the host agent to perform the code "
        "review itself, using OCR only for deterministic engineering: file "
        "selection and rule resolution."
    ),
    "author": "alibaba",
    "repository": "https://github.com/alibaba/open-code-review",
    "license": "Apache-2.0",
    "category": "code-review",
    "keywords": ["code-review", "ocr", "delegation", "agent", "review"],
    "skills": ["./skills/open-code-review-delegate"],
}


def main() -> None:
    if not SKILL_MD.exists():
        raise SystemExit(f"Missing skill source: {SKILL_MD}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()

    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".codebuddy-plugin/plugin.json",
            json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n",
        )
        archive.write(SKILL_MD, "skills/open-code-review-delegate/SKILL.md")

    print(f"Wrote {OUT_ZIP}")
    with zipfile.ZipFile(OUT_ZIP) as archive:
        print("Contents:")
        for name in archive.namelist():
            print(f"  {name}")


if __name__ == "__main__":
    main()
