#!/usr/bin/env python3
"""Install this checkout in the current user's Linux application menu."""

import os
from pathlib import Path
import shutil
import subprocess


def desktop_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def exec_argument(path: Path) -> str:
    # Exec has its own quoting layer after desktop-entry string unescaping.
    value = str(path).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return desktop_value('"' + value + '"')


def main():
    root = Path(__file__).resolve().parent.parent
    launcher = root / "run.sh"
    if not os.access(root / ".venv/bin/markdown-finder", os.X_OK):
        raise SystemExit("請先在專案目錄執行 uv sync --locked，再安裝應用程式入口。")
    if not os.access(launcher, os.X_OK):
        raise SystemExit(f"啟動腳本沒有執行權限：{launcher}")
    configured = os.environ.get("XDG_DATA_HOME", "")
    data_home = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/share"
    applications = data_home / "applications"
    icon = data_home / "icons/hicolor/scalable/apps/markdown-finder.svg"
    applications.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "src/markdown_finder/assets/markdown-finder.svg", icon)
    icon.chmod(0o644)
    entry = applications / "markdown-finder.desktop"
    entry.write_text(
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=Markdown Reader\n"
        "GenericName=Markdown Viewer\n"
        "GenericName[zh_TW]=Markdown 閱讀器\n"
        "Comment=Find and read local Markdown files in tabs\n"
        "Comment[zh_TW]=快速搜尋本機 Markdown 文件並在分頁閱讀\n"
        f"Exec={exec_argument(launcher)} %F\n"
        f"TryExec={desktop_value(str(launcher))}\n"
        f"Path={desktop_value(str(root))}\n"
        f"Icon={desktop_value(str(icon))}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "Categories=Office;Viewer;\n"
        "Keywords=markdown;markdown-finder;reader;viewer;notes;md;閱讀器;筆記;\n",
        encoding="utf-8",
    )
    entry.chmod(0o644)
    if shutil.which("desktop-file-validate"):
        subprocess.run(["desktop-file-validate", str(entry)], check=True)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications)], check=True)
    print(f"已安裝：{entry}")
    print("按 Win 鍵搜尋 markdown，即可開啟 Markdown Reader。")


if __name__ == "__main__":
    main()
