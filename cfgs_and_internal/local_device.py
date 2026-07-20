"""On-device stand-in for adbutils' AdbDevice, used when PylaAI runs ON the phone
(Android, via Shizuku). Everything runs as the local shell user and connects to the
scrcpy server through a direct abstract UNIX socket — so NO adbd / adb-over-TCP is
needed, which removes the whole 'No ADB devices' failure. Desktop keeps using adbutils.

It implements just the subset of the AdbDevice interface the bot + vendored scrcpy use:
shell(), create_connection(), sync.push(), app_current()/app_start()/app_stop(),
get_state(), and .serial.
"""
import os
import re
import shutil
import socket
import subprocess
import time

SH = "/system/bin/sh"


class _StreamProc:
    """Streamed-shell stand-in: used to launch the scrcpy server and read its startup
    output. Mirrors the tiny bit of AdbConnection the scrcpy client relies on."""
    def __init__(self, proc):
        self._proc = proc

    def read(self, n=-1):
        try:
            data = self._proc.stdout.read(n)
            return data if data is not None else b""
        except Exception:
            return b""

    def close(self):
        try:
            self._proc.terminate()
        except Exception:
            pass


class _Sync:
    def push(self, src, dst):
        shutil.copyfile(src, dst)
        try:
            os.chmod(dst, 0o644)
        except Exception:
            pass


class _AppInfo:
    def __init__(self, package):
        self.package = package or ""


class LocalDevice:
    serial = "local"

    def __init__(self):
        self.sync = _Sync()

    @staticmethod
    def _join(cmd):
        if isinstance(cmd, (list, tuple)):
            return " ".join(str(c) for c in cmd)
        return cmd

    @staticmethod
    def _clean_env():
        # Android system binaries (app_process for scrcpy/wm/input/am, etc.) must run with
        # the SYSTEM environment, NOT the bundled Python env — inheriting the pyla env's
        # LD_LIBRARY_PATH/PYTHONHOME makes them load the wrong libs and fail (e.g. the
        # scrcpy server silently dies and its socket never appears).
        env = dict(os.environ)
        for k in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "LD_PRELOAD", "PREFIX"):
            env.pop(k, None)
        env["PATH"] = "/system/bin:/system/xbin"
        return env

    def shell(self, cmd, stream=False, timeout=30, **kwargs):
        cmd = self._join(cmd)
        env = self._clean_env()
        if stream:
            proc = subprocess.Popen(
                [SH, "-c", cmd], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
            return _StreamProc(proc)
        try:
            r = subprocess.run([SH, "-c", cmd], env=env, capture_output=True, text=True, timeout=timeout)
            return r.stdout
        except Exception:
            return ""

    def create_connection(self, network=None, address="scrcpy", timeout=8.0):
        """Connect straight to the localabstract socket the scrcpy server opened.
        Retries because the server takes a moment to create it after launch."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                # Abstract socket: MUST be exact bytes with a leading NUL (a str address
                # gets NUL-padded to 108 bytes → wrong name → ECONNREFUSED).
                s.connect(b"\x00" + address.encode("utf-8"))
                s.settimeout(None)
                return s
            except Exception as e:
                last = e
                time.sleep(0.1)
        raise ConnectionError(f"local connect to abstract:{address} failed: {last}")

    def get_state(self):
        return "device"

    def app_start(self, package, **kwargs):
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def app_stop(self, package):
        self.shell(f"am force-stop {package}")

    def app_current(self):
        # The foreground package appears as "<pkg>/<activity>" in the ResumedActivity line
        # (its exact label varies by Android version: 'ResumedActivity:', 'mResumedActivity',
        # 'topResumedActivity'), with mCurrentFocus/mFocusedApp as a fallback.
        for cmd in (
            "dumpsys activity activities 2>/dev/null | grep -m1 ResumedActivity",
            "dumpsys window 2>/dev/null | grep -m1 -E 'mCurrentFocus|mFocusedApp'",
        ):
            out = self.shell(cmd) or ""
            m = re.search(r"([a-zA-Z][A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+", out)
            if m:
                return _AppInfo(m.group(1))
        return _AppInfo("")
