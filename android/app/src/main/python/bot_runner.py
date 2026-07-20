"""
Entry point called by Chaquopy from MainActivity.
Sets up sys.path and preloads native libraries from the extracted
Termux package tree, then runs the bot's main.py in-process.
"""

import ctypes
import os
import runpy
import sys


def start(bot_dir: str, pkgs_dir: str) -> None:
    """
    Args:
        bot_dir:  Path to cfgs_and_internal (where main.py lives).
        pkgs_dir: Root of the extracted Termux packages (contains usr/).
    """
    _preload_native(pkgs_dir)
    _extend_path(bot_dir, pkgs_dir)
    os.chdir(bot_dir)
    runpy.run_path(os.path.join(bot_dir, "main.py"), run_name="__main__")


def _preload_native(pkgs_dir: str) -> None:
    """
    Eagerly load Termux shared libraries with RTLD_GLOBAL so that subsequent
    import statements for C-extension modules (numpy, cv2, torch…) can resolve
    their symbol dependencies without needing LD_LIBRARY_PATH to be set at
    process start time.
    """
    lib_dir = os.path.join(pkgs_dir, "usr", "lib")
    if not os.path.isdir(lib_dir):
        return

    # Update LD_LIBRARY_PATH for child processes and future dlopen calls.
    old_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{old_ld}" if old_ld else lib_dir

    # Load base runtime libraries first, in dependency order.
    priority = [
        "libandroid-support.so",
        "libc++_shared.so",
        "libgfortran.so.5",
        "libgfortran.so",
        "libopenblas.so",
        "liblapack.so",
    ]
    loaded: set = set()
    for name in priority:
        path = os.path.join(lib_dir, name)
        if os.path.isfile(path):
            _load(path)
            loaded.add(name)

    # Load every remaining .so globally so extension modules that link against
    # them (e.g. libopencv_*.so for cv2) can find their symbols.
    try:
        for entry in sorted(os.scandir(lib_dir), key=lambda e: e.name):
            if entry.name.endswith(".so") and entry.name not in loaded:
                _load(entry.path)
    except OSError:
        pass


def _load(path: str) -> None:
    try:
        ctypes.CDLL(path, ctypes.RTLD_GLOBAL)
    except OSError:
        pass


def _extend_path(bot_dir: str, pkgs_dir: str) -> None:
    """Add Termux site-packages and the bot directory to sys.path."""
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    site = os.path.join(pkgs_dir, "usr", "lib", f"python{py_ver}", "site-packages")
    for p in (site, bot_dir):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
