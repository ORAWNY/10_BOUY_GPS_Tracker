# build_hydraparse_msi.py
"""
Build a Windows MSI installer for HydraParse using cx_Freeze.

Usage (from project root):
    py build_hydraparse_msi.py
or:
    python build_hydraparse_msi.py bdist_msi
Result:
    dist/HydraParse-<version>-amd64.msi
"""

from __future__ import annotations
import os
import sys
from pathlib import Path
import importlib.util

def has_mod(name: str) -> bool:
    return importlib.util.find_spec(name) is not None

# Default to building an MSI when run directly
if len(sys.argv) == 1:
    sys.argv += ["bdist_msi"]

# -------------- project meta --------------
NAME = "HydraParse"
COMPANY = "Metocean"           # 👈 change if you want a different publisher name
VERSION = "0.1.0"              # 👈 bump per release
UPGRADE_CODE = "{E1C0B6A5-5D7B-4C2D-A6A1-1D9E7B6E9B3B}"  # 👈 keep this GUID STABLE across versions

root = Path(__file__).resolve().parent
main_script = root / "gui.py"
if not main_script.exists():
    raise FileNotFoundError(f"Cannot find entry point: {main_script}")

# Icon
icon_path = (root / "resource" / "icons" / "app_icon.ico")
if not icon_path.exists():
    icon_path = (root / "resource" / "icons" / "app_icon.png")  # optional fallback

# -------------- include files --------------
include_files: list[tuple[str, str]] = []

def add_path(rel_src: str, dest_rel: str | None = None):
    src = (root / rel_src).resolve()
    if src.exists():
        include_files.append((str(src), dest_rel or rel_src))
    else:
        print(f"[warn] missing asset path (skipped): {src}")

# Bundle your assets (mirrors the PyInstaller build)
add_path("resource", "resource")
add_path("resources", "resources")
add_path("ui", "ui")
add_path("utils", "utils")

# Matplotlib data (safe if missing)
try:
    import matplotlib
    include_files.append((matplotlib.get_data_path(), "mpl-data"))
except Exception:
    pass

# -------------- build options --------------
packages = ["PyQt6", "pandas", "matplotlib"]
includes = ["matplotlib.backends.backend_qtagg"]  # ensure QtAgg backend

if has_mod("pyproj"):
    packages.append("pyproj")
if has_mod("tzdata"):
    packages.append("tzdata")

build_exe_options = {
    "packages": packages,
    "includes": includes,
    "include_files": include_files,
    "include_msvcr": True,   # include MSVC runtime
    "optimize": 1,
}

# Shortcuts in MSI (Desktop + Start Menu)
shortcut_table = [
    # (Shortcut, Directory_, Name, Component_, Target, Arguments, Description, Hotkey, Icon, IconIndex, ShowCmd, WkDir)
    ("DesktopShortcut", "DesktopFolder", NAME, "TARGETDIR",
     f"[TARGETDIR]{NAME}.exe", None, NAME, None, None, None, None, "TARGETDIR"),
    ("StartMenuShortcut", "ProgramMenuFolder", NAME, "TARGETDIR",
     f"[TARGETDIR]{NAME}.exe", None, NAME, None, None, None, None, "TARGETDIR"),
]

bdist_msi_options = {
    "upgrade_code": UPGRADE_CODE,
    "add_to_path": False,
    "all_users": True,  # installs to Program Files
    "initial_target_dir": rf"[ProgramFilesFolder]\{COMPANY}\{NAME}",
    "data": {"Shortcut": shortcut_table},
}

# -------------- cx_Freeze setup --------------
from cx_Freeze import setup, Executable

base = "Win32GUI"  # GUI app (no console window)
exe = Executable(
    script=str(main_script),
    base=base,
    target_name=f"{NAME}.exe",
    icon=str(icon_path) if icon_path.exists() else None,
)

setup(
    name=NAME,
    version=VERSION,
    description="HydraParse",
    author=COMPANY,
    options={
        "build_exe": build_exe_options,
        "bdist_msi": bdist_msi_options,
    },
    executables=[exe],
)
