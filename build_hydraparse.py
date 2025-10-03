# build_hydraparse.py
"""
Build HydraParse (onedir) with PyInstaller.

Usage:
    py -m pip install pyinstaller
    py build_hydraparse.py
Result:
    dist/HydraParse/  -> folder you can zip and share
"""

from __future__ import annotations
import os
import sys
from pathlib import Path
import importlib.util

def has_mod(name: str) -> bool:
    return importlib.util.find_spec(name) is not None

def main():
    try:
        import PyInstaller.__main__ as pyi
    except Exception as e:
        print("PyInstaller is not installed. Install with:\n  pip install pyinstaller")
        raise

    root = Path(__file__).resolve().parent
    main_script = root / "gui.py"  # your entry point
    if not main_script.exists():
        raise FileNotFoundError(f"Cannot find {main_script}")

    # Data folders/files to include inside dist/HydraParse/
    # (Your code already uses _asset_path to look in 'resource' or 'resources')
    add_data = []

    def add_path(rel_src: str, dest_rel: str | None = None):
        src = (root / rel_src).resolve()
        if src.exists():
            dest = dest_rel or rel_src  # keep same relative name
            add_data.append(f"{src}{os.pathsep}{dest}")
        else:
            print(f"[warn] missing asset path (skipped): {src}")

    # Bundle the whole resource tree (icons/, splash/, style.qss, etc.)
    add_path("resource", "resource")
    # If you also keep assets under "resources", include it too (harmless if absent)
    add_path("resources", "resources")

    # If you have .ui files or other non-.py assets in these folders, include them:
    # (Safe to include even if they are only .py — PyInstaller ignores dupes)
    add_path("ui", "ui")
    add_path("utils", "utils")

    # Optional project defaults (databases, sample projects) — include only if you want them
    # add_path("Logger_Data", "Logger_Data")
    # add_path("test_projects", "test_projects")

    # Icon (Windows .ico preferred). If missing, PyInstaller will still build.
    icon_path = (root / "resource" / "icons" / "app_icon.ico")
    if not icon_path.exists():
        # fallback to png for non-Windows (PyInstaller accepts .ico best on Win)
        icon_path = (root / "resource" / "icons" / "app_icon.png")

    args = [
        "--noconfirm",
        "--clean",
        "--windowed",              # no console
        "--onedir",                # folder with exe + all deps
        f"--name=HydraParse_1.0",
        f"--paths={root}",         # ensure relative imports resolve (Create_tabs, utils, ui, etc.)
        f"--distpath={root / 'dist'}",
        f"--workpath={root / 'build'}",
    ]

    if icon_path.exists():
        args.append(f"--icon={icon_path}")

    # Robust dependency collection (handles plugins/backends/data files)
    args += [
        "--collect-all=PyQt6",
        "--collect-all=matplotlib",
        "--collect-submodules=matplotlib.backends",
        "--collect-all=pandas",
    ]

    # Collect pyproj (used by Distance viewer) if installed.
    if has_mod("pyproj"):
        args.append("--collect-all=pyproj")

    # Collect tzdata package when present (ensures zoneinfo works on Windows without system tzdb)
    if has_mod("tzdata"):
        args.append("--collect-data=tzdata")

    # Include win32com pieces if present (Outlook emailer). Usually auto-hooked, but this helps.
    if has_mod("win32com"):
        args += ["--collect-submodules=win32com"]

    # Add data folders/files
    for spec in add_data:
        args.append(f"--add-data={spec}")

    # Exclude obviously unneeded things if present (keeps build smaller; optional)
    # args += ["--exclude-module=tests", "--exclude-module=pytest", "--exclude-module=unittest"]

    # Finally, the script to build
    args.append(str(main_script))

    print("PyInstaller args:")
    for a in args:
        print(" ", a)

    # Run PyInstaller
    pyi.run(args)

    print("\n✅ Build complete.")
    print(f"→ Open: {root / 'dist' / 'HydraParse_1.0'}")

if __name__ == "__main__":
    main()
