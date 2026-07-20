import atexit
import sys
import math
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import scrcpy
from adbutils import adb, AdbDevice
from debug_view import DebugViewPublisher
from local_device import LocalDevice
from utils import config_bool, load_toml_as_dict, save_dict_as_toml, is_android, invalidate_toml_cache

brawl_stars_width, brawl_stars_height = 1920, 1080

press_coords_dict = load_toml_as_dict("cfg/buttons_config.toml")
KNOWN_BS_PACKAGES = ("com.supercell.brawlstars", "bsd.suitcase.release")


def restart_adb_server() -> None:
    try:
        adb.server_kill()
    except Exception:
        pass
    time.sleep(0.5)
    try:
        adb.server_start()
    except Exception:
        pass
    time.sleep(0.5)


def online_devices():
    out = []
    for d in adb.device_list():
        try:
            state = d.get_state() if hasattr(d, "get_state") else d.state
        except Exception:
            state = "device"
        if state == "device":
            out.append(d)
    return out


def discover_device(verbose: bool = False):
    # On the phone we DON'T use adb at all: the bot runs as the shell user (via Shizuku),
    # so it controls the device locally and talks to the scrcpy server through a direct
    # abstract socket. This removes the whole "No ADB devices" failure (adbd not reachable
    # on a scannable TCP port) that hit real users. Desktop still uses the adb scan below.
    if is_android():
        if verbose:
            print("Android: using on-device local shell + scrcpy socket (no adb needed).")
        return LocalDevice()

    preferred_port = load_toml_as_dict("cfg/general_config.toml")["emulator_port"]
    candidates = [5137, 5555, 16384, 7555, 5635, 62001, 62025, 62026, 7556, 7565, 16416] + list(range(5556, 5566)) + list(range(5565, 5756, 10))

    def _safe_connect(port: int):
        dev = adb.connect(f"127.0.0.1:{port}")
        return dev

    def _try(port):
        try:
            _safe_connect(port)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        executor.map(_try, candidates)

    devices = online_devices()
    if verbose:
        print(f"Online devices after scan: {[d.serial for d in devices]}")

    if not devices:
        raise ConnectionError("No ADB devices came online after scan.")

    if preferred_port:
        pref = next((d for d in devices if d.serial.endswith(f"{preferred_port}")), None)
        if pref:
            if verbose and len(devices) > 1:
                print(f"Multiple devices online; using configured port {preferred_port} ({pref.serial})")
            return pref

    if len(devices) == 1:
        return devices[0]

    chosen = devices[0]
    print(f"Multiple ADB devices online and no port configured. "
          f"Picking {chosen.serial} (first one). Others: "
          f"{[d.serial for d in devices if d is not chosen]}")
    return chosen

def _make_scrcpy_client(device, max_ips):
    if is_android():
        fps = 15 if max_ips == "auto" else min(int(max_ips), 15)
        return scrcpy.Client(device=device, max_width=0, bitrate=2000000, max_fps=fps)
    if max_ips == "auto":
        return scrcpy.Client(device=device, max_width=0, bitrate=4000000)
    return scrcpy.Client(device=device, max_width=0, bitrate=4000000, max_fps=max_ips)


class WindowController:
    def __init__(self, max_ips="auto"):
        self._resolution_overridden = False
        self.scale_factor = None
        self.width = None
        self.height = None
        self.width_ratio = None
        self.height_ratio = None
        self.joystick_x, self.joystick_y = None, None
        self.BRAWL_STARS_PACKAGE = load_toml_as_dict("cfg/general_config.toml")["brawl_stars_package"]
        self.verbose_debug = config_bool(
            load_toml_as_dict("cfg/debug_settings.toml").get("verbose_debug"),
            False
        )

        print("Connecting to ADB (might take up to 2 minutes)...")
        self.device = discover_device(verbose=self.verbose_debug)
        print(f"Connected to device: {self.device.serial}")

        try:
            if not self.is_brawl_stars_running():
                print("Freeing cached background app RAM...")
                self.device.shell("am kill-all")
                time.sleep(1.0)
            else:
                print("Brawl already running — skipping RAM free to keep it alive (crash-recovery).")
        except Exception as e:
            print(f"am kill-all failed (non-fatal): {e}")

        try:
            self._force_game_resolution()
        except Exception as e:
            print(f"Could not set 16:9 game resolution (non-fatal): {e}")

        self.frame_lock = threading.Lock()
        self.max_ips = max_ips
        self.scrcpy_client = _make_scrcpy_client(self.device, self.max_ips)
        self.last_frame = None
        self.last_frame_time = 0.0
        self.last_joystick_pos = (None, None)
        self.FRAME_STALE_TIMEOUT = 15.0
        self.re_apply_movement = config_bool(
            load_toml_as_dict("cfg/debug_settings.toml").get("re_apply_movement"),
            True
        )
        self.debug_view = DebugViewPublisher.from_config()

        def on_frame(frame):
            if frame is not None:
                with self.frame_lock:
                    self.last_frame = frame
                    self.last_frame_time = time.time()

        self.scrcpy_client.add_listener(scrcpy.EVENT_FRAME, on_frame)
        self.scrcpy_client.start(threaded=True)
        atexit.register(self.close)
        print("Scrcpy client started successfully.")
        self.are_we_moving = False
        self.PID_JOYSTICK = 1
        self.PID_ATTACK = 2

    def _read_current_density(self):
        try:
            out = self.device.shell("wm density") or ""
        except Exception:
            return None
        dens = None
        for line in out.splitlines():
            line = line.strip()
            if "density:" in line:
                try:
                    val = int(line.split(":")[1].strip())
                except (ValueError, IndexError):
                    continue
                dens = val
                if line.lower().startswith("override"):
                    break
        return dens

    def _force_game_resolution(self, landscape_size="1920x1080", density=280):
        try:
            density = int(load_toml_as_dict("cfg/general_config.toml").get("game_density", density))
        except Exception:
            pass
        self._original_density = self._read_current_density()
        self._original_stayon = None
        try:
            self._original_stayon = (self.device.shell(
                "settings get global stay_on_while_plugged_in") or "").strip()
        except Exception:
            pass
        print(f"Display density before override: {self._original_density}")
        try:
            self.device.shell("svc power stayon true")
            self.device.shell("settings put global stay_on_while_plugged_in 7")
            self.device.shell("input keyevent 224")
            self.device.shell("wm dismiss-keyguard")
            self.device.shell(f"wm size {landscape_size}")
            self.device.shell(f"wm density {density}")
            time.sleep(1.5)
            self.device.shell("input keyevent 224")
            self.device.shell("wm dismiss-keyguard")
            self._resolution_overridden = True
            print(f"Forced display to {landscape_size} @ {density}dpi "
                  f"(16:9 for scrcpy; kept awake + keyguard dismissed to avoid lock).")
        except Exception as e:
            print(f"Could not force display size/density (non-fatal): {e}")

        self._fps_capped = False
        _off = ("off", "", "0", "none")
        try:
            cfg = load_toml_as_dict("cfg/general_config.toml")
            fps = str(cfg.get("limit_game_fps", "off")).lower()
            dscale = str(cfg.get("game_downscale", "off")).lower()
        except Exception:
            fps, dscale = "off", "off"
        game_args = []
        if fps not in _off:
            game_args.append(f"--fps {fps}")
        if dscale not in _off:
            game_args.append(f"--downscale {dscale}")
        if game_args:
            try:
                self.device.shell(f"cmd game set {' '.join(game_args)} {self.BRAWL_STARS_PACKAGE}")
                self._fps_capped = True
                print(f"Set Brawl GameManager intervention ({' '.join(game_args)}) "
                      f"(applies on the game's next launch; frees GPU for inference).")
            except Exception as e:
                print(f"Game intervention failed (non-fatal): {e}")

    def _reset_display(self):
        try:
            self.device.shell("wm size reset")
        except Exception:
            pass
        try:
            orig_density = getattr(self, "_original_density", None)
            if orig_density:
                self.device.shell(f"wm density {orig_density}")
            else:
                self.device.shell("wm density reset")
        except Exception:
            pass
        try:
            orig_stayon = getattr(self, "_original_stayon", None)
            if orig_stayon not in (None, ""):
                self.device.shell(f"settings put global stay_on_while_plugged_in {orig_stayon}")
            self.device.shell("svc power stayon false")
        except Exception:
            pass
        try:
            if getattr(self, "_fps_capped", False):
                self.device.shell(f"cmd game reset {self.BRAWL_STARS_PACKAGE}")
        except Exception:
            pass

    def get_latest_frame(self):
        with self.frame_lock:
            if self.last_frame is None:
                return None, 0.0
            return self.last_frame, self.last_frame_time

    def force_rediscover(self) -> bool:
        try:
            self.scrcpy_client.stop()
        except Exception:
            pass
        if not is_android():
            print("Restarting ADB server and re-discovering device.")
            restart_adb_server()
        else:
            print("Re-discovering device (keeping adb server — Android).")
        try:
            new_dev = discover_device(self.verbose_debug)
        except ConnectionError:
            return False
        self.device = new_dev
        print(f"Re-discovered device: {self.device.serial}")
        return True

    def reconnect_scrcpy(self, max_retries=3):
        for attempt in range(1, max_retries + 1):
            print(f"Scrcpy reconnect attempt {attempt}/{max_retries}")
            try:
                self.scrcpy_client.stop()
            except Exception:
                pass
            time.sleep(1)

            with self.frame_lock:
                self.last_frame = None
                self.last_frame_time = 0.0

            self.are_we_moving = False
            self.last_joystick_pos = (None, None)

            try:
                _ = self.device.get_state()
            except Exception:
                if not self.force_rediscover():
                    print("Device gone and re-discovery failed.")
                    time.sleep(2 * attempt)
                    continue

            def on_frame(frame):
                if frame is not None:
                    with self.frame_lock:
                        self.last_frame = frame
                        self.last_frame_time = time.time()

            try:
                self.scrcpy_client = _make_scrcpy_client(self.device, self.max_ips)
                self.scrcpy_client.add_listener(scrcpy.EVENT_FRAME, on_frame)
                self.scrcpy_client.start(threaded=True)
            except Exception as e:
                print(f"Scrcpy client creation failed: {e}")
                time.sleep(2 * attempt)
                continue

            deadline = time.time() + 8
            while time.time() < deadline:
                _, ft = self.get_latest_frame()
                if ft > 0 and (time.time() - ft) < 2:
                    print(f"Scrcpy feed restored on attempt {attempt}")
                    return True
                time.sleep(0.5)

            print(f"Attempt {attempt} did not restore frame feed")
            time.sleep(2 * attempt)

        print("All scrcpy reconnect attempts exhausted")
        return False

    def restart_brawl_stars(self):
        self.device.app_stop(self.BRAWL_STARS_PACKAGE)
        time.sleep(1)
        self.device.app_start(self.BRAWL_STARS_PACKAGE)
        time.sleep(3)
        print("Brawl stars restarted successfully.")

    def is_brawl_stars_running(self):
        try:
            opened_app = self.device.app_current().package.strip()
            detected_known_package = False
            for package in KNOWN_BS_PACKAGES:
                if opened_app == package:
                    detected_known_package = True
                    break
            if detected_known_package:
                if opened_app != self.BRAWL_STARS_PACKAGE:
                    general_config = load_toml_as_dict("cfg/general_config.toml")
                    general_config["brawl_stars_package"] = opened_app
                    save_dict_as_toml(general_config, "cfg/general_config.toml")
                    self.BRAWL_STARS_PACKAGE = opened_app
                    invalidate_toml_cache("cfg/general_config.toml")
                    print(f"Detected Brawl Stars running under the '{opened_app}' package. Updating configuration to match.")
            return opened_app == self.BRAWL_STARS_PACKAGE.strip()
        except Exception as e:
            print(f"Error checking if Brawl Stars is running: {e}")
            return False

    def screenshot(self):
        frame, frame_time = self.get_latest_frame()

        deadline = time.time() + 15
        while frame is None:
            if time.time() > deadline:
                raise ConnectionError(
                    "No frame received from scrcpy within 15s. "
                    "Check USB/emulator connection."
                )
            print("Waiting for first frame...")
            time.sleep(0.1)
            frame, frame_time = self.get_latest_frame()

        age = time.time() - frame_time
        if frame_time > 0 and age > self.FRAME_STALE_TIMEOUT:
            print(f"WARNING: scrcpy frame is {age:.1f}s stale -- feed may be frozen")

        if not self.width or not self.height:
            self.width = frame.shape[1]
            self.height = frame.shape[0]
            if (self.width, self.height) != (brawl_stars_width, brawl_stars_height):
                print(f"WARNING: Unexpected resolution: {self.width}x{self.height}. Expected {brawl_stars_width}x{brawl_stars_height}. Please set your emulator resolution to 1920x1080 for best results.")
            self.width_ratio = self.width / brawl_stars_width
            self.height_ratio = self.height / brawl_stars_height
            self.joystick_x, self.joystick_y = 220 * self.width_ratio, 870 * self.height_ratio
            self.scale_factor = min(self.width_ratio, self.height_ratio)
        return frame

    def touch_down(self, x, y, pointer_id=0):
        try:
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_DOWN, pointer_id)
        except Exception as e:
            print(f"Error during touch_down at ({x}, {y}) with pointer_id {pointer_id}: {e}")
            if self.reconnect_scrcpy() :
                try:
                    self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_DOWN, pointer_id)
                except Exception as e2:
                    print(f"Retry after reconnect failed during touch_down at ({x}, {y}) with pointer_id {pointer_id}: {e2}")

    def touch_move(self, x, y, pointer_id=0):
        try:
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_MOVE, pointer_id)
        except Exception as e:
            print(f"Error during touch_move at ({x}, {y}) with pointer_id {pointer_id}: {e}")
            if self.reconnect_scrcpy():
                try:
                    self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_MOVE, pointer_id)
                except Exception as e2:
                    print(f"Retry after reconnect failed during touch_move at ({x}, {y}) with pointer_id {pointer_id}: {e2}")

    def touch_up(self, x, y, pointer_id=0):
        try:
            self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_UP, pointer_id)
        except Exception as e:
            print(f"Error during touch_up at ({x}, {y}) with pointer_id {pointer_id}: {e}")
            if self.reconnect_scrcpy():
                try:
                    self.scrcpy_client.control.touch(int(x), int(y), scrcpy.ACTION_UP, pointer_id)
                except Exception as e2:
                    print(f"Retry after reconnect failed during touch_up at ({x}, {y}) with pointer_id {pointer_id}: {e2}")

    def move(self, x, y):
        target_x = self.joystick_x + x
        target_y = self.joystick_y + y
        if not self.are_we_moving:
            self.touch_down(self.joystick_x, self.joystick_y, pointer_id=self.PID_JOYSTICK)
            self.touch_move(target_x, target_y, pointer_id=self.PID_JOYSTICK)
            self.are_we_moving = True
            self.last_joystick_pos = (target_x, target_y)
            return

        if not self.re_apply_movement and self.last_joystick_pos == (target_x, target_y):
            return

        self.touch_move(target_x, target_y, pointer_id=self.PID_JOYSTICK)
        self.last_joystick_pos = (target_x, target_y)

    def release_movement(self):
        if self.are_we_moving:
            self.touch_up(self.joystick_x, self.joystick_y, pointer_id=self.PID_JOYSTICK)
            self.are_we_moving = False
            self.last_joystick_pos = (None, None)

    def click(self, x: int, y: int, delay=0.02, already_include_ratio=True, touch_up=True, touch_down=True):
        if not already_include_ratio:
            x = x * self.width_ratio
            y = y * self.height_ratio
        if touch_down: self.touch_down(x, y, pointer_id=self.PID_ATTACK)
        time.sleep(delay)
        if touch_up: self.touch_up(x, y, pointer_id=self.PID_ATTACK)

    def press(self, key, delay=0.02, touch_up=True, touch_down=True):
        if key not in press_coords_dict:
            return
        x, y = press_coords_dict[key]
        target_x = x * self.width_ratio
        target_y = y * self.height_ratio
        self.click(target_x, target_y, delay, touch_up=touch_up, touch_down=touch_down)

    def swipe(self, start_x, start_y, end_x, end_y, duration=0.2):
        dist_x = end_x - start_x
        dist_y = end_y - start_y
        distance = math.sqrt(dist_x ** 2 + dist_y ** 2)

        if distance == 0:
            return

        step_len = 25
        steps = max(int(distance / step_len), 1)
        step_delay = duration / steps

        self.touch_down(int(start_x), int(start_y), pointer_id=self.PID_ATTACK)
        for i in range(1, steps + 1):
            t = i / steps
            cx = start_x + dist_x * t
            cy = start_y + dist_y * t
            time.sleep(step_delay)
            self.touch_move(int(cx), int(cy), pointer_id=self.PID_ATTACK)
        self.touch_up(int(end_x), int(end_y), pointer_id=self.PID_ATTACK)

    def close(self):
        try:
            self.debug_view.close()
        except Exception as exc:
            print(f"Debug view close failed: {exc}")
        if getattr(self, "_resolution_overridden", False):
            self._reset_display()
            print("Reset display size + density to physical.")
        self.stop_scrcpy_with_timeout()

    def stop_scrcpy_with_timeout(self, timeout=2.0):
        def stop_client():
            try:
                self.scrcpy_client.stop()
            except Exception as exc:
                print(f"Scrcpy stop failed: {exc}")

        stop_thread = threading.Thread(target=stop_client, daemon=True, name="scrcpy-stop")
        stop_thread.start()
        stop_thread.join(timeout=timeout)
        if stop_thread.is_alive():
            print("Scrcpy stop is still running in the background; continuing shutdown.")
