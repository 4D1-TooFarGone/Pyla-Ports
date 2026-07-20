"""
Downloads ARM64 .deb packages from Termux's APT repository and extracts
them into the app's private files directory so Chaquopy's Python can use
the compiled-for-Android native extensions (numpy, opencv, torch, etc.).

Call install(dest_dir) once on first launch; it writes a marker file so
subsequent calls are instant.
"""

import gzip
import io
import lzma
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from typing import Callable, Optional

MIRROR = "https://packages.termux.dev/apt/termux-main"
INDEX_URL = f"{MIRROR}/dists/stable/main/binary-aarch64/Packages.xz"

# Termux package names to fetch (plus all recursive dependencies).
# Pure-Python packages are handled by Chaquopy's pip at build time.
WANTED = [
    "python-numpy",
    "python-scipy",
    "python-opencv",
    "python-torch",
    "onnxruntime",
    "libopenblas",
    "libc++",
    "libandroid-support",
]

_TERMUX_FILES = "./data/data/com.termux/files"
_LOG_FILE: Optional[str] = None


# ── logging ────────────────────────────────────────────────────────────────────

def _log(msg: str, cb: Optional[Callable[[str], None]] = None) -> None:
    print(msg, flush=True)
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a") as f:
                f.write(msg + "\n")
        except OSError:
            pass
    if cb:
        cb(msg)


# ── package index ──────────────────────────────────────────────────────────────

def _fetch_index() -> dict:
    """Download and parse Termux's Packages.xz → {name: record}."""
    with urllib.request.urlopen(INDEX_URL, timeout=60) as f:
        raw = lzma.decompress(f.read())

    pkgs: dict = {}
    current: dict = {}
    for line in raw.decode("utf-8").splitlines():
        if line == "":
            if "Package" in current:
                pkgs[current["Package"]] = current
            current = {}
        elif ": " in line:
            k, _, v = line.partition(": ")
            current[k.strip()] = v.strip()
    if "Package" in current:
        pkgs[current["Package"]] = current
    return pkgs


def _dep_names(dep_str: str) -> list:
    """'libopenblas (>= 0.3), python | python3' → ['libopenblas', 'python']"""
    result = []
    for part in dep_str.split(","):
        name = part.strip().split("|")[0].strip().split("(")[0].strip()
        if name:
            result.append(name)
    return result


def _resolve(wanted: list, index: dict) -> list:
    """BFS: wanted packages + all recursive Depends, in install order."""
    seen: set = set()
    queue = list(wanted)
    order: list = []
    while queue:
        name = queue.pop(0)
        if name in seen or name not in index:
            continue
        seen.add(name)
        order.append(name)
        queue.extend(_dep_names(index[name].get("Depends", "")))
    return order


# ── deb extraction ─────────────────────────────────────────────────────────────

def _ar_entries(data: bytes):
    """Yield (name, payload) for each entry in an AR archive."""
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("Not a valid AR / .deb archive")
    pos = 8
    while pos + 60 <= len(data):
        hdr = data[pos: pos + 60]
        name = hdr[0:16].rstrip(b" ").decode("latin-1").rstrip("/")
        size = int(hdr[48:58].rstrip(b" "))
        pos += 60
        payload = data[pos: pos + size]
        pos += size + (size & 1)
        yield name, payload


def _decompress_zst(data: bytes) -> bytes:
    """Decompress zstd bytes; tries system unzstd (Android 12+) or zstandard."""
    # Try the pure-Python / CFFI zstandard package if installed.
    try:
        import zstandard  # noqa: F401
        dctx = zstandard.ZstdDecompressor()
        return dctx.decompress(data, max_output_size=2 * 1024 ** 3)
    except ImportError:
        pass

    # Fall back to the system unzstd binary (present on many Android 12+ ROMs).
    with tempfile.NamedTemporaryFile(suffix=".zst", delete=False) as f:
        f.write(data)
        inp = f.name
    out = inp[:-4]
    try:
        for cmd in ["unzstd", "/system/bin/unzstd"]:
            try:
                subprocess.run(
                    [cmd, "-d", inp, "-o", out, "--force", "-q"],
                    check=True, capture_output=True, timeout=300,
                )
                with open(out, "rb") as f:
                    return f.read()
            except (FileNotFoundError, subprocess.CalledProcessError, OSError):
                continue
        raise RuntimeError(
            "Cannot decompress .zst: neither 'zstandard' pip package "
            "nor system 'unzstd' is available on this device."
        )
    finally:
        for p in (inp, out):
            try:
                os.unlink(p)
            except OSError:
                pass


def _extract_deb(deb_data: bytes, dest: str) -> None:
    """
    Extract a .deb's data.tar.* into dest/, stripping the Termux prefix
    (./data/data/com.termux/files) so files land at dest/usr/...
    """
    for ar_name, payload in _ar_entries(deb_data):
        if not ar_name.startswith("data.tar"):
            continue

        if ar_name.endswith(".xz"):
            raw = lzma.decompress(payload)
        elif ar_name.endswith(".gz"):
            raw = gzip.decompress(payload)
        elif ar_name.endswith(".zst"):
            raw = _decompress_zst(payload)
        elif ar_name in ("data.tar", "data.tar."):
            raw = payload
        else:
            continue

        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                mname = member.name
                if mname.startswith(_TERMUX_FILES):
                    member.name = mname[len(_TERMUX_FILES):]
                member.name = member.name.lstrip("/")
                if not member.name or member.name == ".":
                    continue

                dest_path = os.path.join(dest, member.name)

                if member.isdir():
                    os.makedirs(dest_path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    try:
                        src = tar.extractfile(member)
                        if src:
                            with src, open(dest_path, "wb") as dst:
                                dst.write(src.read())
                            os.chmod(dest_path, 0o755 if member.mode & 0o111 else 0o644)
                    except OSError:
                        pass
                elif member.issym():
                    link_dest = os.path.join(os.path.dirname(dest_path), member.linkname)
                    try:
                        if os.path.lexists(dest_path):
                            os.unlink(dest_path)
                        os.symlink(member.linkname, dest_path)
                    except OSError:
                        pass
        return  # stop after the first data.tar entry found


# ── public API ─────────────────────────────────────────────────────────────────

def install(
    dest_dir: str,
    extra_packages: Optional[list] = None,
    log_file: Optional[str] = None,
    cb: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Download and extract all WANTED Termux packages into dest_dir.
    Skips if the marker file .installed already exists.

    Args:
        dest_dir:       Path to extract packages into (app filesDir sub-dir).
        extra_packages: Additional Termux package names beyond WANTED.
        log_file:       Optional path to append progress messages to.
        cb:             Optional Python callable called with each progress string.
    """
    global _LOG_FILE
    _LOG_FILE = log_file

    marker = os.path.join(dest_dir, ".installed")
    if os.path.exists(marker):
        _log("Native packages already installed, skipping.", cb)
        return

    os.makedirs(dest_dir, exist_ok=True)
    wanted = WANTED + (extra_packages or [])

    _log("Fetching Termux package index…", cb)
    index = _fetch_index()
    _log(f"Index loaded: {len(index)} packages available.", cb)

    to_install = _resolve(wanted, index)
    _log(f"Resolved {len(to_install)} packages (including dependencies).", cb)

    for pkg_name in to_install:
        if pkg_name not in index:
            _log(f"  [skip] {pkg_name} — not in index", cb)
            continue

        pkg = index[pkg_name]
        filename = pkg.get("Filename", "")
        if not filename:
            _log(f"  [skip] {pkg_name} — no Filename in index", cb)
            continue

        url = f"{MIRROR}/{filename}"
        size_kb = pkg.get("Installed-Size", "?")
        _log(f"  [{pkg_name}] downloading ({size_kb} KB installed)…", cb)

        try:
            with urllib.request.urlopen(url, timeout=120) as f:
                deb_data = f.read()
            _log(f"  [{pkg_name}] extracting…", cb)
            _extract_deb(deb_data, dest_dir)
        except Exception as e:
            _log(f"  [{pkg_name}] FAILED: {e}", cb)

    open(marker, "w").close()
    _log("All packages installed successfully.", cb)
