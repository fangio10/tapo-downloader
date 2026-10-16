import nest_asyncio
import argparse
import contextvars
import fcntl
import inspect
import logging
import json
import psutil
import glob
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import aiohttp
import time
import ipaddress
from datetime import datetime, timedelta
import pytz
import sys
import os
import asyncio
from functools import wraps
from typing import Any, Dict, List, Optional, Callable, Tuple
from pytapo import Tapo
# ── Local patches ──────────────────────────────────────────────────────────────
# tapo_patches.py lives alongside this script and extends the installed pytapo
# library without modifying it.  Upgrading pytapo (pip install -U pytapo) will
# never overwrite these customisations.
#
# What the patches add / change vs the upstream library:
#   Convert           – generate_thumbnail() method (monkey-patched onto the class)
#   CustomDownloader  – predefined filename format, thumbnail calls after save,
#                       short-segment skip (< 3 s), overwriteFiles flag,
#                       direct timeCorrection usage
from tapo_patches import CustomDownloader as Downloader, Convert
# ──────────────────────────────────────────────────────────────────────────────
import urllib3
import ssl
import requests

# Most effective fix for Tapo cameras
urllib3.util.ssl_.DEFAULT_CIPHERS = 'ALL:@SECLEVEL=1'

nest_asyncio.apply()

""" Run modes (config key "mode", or --mode on the command line)

cron:    One-shot full scan of every camera, then exit. Run it from cron / a
         systemd timer. This is the original behaviour and the default.
sweep:   Long-running. Listens for camera detection events (tapo-events.py);
         each camera is downloaded `settle_seconds` after its last detection.
         A full scan of every camera also runs every `sweep_minutes` as a
         safety net, so no external scheduler is needed.
hybrid:  Long-running. Event-triggered downloads only, no internal sweep.
         Keep a cron job calling `tapo-downloader.py --once` as the catch-all.
watch:   Long-running, no ONVIF at all. Every `watch_seconds` each camera is checked for new
         recordings (today and yesterday) over a login that is kept open, with a full scan
         every `sweep_minutes`. A camera that is busy is skipped until the next check.

`watch_cameras` (sweep/hybrid): cameras whose ONVIF events do not work (cam3/cam4 on some models)
are watched like in `watch` mode, while the others still use ONVIF.

Targeted runs (what the listener executes; also handy by hand):
  tapo-downloader.py --cam cam1 --start 2026-10-15T03:20:00Z --end 2026-10-15T03:21:10Z
  tapo-downloader.py --cam cam1            # full scan of one camera
Exit code 75 means some clips were deferred (still recording, or the camera was
busy) and a later run should pick them up.
"""

""" Detection types (filename suffix)
Sourced from the camera's searchDetectionList alarm_type codes; not all
codes apply to every model/firmware. See DETECTION_TYPE_NAMES below.

motion:             Standard pixel-change motion detection.
person:             A person has been identified.
pet:                A pet (such as a dog or cat) has been spotted.
vehicle:            A car, truck, or moving vehicle was found.
baby_cry:           Audio detection picking up an infant crying.
bark / meow:        Audio detection of a dog barking / cat meowing.
line_crossing:      Something crossed a pre-configured electronic tripwire.
area_intrusion:     Something entered a defined detection zone.
tamper:             The camera lens was physically covered or blocked.
glass_break:        Audio detection of breaking glass.
smoke:              Smoke detected.
face:               A face was recognized.
loitering:          Prolonged presence detected in view.
package_delivered:  A package was placed in view.
package_picked_up:  A package was removed from view.

Unmapped codes fall back to "type_<n>" so new/unknown codes are still
visible in filenames rather than silently mislabeled.
"""

# Constants
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".tapo-downloader")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
COMPLETED_FOLDERS_FILE = os.path.join(CONFIG_DIR, "completed_folders.json")
FAILED_CLIPS_FILE = os.path.join(CONFIG_DIR, "failed_clips.json")
CLIP_RETRY_DELAYS_S = (600, 3600, 21600, 86400)   # wait after the 1st, 2nd, 3rd, 4th+ failed download of a clip
CLIP_FAILURE_TTL_S = 7 * 86400                    # forget a clip after this long (it has left the SD card by then)
THUMBNAILS_SUBDIR = "thumbnails"
DIR_PERMISSIONS = 0o777
VALID_MODES = ("cron", "sweep", "hybrid", "watch")
DAEMON_MODES = ("sweep", "hybrid", "watch")
EVENTS_SCRIPT_NAME = "tapo-events.py"
EXIT_TEMPFAIL = 75                 # EX_TEMPFAIL: some clips deferred, try again later
EVENT_WINDOW_MARGIN_S = 120        # widen an event window so clips near midnight are not missed
RUN_STATS = {"deferred": 0}        # clips skipped because still recording / camera busy
LOGIN_ERRORS: Dict[int, str] = {}  # camera number -> last login/connect error (cleared on success)
AUTH_ERROR_RE = re.compile(r"authentication|unauthori[sz]ed|password|credential", re.I)
SUSPENDED_RE = re.compile(r"try again in (\d+) seconds", re.I)
WATCH_AUTH_RETRY_S = 1800          # after a rejected login wait this long; failed logins add up to a lockout
WATCH_AUTH_MAX_FAILURES = 2        # then stop watching that camera until restarted
SESSION_MAX_AGE_S = 3600           # watch mode re-logs-in this often
DEFAULT_CONFIG = {
    "ip_addresses": ["192.168.1.100", "", "", ""],
    "username": "cloud_username",
    "password": "cloud_password",
    "download_directory": "/nas/tapo/",
    "max_days": 30,
    "timezone": "Australia/Sydney",
    "timeout_minutes": 55,
    "regenerate_thumbnails": False,
    "force_regenerate_thumbnails": False,
    "thumbnail_height": 100,
    "thumbnail_time_percentage": 0.1,
    "thumbnail_quality": 2,
    "max_retries": 1,
    "freshness_seconds": 0,  # User-defined override for timeCorrection
    "onvif_username": "",          # sweep/hybrid: camera account for ONVIF events (blank = use username/password)
    "onvif_password": "",
    "mode": "cron",                # cron | sweep | hybrid | watch
    "watch_cameras": [],           # e.g. ["cam3", "cam4"]: cameras watched by polling instead of ONVIF events
    "watch_seconds": 60,           # watch: seconds between checks
    "settle_seconds": 300,         # sweep/hybrid: quiet time after a detection before downloading
    "sweep_minutes": 60,           # sweep: interval of the full safety-net scan
    "hook_timeout_minutes": 60     # sweep/hybrid: kill a triggered download after this long
}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TapoDownloader")

def handle_exceptions(context: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {context}: {str(e)}")
                raise
        return wrapper
    return decorator

def _type_name(expected_type) -> str:
    if isinstance(expected_type, tuple):
        return " or ".join(t.__name__ for t in expected_type)
    return expected_type.__name__

def load_config() -> Dict:
    if not os.path.exists(CONFIG_FILE):
        logger.info("Config file not found at %s. Creating default config.", CONFIG_FILE)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logger.info("Default config created at %s. Please update it with your camera details.", CONFIG_FILE)
        except Exception as e:
            logger.error("Failed to create default config at %s: %s", CONFIG_FILE, str(e))
            sys.exit(1)

    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        required_fields = {
            "ip_addresses": list,
            "username": str,
            "password": str,
            "download_directory": str,
            "max_days": int,
            "timeout_minutes": (int, float),
            "thumbnail_height": int,
            "thumbnail_time_percentage": float,
            "thumbnail_quality": int,
            "max_retries": int,
            "freshness_seconds": int
        }
        for field, expected_type in required_fields.items():
            if field not in config:
                config[field] = DEFAULT_CONFIG[field]
                logger.info("Setting default '%s' to %s in %s.", field, config[field], CONFIG_FILE)
            if not isinstance(config[field], expected_type):
                logger.error("Field '%s' in %s must be of type %s.", field, CONFIG_FILE, _type_name(expected_type))
                sys.exit(1)

        optional_fields = {
            "regenerate_thumbnails": bool,
            "force_regenerate_thumbnails": bool,
            "timezone": str,
            "mode": str,
            "watch_cameras": list,
            "watch_seconds": (int, float),
            "onvif_username": str,
            "onvif_password": str,
            "settle_seconds": (int, float),
            "sweep_minutes": (int, float),
            "hook_timeout_minutes": (int, float)
        }
        for field, expected_type in optional_fields.items():
            if field not in config:
                config[field] = DEFAULT_CONFIG[field]
                logger.info("Setting default '%s' to %s in %s.", field, config[field], CONFIG_FILE)
            if not isinstance(config[field], expected_type):
                logger.error("Field '%s' in %s must be of type %s.", field, CONFIG_FILE, _type_name(expected_type))
                sys.exit(1)

        # Validate IP addresses
        if not config["ip_addresses"] or len(config["ip_addresses"]) > 4:
            logger.error("'ip_addresses' in %s must contain 1 to 4 IP addresses.", CONFIG_FILE)
            sys.exit(1)
        for ip in config["ip_addresses"]:
            if ip:
                try:
                    ipaddress.IPv4Address(ip)
                except ValueError:
                    logger.error("Invalid IP address '%s' in %s.", ip, CONFIG_FILE)
                    sys.exit(1)

        # Validate download directory
        if not os.path.isdir(config["download_directory"]) or not os.access(config["download_directory"], os.W_OK):
            logger.error("Download directory '%s' in %s is not a writable directory.", config["download_directory"], CONFIG_FILE)
            sys.exit(1)

        # Validate timeout
        if config["timeout_minutes"] <= 0:
            logger.error("'timeout_minutes' in %s must be a positive number.", CONFIG_FILE)
            sys.exit(1)

        # Validate timezone
        try:
            pytz.timezone(config.get("timezone", datetime.now().astimezone().tzinfo))
        except pytz.exceptions.UnknownTimeZoneError:
            logger.error("Invalid timezone '%s' in %s.", config["timezone"], CONFIG_FILE)
            sys.exit(1)

        # Validate thumbnail parameters
        if config["thumbnail_height"] <= 0:
            logger.error("'thumbnail_height' in %s must be a positive integer.", CONFIG_FILE)
            sys.exit(1)
        if not 0 <= config["thumbnail_time_percentage"] <= 1:
            logger.error("'thumbnail_time_percentage' in %s must be between 0 and 1.", CONFIG_FILE)
            sys.exit(1)
        if not 2 <= config["thumbnail_quality"] <= 31:
            logger.error("'thumbnail_quality' in %s must be an integer between 2 and 31.", CONFIG_FILE)
            sys.exit(1)
        if config["max_retries"] < 0:
            logger.error("'max_retries' in %s must be non-negative.", CONFIG_FILE)
            sys.exit(1)
        if config["force_regenerate_thumbnails"] and not config["regenerate_thumbnails"]:
            logger.warning("'force_regenerate_thumbnails' is True but 'regenerate_thumbnails' is False in %s. Setting 'regenerate_thumbnails' to True.", CONFIG_FILE)
            config["regenerate_thumbnails"] = True
        if config["freshness_seconds"] < 0:
            logger.error("'freshness_seconds' in %s must be non-negative.", CONFIG_FILE)
            sys.exit(1)

        # Validate run mode settings
        if config["mode"] not in VALID_MODES:
            logger.error("'mode' in %s must be one of: %s.", CONFIG_FILE, ", ".join(VALID_MODES))
            sys.exit(1)
        for cam in config["watch_cameras"]:
            if not (isinstance(cam, str) and re.fullmatch(r"cam[1-4]", cam.strip().lower())):
                logger.error("'watch_cameras' in %s must list names like cam1..cam4.", CONFIG_FILE)
                sys.exit(1)
        if config["watch_seconds"] < 10:
            logger.error("'watch_seconds' in %s must be at least 10.", CONFIG_FILE)
            sys.exit(1)
        if config["settle_seconds"] < 0:
            logger.error("'settle_seconds' in %s must be non-negative.", CONFIG_FILE)
            sys.exit(1)
        if config["sweep_minutes"] <= 0:
            logger.error("'sweep_minutes' in %s must be a positive number.", CONFIG_FILE)
            sys.exit(1)
        if config["hook_timeout_minutes"] <= 0:
            logger.error("'hook_timeout_minutes' in %s must be a positive number.", CONFIG_FILE)
            sys.exit(1)

        return config
    except FileNotFoundError:
        logger.error("%s not found.", CONFIG_FILE)
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in %s.", CONFIG_FILE)
        sys.exit(1)

def load_completed_folders() -> Dict:
    try:
        with open(COMPLETED_FOLDERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("Creating empty completed folders file at %s", COMPLETED_FOLDERS_FILE)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(COMPLETED_FOLDERS_FILE, "w") as f:
            json.dump({}, f, indent=2)
        return {}

def mark_folder_completed(completed_folders: Dict, folder_key: str, date: str) -> None:
    """Record a completed day-folder. Several processes (cron scan plus event-triggered
    runs) can write this file, so re-read it under a lock, merge, and replace it
    atomically instead of overwriting it with one process's in-memory copy."""
    entry = {"completed": True, "date": date}
    completed_folders[folder_key] = entry
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(COMPLETED_FOLDERS_FILE + ".lock", "a") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                with open(COMPLETED_FOLDERS_FILE, "r") as f:
                    on_disk = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                on_disk = {}
            on_disk[folder_key] = entry
            fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(on_disk, f, indent=2)
            os.replace(tmp_path, COMPLETED_FOLDERS_FILE)
            completed_folders.update(on_disk)
        logger.info("Updated completed folders file at %s", COMPLETED_FOLDERS_FILE)
    except Exception as e:
        logger.error("Failed to save completed folders to %s: %s", COMPLETED_FOLDERS_FILE, str(e))

def _locked_json_update(path: str, mutator: Callable[[Dict], None]) -> Dict:
    """Read-modify-write a small JSON state file under a lock, replacing it atomically."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(path + ".lock", "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        mutator(data)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        return data

def clip_retry_wait(key: str) -> float:
    """Seconds still to wait before this clip may be downloaded again (0 = go ahead)."""
    try:
        with open(FAILED_CLIPS_FILE, "r") as f:
            entry = json.load(f).get(key)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0.0
    if not entry:
        return 0.0
    delay = CLIP_RETRY_DELAYS_S[min(entry.get("count", 1), len(CLIP_RETRY_DELAYS_S)) - 1]
    return max(0.0, entry.get("last", 0) + delay - time.time())

def record_clip_failure(key: str) -> int:
    """Remember a failed download so it is not retried every cycle. Returns the failure count."""
    now = time.time()
    def mutate(data: Dict) -> None:
        for k in [k for k, v in data.items() if now - v.get("last", 0) > CLIP_FAILURE_TTL_S]:
            del data[k]
        data[key] = {"count": data.get(key, {}).get("count", 0) + 1, "last": now}
    try:
        return _locked_json_update(FAILED_CLIPS_FILE, mutate)[key]["count"]
    except Exception as e:
        logger.error("Failed to save %s: %s", FAILED_CLIPS_FILE, str(e))
        return 1

def clear_clip_failure(key: str) -> None:
    def mutate(data: Dict) -> None:
        data.pop(key, None)
    try:
        if os.path.exists(FAILED_CLIPS_FILE):
            _locked_json_update(FAILED_CLIPS_FILE, mutate)
    except Exception as e:
        logger.error("Failed to update %s: %s", FAILED_CLIPS_FILE, str(e))

# Load configuration
config = load_config()
ip_addresses = config["ip_addresses"]
username = config["username"]
password = config["password"]
base_download_dir = os.path.abspath(config["download_directory"])
max_days = config["max_days"]
timezone = pytz.timezone(config.get("timezone", str(datetime.now().astimezone().tzinfo)))
timeout_seconds = config["timeout_minutes"] * 60
regenerate_thumbnails = config["regenerate_thumbnails"]
force_regenerate_thumbnails = config["force_regenerate_thumbnails"]
thumbnail_height = config["thumbnail_height"]
thumbnail_time_percentage = config["thumbnail_time_percentage"]
thumbnail_quality = config["thumbnail_quality"]
max_retries = config["max_retries"]
freshness_seconds = config["freshness_seconds"]
run_mode = config["mode"]
watch_cameras = config["watch_cameras"]
watch_seconds = config["watch_seconds"]
onvif_username = config["onvif_username"] or username   # ONVIF listener: camera account
onvif_password = config["onvif_password"] or password   # (username/password stay the cloud login for downloads)
settle_seconds = config["settle_seconds"]
sweep_minutes = config["sweep_minutes"]
hook_timeout_minutes = config["hook_timeout_minutes"]

# Clips this process has already confirmed are on disk. The archive is append-only (nothing is deleted
# behind our back), so later watch cycles skip them without touching the filesystem; a restart or a
# one-shot run re-checks everything once.
CONFIRMED_CLIPS: set = set()                       # (camera ip, clip start time)
PREPARED_DIRS: set = set()                         # day folders already created / chmod'ed
TARGET_WINDOW: Optional[Tuple[int, int]] = None    # --start/--end of a targeted run, as epoch seconds
WINDOW_CLIP_MARGIN_S = 300                         # a clip counts as part of the window if within this of it
QUIET: contextvars.ContextVar = contextvars.ContextVar("quiet", default=False)        # watch cycles log routine lines at DEBUG
RUN_COUNTS: contextvars.ContextVar = contextvars.ContextVar("run_counts", default=None)  # per camera run: new / failed / ...

def routine(msg: str, *args) -> None:
    """INFO in one-shot runs; DEBUG in watch cycles, where the same lines would repeat every minute."""
    (logger.debug if QUIET.get() else logger.info)(msg, *args)

def count(kind: str, n: int = 1) -> None:
    counts = RUN_COUNTS.get()
    if counts is not None:
        counts[kind] = counts.get(kind, 0) + n

def clip_is_pending(ip_address: str, start: int, end: int) -> bool:
    """False for clips already confirmed on disk, or outside the event window of a targeted run."""
    if (ip_address, start) in CONFIRMED_CLIPS:
        return False
    if TARGET_WINDOW and (end < TARGET_WINDOW[0] - WINDOW_CLIP_MARGIN_S or start > TARGET_WINDOW[1] + WINDOW_CLIP_MARGIN_S):
        return False
    return True

def recording_pending(recording: Dict, ip_address: str) -> bool:
    for value in recording.values():
        try:
            start, end = int(value.get("startTime")), int(value.get("endTime"))
        except (TypeError, ValueError, AttributeError):
            return True   # unparseable: let process_recording report it
        if clip_is_pending(ip_address, start, end):
            return True
    return False

def check_disk_space(output_dir: str) -> None:
    try:
        disk = psutil.disk_usage(output_dir)
        free_mb = disk.free / (1024 * 1024)
        routine("Disk space: %d MB free in %s", free_mb, output_dir)
        if free_mb < 1000:
            logger.warning("Low disk space (<1GB) in %s.", output_dir)
    except Exception as e:
        logger.error("Failed to check disk space for %s: %s", output_dir, str(e))

async def tcall(fn, *args):
    """pytapo methods are synchronous in current releases and async in older ones. Call once and
    await the result only if it is awaitable (the old try/await/except TypeError/call-again
    pattern sent every request to the camera twice)."""
    # Run in a worker thread: a synchronous call that blocks on a slow camera must not freeze the
    # event loop, which also streams every other camera's downloads.
    result = await asyncio.to_thread(fn, *args)
    if inspect.isawaitable(result):
        result = await result
    return result

def build_video_path(recording_dir: str, filename: str) -> str:
    return os.path.join(recording_dir, filename)

def build_thumbnail_path(recording_dir: str, filename: str) -> str:
    return os.path.join(recording_dir, THUMBNAILS_SUBDIR, os.path.splitext(filename)[0] + ".jpg")

def check_existing_file(filepath: str) -> bool:
    return os.path.exists(filepath)

def find_existing_clip(recording_dir: str, filename: str) -> Optional[str]:
    """Return the path of an already-downloaded copy of this clip, or None.

    Filenames end in the detection type, which comes from the camera's event log.
    If that log hadn't caught up on an earlier run the clip may have been saved as
    '..._motion.mp4' and would now be named '..._person.mp4', so a clip also counts
    as present when any file in the folder starts with the same start timestamp."""
    exact = build_video_path(recording_dir, filename)
    if os.path.exists(exact):
        return exact
    m = re.match(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_", filename)
    if m:
        pattern = os.path.join(glob.escape(recording_dir), glob.escape(m.group(1)) + "_*.mp4")
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None

def dates_for_window(start_iso: str, end_iso: str) -> List[str]:
    """Local-date strings (YYYYMMDD, config timezone, newest first) covered by a UTC
    event window, widened slightly so a clip near midnight is not missed."""
    def parse(value: str) -> Optional[datetime]:
        if not value:
            return None
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)

    start, end = parse(start_iso), parse(end_iso)
    start = start or end
    end = end or start
    margin = timedelta(seconds=EVENT_WINDOW_MARGIN_S)
    today = datetime.now(timezone).date()
    last = min((end + margin).astimezone(timezone).date(), today)
    first = max((start - margin).astimezone(timezone).date(), last - timedelta(days=max_days - 1))
    dates = []
    day = last
    while day >= first:
        dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    return dates

async def acquire_camera_lock(camera_number: int, wait_seconds: float):
    """Exclusive per-camera lock shared by every process (cron scan, sweep, event
    triggers). Tapo cameras handle few concurrent sessions and two runs would also
    race on the same files. Returns an open file handle (close it to release) or
    None if the lock was not obtained within wait_seconds."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fh = open(os.path.join(CONFIG_DIR, f"cam{camera_number}.lock"), "a")
    deadline = time.time() + max(wait_seconds, 0)
    announced = False
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.time() >= deadline:
                fh.close()
                return None
            if not announced:
                logger.info("Camera %s: another run is using this camera, waiting for it to finish", camera_number)
                announced = True
            await asyncio.sleep(5)

@handle_exceptions("get_recording_days")
async def get_recording_days(tapo: Tapo, ip_address: str, output_dir: str, completed_folders: Dict,
                             day_cache: Optional[Dict[str, Any]] = None) -> List[str]:
    recording_days = []
    today = datetime.now(timezone).date()
    two_days_ago = today - timedelta(days=2)

    for days_back in range(max_days):
        check_date = today - timedelta(days=days_back)
        date_str = check_date.strftime("%Y%m%d")
        day_dir = os.path.join(output_dir, check_date.strftime("%Y-%m-%d"))
        folder_key = f"{ip_address}:{day_dir}"

        if folder_key in completed_folders and check_date < two_days_ago:
            logger.debug("Camera %s: Skipping completed folder %s", ip_address, day_dir)
            continue

        try:
            recordings = await tcall(tapo.getRecordings, date_str)
            if recordings:
                recording_days.append(date_str)
                if day_cache is not None:
                    day_cache[date_str] = recordings   # reused by download_recordings_for_day: one request per day, not two
                logger.debug("Camera %s: Found recordings for %s", ip_address, date_str)
        except Exception as e:
            logger.error("Camera %s: Error checking recordings for %s: %s", ip_address, date_str, str(e))

    return sorted(recording_days, reverse=True)

# Numeric alarm_type codes returned by the camera's searchDetectionList API.
# Sourced from community reverse-engineering (e.g. iobroker.tapo); not all
# codes apply to every model/firmware.
DETECTION_TYPE_NAMES = {
    2: "motion",
    3: "tamper",
    4: "line_crossing",
    5: "area_intrusion",
    6: "person",
    7: "baby_cry",
    8: "vehicle",
    9: "pet",
    11: "bark",
    12: "meow",
    13: "glass_break",
    14: "smoke",
    15: "package_delivered",
    16: "package_picked_up",
    20: "face",
    32: "loitering",
}


def find_event_type(events: List[Dict], rec_start: int, rec_end: int) -> str:
    """Match a recording's time range against fetched detection events to find
    what triggered it (e.g. motion vs person). Falls back to 'motion' if no
    overlapping event is found or the events list is empty."""
    for ev in events:
        ev_start = ev.get("start_time")
        ev_end = ev.get("end_time")
        if ev_start is None or ev_end is None:
            continue
        # Overlapping time ranges
        if ev_start <= rec_end and ev_end >= rec_start:
            raw_type = (
                ev.get("type")
                or ev.get("detect_type")
                or ev.get("alarm_type")
                or ev.get("event_type")
                or ev.get("class")
            )
            if raw_type is not None:
                # Numeric codes get mapped to a readable name; unknown codes
                # fall back to "type_<code>" so they're still distinguishable.
                try:
                    code = int(raw_type)
                    return DETECTION_TYPE_NAMES.get(code, f"type_{code}")
                except (ValueError, TypeError):
                    return str(raw_type).strip().lower().replace(" ", "_").replace("-", "_")
    return "motion"


async def process_recording(
    tapo: Tapo,
    recording: Dict,
    date: str,
    time_correction: int,
    output_dir: str,
    ip_address: str,
    start_time: float,
    idx: int,
    day_events: Optional[List[Dict]] = None
) -> bool:
    logger.debug("Camera %s: Processing recording %d for date %s", ip_address, idx + 1, date)
    
    for key, value in recording.items():
        start_time_rec = value.get("startTime")
        end_time = value.get("endTime")
        if not start_time_rec or not end_time:
            logger.warning("Camera %s: Invalid start_time or end_time for recording %s on %s", ip_address, key, date)
            continue

        try:
            start_time_rec = int(start_time_rec)
            end_time = int(end_time)
        except (ValueError, TypeError) as e:
            logger.error("Camera %s: Invalid timestamp format for recording %s on %s: %s", ip_address, key, date, str(e))
            continue

        if end_time <= start_time_rec:
            logger.warning("Camera %s: Invalid duration for recording %s on %s (start=%s, end=%s)", 
                           ip_address, key, date, start_time_rec, end_time)
            continue

        if not clip_is_pending(ip_address, start_time_rec, end_time):
            continue
        clip_id = (ip_address, start_time_rec)

        # ── EXTRACT EVENT TYPE ──────────────────────────────────────────────────
        # NOTE: getRecordings() (searchVideoOfDay) does NOT return alarm_type/type
        # fields - that data only exists in the camera's detection-event API.
        # We match this recording's time range against day_events (fetched once
        # per day via tapo.getEvents()) to find what actually triggered it.
        event_suffix = find_event_type(day_events or [], start_time_rec, end_time)
        # ────────────────────────────────────────────────────────────────────────

        logger.debug("Camera %s: Processing recording: start=%s, end=%s, event=%s", ip_address, start_time_rec, end_time, event_suffix)
        try:
            utc_dt = datetime.fromtimestamp(start_time_rec, pytz.UTC)
            local_dt = utc_dt.astimezone(timezone)
            day_dir_name = local_dt.strftime("%Y-%m-%d")
            duration_seconds = end_time - start_time_rec
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            duration_str = f"{minutes}m-{seconds}s"
            
            # Appended event_suffix to the end of the filename before extension
            filename = f"{local_dt.strftime('%Y-%m-%d_%H-%M-%S')}_{duration_str}_{event_suffix}.mp4"
            logger.debug("Camera %s: Generated filename %s for timestamp %s", ip_address, filename, start_time_rec)
        except (ValueError, TypeError) as e:
            logger.error("Camera %s: Failed to process timestamp %s: %s", ip_address, start_time_rec, str(e))
            day_dir_name = "unknown"
            filename = f"recording_{idx+1}_{event_suffix}.mp4"

        recording_dir = os.path.join(output_dir, day_dir_name)

        video_path = build_video_path(recording_dir, filename)
        existing_clip = find_existing_clip(recording_dir, filename)
        if existing_clip:
            video_path = existing_clip
        thumbnail_path = build_thumbnail_path(recording_dir, os.path.basename(video_path))

        if existing_clip and not config.get("overwrite_files", False):
            logger.debug("Camera %s: Skipping download: file %s already exists", ip_address, video_path)
            CONFIRMED_CLIPS.add(clip_id)
            if regenerate_thumbnails and (force_regenerate_thumbnails or not os.path.exists(thumbnail_path)):
                logger.info("Camera %s: Generating thumbnail for existing file %s at %s", ip_address, video_path, thumbnail_path)
                convert = Convert()
                try:
                    await convert.generate_thumbnail(
                        video_path,
                        thumbnail_path,
                        height=thumbnail_height,
                        time_percentage=thumbnail_time_percentage,
                        quality=thumbnail_quality,
                        max_retries=max_retries
                    )
                    logger.info("Camera %s: Successfully generated thumbnail at %s", ip_address, thumbnail_path)
                except Exception as e:
                    logger.error("Camera %s: Failed to generate thumbnail for %s: %s", ip_address, video_path, str(e))
            continue

        video_path = build_video_path(recording_dir, filename)
        clip_key = f"{ip_address}:{filename}"
        wait_s = clip_retry_wait(clip_key)
        if wait_s > 0:
            logger.debug("Camera %s: Not retrying %s for another %d min (earlier download failed)",
                         ip_address, filename, int(wait_s // 60) + 1)
            continue
        if recording_dir not in PREPARED_DIRS:   # once per day folder, not once per clip
            os.makedirs(recording_dir, exist_ok=True)
            try:
                os.chmod(recording_dir, DIR_PERMISSIONS)
            except Exception as e:
                logger.error("Camera %s: Failed to set permissions for %s: %s", ip_address, recording_dir, str(e))
            PREPARED_DIRS.add(recording_dir)
        logger.info("Camera %s: Initiating download for recording %s to %s", ip_address, key, video_path)
        last_status = None
        try:
            effective_time_correction = freshness_seconds if freshness_seconds > 0 else time_correction
            downloader = Downloader(
                tapo,
                start_time_rec,
                end_time,
                effective_time_correction,
                outputDirectory=recording_dir + os.sep,
                fileName=filename,
                max_retries=max_retries,
                thumbnail_height=thumbnail_height,
                thumbnail_time_percentage=thumbnail_time_percentage,
                thumbnail_quality=thumbnail_quality,
                force_regenerate_thumbnails=force_regenerate_thumbnails,
                overwriteFiles=config.get("overwrite_files", False)
            )
            last_logged = None
            async for status in downloader.download():
                last_status = status
                action = status["currentAction"]
                if action == "Recording in progress":
                    RUN_STATS["deferred"] += 1
                    count("deferred")
                # Log only when something changed, in whole seconds: one line per second of video at most.
                progress, total = int(status["progress"]), int(status["total"])
                if (action, progress) != last_logged:
                    last_logged = (action, progress)
                    name = os.path.basename(status.get("fileName") or filename)
                    if total:
                        logger.info("Camera %s: %s %s: %d / %d s", ip_address, action, name, progress, total)
                    else:
                        logger.info("Camera %s: %s %s", ip_address, action, name)

            actual_file = last_status.get('fileName') if last_status else None
            if last_status and last_status["currentAction"] == "Giving up":
                failures = record_clip_failure(clip_key)
                count("failed")
                logger.warning("Camera %s: Download of %s failed (attempt %d); will not retry for %d min",
                               ip_address, filename, failures,
                               CLIP_RETRY_DELAYS_S[min(failures, len(CLIP_RETRY_DELAYS_S)) - 1] // 60)
            elif actual_file and os.path.exists(actual_file):
                clear_clip_failure(clip_key)
                CONFIRMED_CLIPS.add(clip_id)
                count("new")
                logger.info("Camera %s: Finished downloading to %s", ip_address, actual_file)
                thumbnail_file = build_thumbnail_path(recording_dir, filename)
                if os.path.exists(thumbnail_file):
                    logger.debug("Camera %s: Thumbnail created at %s", ip_address, thumbnail_file)
                else:
                    logger.warning("Camera %s: Thumbnail not found at %s", ip_address, thumbnail_file)
            else:
                logger.warning("Camera %s: Expected file %s not found after download", ip_address, actual_file)
        except Exception as e:
            logger.error("Camera %s: Download failed for %s: %s", ip_address, key, str(e))
            record_clip_failure(clip_key)
            count("failed")
            actual_file = last_status.get('fileName') if last_status else None
            if actual_file and os.path.exists(actual_file):
                os.remove(actual_file)
                logger.info("Camera %s: Removed partial file %s", ip_address, actual_file)
    return False


@handle_exceptions("get_day_events")
async def get_day_events(tapo: Tapo, date: str, ip_address: str) -> List[Dict]:
    """Fetch detection events (motion/person/etc) for an entire day so they
    can be matched against recording segments by time overlap."""
    try:
        day = datetime.strptime(date, "%Y%m%d")
        local_start = timezone.localize(day)
        local_end = timezone.localize(day + timedelta(hours=23, minutes=59, seconds=59))
        start_ts = int(local_start.astimezone(pytz.UTC).timestamp())
        end_ts = int(local_end.astimezone(pytz.UTC).timestamp())

        events = await tcall(tapo.getEvents, start_ts, end_ts)

        if events:
            logger.debug("Camera %s: Sample raw event dict for %s: %s", ip_address, date, events[0])
        routine("Camera %s: Fetched %d detection events for %s", ip_address, len(events) if events else 0, date)
        return events or []
    except Exception as e:
        logger.error("Camera %s: Failed to fetch detection events for %s: %s", ip_address, date, str(e))
        return []


@handle_exceptions("download_recordings_for_day")
async def download_recordings_for_day(
    tapo: Tapo,
    date: str,
    time_correction: int,
    output_dir: str,
    ip_address: str,
    start_time: float,
    completed_folders: Dict,
    recordings: Optional[List] = None
) -> bool:
    try:
        folder_date = datetime.strptime(date, "%Y%m%d").date()
        day_dir = os.path.join(output_dir, folder_date.strftime("%Y-%m-%d"))
        folder_key = f"{ip_address}:{day_dir}"
        two_days_ago = (datetime.now(timezone) - timedelta(days=2)).date()
        current_date = datetime.now(timezone).date()

        if folder_date > current_date:
            logger.warning("Camera %s: Skipping future date %s", ip_address, date)
            return False

        if folder_key in completed_folders and folder_date < two_days_ago:
            logger.debug("Camera %s: Skipping completed folder %s", ip_address, day_dir)
            return False

        if recordings is None:   # a full scan already fetched this day while probing for days with recordings
            logger.debug("Camera %s: Getting recordings for %s", ip_address, date)
            recordings = await tcall(tapo.getRecordings, date)

        if not recordings:
            routine("Camera %s: No recordings found for %s", ip_address, date)
            return False

        # If recordings data returns directly as a dictionary instead of a list of chunks, format it into a loopable list
        if isinstance(recordings, dict):
            recordings = [recordings]

        pending = [(idx, rec) for idx, rec in enumerate(recordings) if recording_pending(rec, ip_address)]
        count("seen", len(recordings))
        routine("Camera %s: %d recordings for %s, %d to check", ip_address, len(recordings), date, len(pending))
        # The event log is only needed to name clips we have not seen yet.
        day_events = await get_day_events(tapo, date, ip_address) if pending else []
        for idx, recording in pending:
            elapsed_seconds = time.time() - start_time
            if elapsed_seconds > timeout_seconds:
                logger.info("Camera %s: Timeout reached (%d minutes). Aborting download.",
                            ip_address, config["timeout_minutes"])
                return True

            logger.debug("Camera %s: Processing recording %d: %s", ip_address, idx + 1, recording)
            try:
                await process_recording(tapo, recording, date, time_correction, output_dir, ip_address, start_time, idx, day_events)
            except Exception as e:
                logger.error("Camera %s: Failed to process recording %d for %s: %s", ip_address, idx + 1, date, str(e))

        if folder_date < two_days_ago and TARGET_WINDOW is None:   # a windowed run did not look at the whole day
            mark_folder_completed(completed_folders, folder_key, date)
            logger.info("Camera %s: Marked folder %s as completed", ip_address, day_dir)

        return False

    except ValueError as e:
        logger.error("Camera %s: Invalid date format for %s: %s", ip_address, date, str(e))
        return False


@handle_exceptions("regenerate_missing_thumbnails")
async def regenerate_missing_thumbnails(output_dir: str, camera_number: int, video_files: List[str]) -> int:
    logger.info("Camera %s: Checking for videos %s thumbnails in %s",
                camera_number, "forcing regeneration of" if force_regenerate_thumbnails else "missing", output_dir)
    convert = Convert()
    regenerated_count = 0

    for video_path in video_files:
        thumbnail_path = build_thumbnail_path(os.path.dirname(video_path), os.path.basename(video_path))
        thumbnail_dir = os.path.dirname(thumbnail_path)
        os.makedirs(thumbnail_dir, exist_ok=True)
        try:
            os.chmod(thumbnail_dir, DIR_PERMISSIONS)
        except Exception as e:
            logger.error("Camera %s: Failed to set permissions for %s: %s", camera_number, thumbnail_dir, str(e))

        if force_regenerate_thumbnails or not os.path.exists(thumbnail_path):
            action = "Regenerating" if os.path.exists(thumbnail_path) else "Generating"
            logger.info("Camera %s: %s thumbnail for %s at %s with height=%d, time=%.0f%%, quality=%d",
                        camera_number, action, video_path, thumbnail_path,
                        thumbnail_height, thumbnail_time_percentage * 100, thumbnail_quality)
            try:
                await convert.generate_thumbnail(
                    video_path,
                    thumbnail_path,
                    height=thumbnail_height,
                    time_percentage=thumbnail_time_percentage,
                    quality=thumbnail_quality,
                    max_retries=max_retries
                )
                regenerated_count += 1
                logger.info("Camera %s: Successfully generated thumbnail at %s", camera_number, thumbnail_path)
            except Exception as e:
                logger.error("Camera %s: Failed to generate thumbnail for %s: %s", camera_number, video_path, str(e))
        else:
            logger.debug("Camera %s: Thumbnail already exists for %s at %s", camera_number, video_path, thumbnail_path)

    logger.info("Camera %s: Regenerated %d thumbnails", camera_number, regenerated_count)
    return regenerated_count

async def _process_camera(ip_address: str, camera_number: int, base_download_dir: str, username: str, password: str,
                          start_time: float, dates: Optional[List[str]] = None,
                          session: Optional[Dict] = None) -> bool:
    QUIET.set(session is not None)   # watch cycles repeat every minute: keep routine lines at DEBUG
    RUN_COUNTS.set({})
    output_dir = os.path.join(base_download_dir, f"cam{camera_number}")
    os.makedirs(output_dir, exist_ok=True)
    try:
        os.chmod(output_dir, DIR_PERMISSIONS)
    except Exception as e:
        logger.error("Camera %s: Failed to set permissions for %s: %s", camera_number, output_dir, str(e))

    completed_folders = load_completed_folders()
    check_disk_space(output_dir)
    tapo = session.get("tapo") if session else None
    if tapo is not None and time.time() - session["created"] > SESSION_MAX_AGE_S:
        tapo = None   # refresh the login now and then
    if tapo is not None:
        time_correction = session["time_correction"]
    else:
        logger.info("Camera %s: Connecting to %s", camera_number, ip_address)
        try:
            tapo = Tapo(
                host=ip_address,
                user=username,
                password=password,
                cloudPassword=password
            )
        except Exception as e:
            logger.error("Camera %s: Failed to initialize Tapo client for %s: %s", camera_number, ip_address, str(e))
            LOGIN_ERRORS[camera_number] = str(e)
            return False

        logger.debug("Camera %s: Authenticating and fetching camera info", camera_number)
        try:
            basic_info = await tcall(tapo.getBasicInfo)
            info = basic_info.get("device_info", {}).get("basic_info", {}) if isinstance(basic_info, dict) else {}
            logger.info("Camera %s: Logged in (%s, firmware %s)", camera_number,
                        info.get("device_model", "unknown model"), info.get("sw_version", "unknown"))
            logger.debug("Camera %s: Camera info: %s", camera_number, basic_info)
        except Exception as e:
            logger.error("Camera %s: Authentication failed for %s: %s", camera_number, ip_address, str(e))
            LOGIN_ERRORS[camera_number] = str(e)
            return False

        logger.debug("Camera %s: Getting time correction", camera_number)
        try:
            time_correction = await tcall(tapo.getTimeCorrection)
        except Exception as e:
            logger.error("Camera %s: Failed to get time correction for %s: %s", camera_number, ip_address, str(e))
            time_correction = 0
        logger.debug("Camera %s: Time correction: %s", camera_number, time_correction)
        if session is not None:
            session.update(tapo=tapo, time_correction=time_correction, created=time.time())
    LOGIN_ERRORS.pop(camera_number, None)

    day_cache: Dict[str, Any] = {}
    if dates is not None:
        # Targeted run (event window or watch cycle): only the day(s) that matter, no 30-day probe.
        recording_days = dates
        routine("Camera %s: Targeted run for dates %s", camera_number, recording_days)
    else:
        routine("Camera %s: Fetching days with recordings (up to %d days)", camera_number, max_days)
        recording_days = await get_recording_days(tapo, ip_address, output_dir, completed_folders, day_cache)
    if not recording_days:
        routine("Camera %s: No days with recordings found", camera_number)
    else:
        routine("Camera %s: Found recordings for %d days: %s", camera_number, len(recording_days), recording_days)

        for date in recording_days:
            should_exit = await download_recordings_for_day(tapo, date, time_correction, output_dir, ip_address, start_time,
                                                            completed_folders, day_cache.get(date))
            if should_exit:
                logger.info("Camera %s: Exiting due to timeout", camera_number)
                return True

    # Walking the whole archive tree is slow on a NAS, so event-triggered runs skip it.
    if dates is None:
        video_files = glob.glob(os.path.join(output_dir, "**", "*.mp4"), recursive=True)
        routine("Camera %s: Final files in %s: %d files", camera_number, output_dir, len(video_files))

        if regenerate_thumbnails:
            await regenerate_missing_thumbnails(output_dir, camera_number, video_files)

    return False

@handle_exceptions("process_camera")
async def process_camera(ip_address: str, camera_number: int, base_download_dir: str, username: str, password: str,
                         start_time: float, dates: Optional[List[str]] = None,
                         session: Optional[Dict] = None, lock_wait: Optional[float] = None) -> Optional[bool]:
    """Returns True if the run hit its time limit, False if it finished, None if the camera was busy."""
    remaining = max(timeout_seconds - (time.time() - start_time), 0) if lock_wait is None else lock_wait
    lock = await acquire_camera_lock(camera_number, remaining)
    if lock is None:
        logger.warning("Camera %s: Camera busy with another run%s; skipping this run", camera_number,
                       " after waiting" if remaining else "")
        RUN_STATS["deferred"] += 1
        return None
    try:
        result = await _process_camera(ip_address, camera_number, base_download_dir, username, password, start_time, dates, session)
    finally:
        lock.close()
    counts = RUN_COUNTS.get() or {}
    news = counts.get("new", 0) or counts.get("failed", 0) or counts.get("deferred", 0)
    (logger.info if news or session is None else logger.debug)(
        "Camera %s: %d new, %d failed, %d deferred (%d clips checked)", camera_number,
        counts.get("new", 0), counts.get("failed", 0), counts.get("deferred", 0), counts.get("seen", 0))
    return result

async def main(only_cam: Optional[int] = None, dates: Optional[List[str]] = None):
    start_time = time.time()
    async with aiohttp.ClientSession() as session:
        tasks = []
        task_indices = []
        logger.info("Processing IP addresses: %s", ip_addresses)
        for idx, ip_address in enumerate(ip_addresses):
            camera_number = idx + 1
            if only_cam is not None and camera_number != only_cam:
                continue
            if not ip_address:
                logger.info("Skipping empty IP at position %d (would be cam%d)", idx, idx + 1)
                continue
            try:
                ipaddress.IPv4Address(ip_address)
            except ValueError:
                logger.info("Skipping invalid IP %s at position %d (would be cam%d)", ip_address, idx, idx + 1)
                continue
            tasks.append(process_camera(ip_address, camera_number, base_download_dir, username, password, start_time, dates))
            task_indices.append(idx)

        if not tasks:
            logger.error("No valid IP addresses to process.")
            return

        logger.info("Starting concurrent processing for %d cameras: %s", len(tasks), [ip_addresses[i] for i in task_indices])
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for task_idx, (idx, ip_address, result) in enumerate([(i, ip_addresses[i], res) for i, res in zip(task_indices, results)]):
            camera_number = idx + 1
            if isinstance(result, Exception):
                logger.error("Camera %d (%s): Failed with exception: %s", camera_number, ip_address, str(result))
            elif result:
                logger.info("Camera %d (%s): Exited due to timeout", camera_number, ip_address)
            else:
                logger.info("Camera %d (%s): Finished processing", camera_number, ip_address)

    elapsed_minutes = (time.time() - start_time) / 60
    logger.info("Script completed in %.2f minutes.", elapsed_minutes)


# ── Daemon modes (sweep / hybrid / watch) ──────────────────────────────────────

def recent_dates() -> List[str]:
    """Today and yesterday in the config timezone (what a new recording can belong to)."""
    today = datetime.now(timezone).date()
    return [(today - timedelta(days=d)).strftime("%Y%m%d") for d in range(min(2, max_days))]


async def watch_camera(ip_address: str, camera_number: int, stop: threading.Event, full_scans: bool) -> None:
    """Check one camera for new recordings every `watch_seconds` over a login that stays open.
    Rejected logins back off for a long time (and eventually stop) so the camera is not locked out."""
    session: Dict = {}
    last_full = time.time()
    auth_failures = 0
    logger.info("Camera %s: watching %s every %ds%s", camera_number, ip_address, watch_seconds,
                f", full scan every {sweep_minutes} min" if full_scans else "")
    while not stop.is_set():
        full = full_scans and time.time() - last_full >= sweep_minutes * 60
        try:
            result = await process_camera(ip_address, camera_number, base_download_dir, username, password,
                                          time.time(), None if full else recent_dates(),
                                          session=session, lock_wait=0)
            if full and result is not None:
                last_full = time.time()
        except Exception as e:
            logger.error("Camera %s: Watch cycle failed: %s", camera_number, e)
        wait = float(watch_seconds)
        error = LOGIN_ERRORS.get(camera_number)
        if error:
            session.clear()
            suspended = SUSPENDED_RE.search(error)
            if suspended:
                wait = int(suspended.group(1)) + 60
                logger.warning("Camera %s: Camera has suspended logins; not retrying for %d min", camera_number, wait // 60)
            elif AUTH_ERROR_RE.search(error):
                auth_failures += 1
                if auth_failures >= WATCH_AUTH_MAX_FAILURES:
                    logger.error("Camera %s: Login rejected %d times (%s); no longer watching this camera until "
                                 "restarted. Check username/password.", camera_number, auth_failures, error)
                    return
                wait = WATCH_AUTH_RETRY_S
                logger.error("Camera %s: Login rejected (%s); trying once more in %d min, not sooner, so the "
                             "camera does not lock logins out", camera_number, error, wait // 60)
        else:
            auth_failures = 0
        waited = 0.0
        while waited < wait and not stop.is_set():
            await asyncio.sleep(min(1.0, wait - waited))
            waited += 1.0


async def watch_all(cams: List[Tuple[int, str]], stop: threading.Event, full_scans: bool) -> None:
    await asyncio.gather(*(watch_camera(ip, number, stop, full_scans) for number, ip in cams))


def run_daemon_mode(effective_mode: str) -> int:
    """Long-running modes. Cameras with working ONVIF events are served by tapo-events.py (supervised
    and restarted if it dies): `settle_seconds` after a camera's last detection this script is run
    again with --cam/--start/--end for just that camera and day. Cameras in `watch_cameras` (every
    camera in watch mode) are polled in this process instead."""
    configured = [(i + 1, ip) for i, ip in enumerate(ip_addresses) if ip]
    if not configured:
        logger.error("No camera IP addresses configured.")
        return 1
    if effective_mode == "watch":
        watch_nums = {n for n, _ in configured}
    else:
        watch_nums = {int(w[3]) for w in (c.strip().lower() for c in watch_cameras)}
        for n in sorted(watch_nums - {n for n, _ in configured}):
            logger.warning("watch_cameras lists cam%d but it has no IP address; ignoring.", n)
    watch_cams = [(n, ip) for n, ip in configured if n in watch_nums]
    onvif_cams = [f"cam{n}={ip}" for n, ip in configured if n not in watch_nums]

    cmd = env = None
    if onvif_cams:
        if getattr(sys, "frozen", False):
            logger.error("ONVIF listening needs to run from source (python3 tapo-downloader.py); the PyInstaller "
                         "build supports cron mode, or watch mode (every camera in watch_cameras).")
            return 1
        this_script = os.path.abspath(__file__)
        events_script = os.path.join(os.path.dirname(this_script), EVENTS_SCRIPT_NAME)
        if not os.path.isfile(events_script):
            logger.error("%s not found next to %s; it is required for mode '%s'.", EVENTS_SCRIPT_NAME, this_script, effective_mode)
            return 1
        # The listener formats this template with {cam} {start} {end}; empty start/end (a sweep)
        # means "full scan of that camera". Braces in paths must be escaped.
        esc = lambda s: s.replace("{", "{{").replace("}", "}}")
        hook = " ".join(esc(shlex.quote(p)) for p in (sys.executable, this_script)) + ' --cam {cam} --start "{start}" --end "{end}"'
        cmd = [sys.executable, events_script,
               "--user", onvif_username,
               "--tapo-user", username,
               "--settle", str(settle_seconds),
               "--on-settled", hook,
               "--hook-timeout", str(hook_timeout_minutes * 60),
               "--timezone", str(timezone),
               "--poll-interval", "0"]   # cameras that need polling are watched by this process instead
        for cam in onvif_cams:
            cmd += ["--cam", cam]
        if effective_mode == "sweep":
            cmd += ["--sweep-min", str(sweep_minutes)]
        # Passwords go in the environment, not on the command line.
        env = dict(os.environ, TAPO_PASSWORD=onvif_password, TAPO_CLOUD_PASSWORD=password)

    stop = threading.Event()
    state = {"proc": None}

    def kill_group(sig):
        proc = state["proc"]
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, sig)   # listener plus any download it started
            except ProcessLookupError:
                pass

    def on_signal(signum, _frame):
        stop.set()
        kill_group(signal.SIGTERM)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    def supervise_listener():
        backoff = 5   # first restart waits 10 s, doubling up to 300 s
        while not stop.is_set():
            started = time.time()
            state["proc"] = subprocess.Popen(cmd, env=env, start_new_session=True)
            if stop.is_set():
                kill_group(signal.SIGTERM)
            rc = state["proc"].wait()
            if stop.is_set():
                break
            backoff = 10 if time.time() - started > 120 else min(backoff * 2, 300)
            logger.warning("Event listener exited with code %s; restarting in %d s", rc, backoff)
            stop.wait(backoff)

    logger.info("Mode '%s': ONVIF events for %s | watching %s", effective_mode,
                ", ".join(onvif_cams) or "no cameras",
                ", ".join(f"cam{n}" for n, _ in watch_cams) or "no cameras")
    listener = None
    if onvif_cams:
        logger.info("ONVIF: settle=%ss%s", settle_seconds,
                    f", sweep every {sweep_minutes} min" if effective_mode == "sweep"
                    else ", no internal sweep (use cron --once as catch-all)")
        listener = threading.Thread(target=supervise_listener, name="listener-supervisor", daemon=True)
        listener.start()
    if watch_cams:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(watch_all(watch_cams, stop, effective_mode in ("sweep", "watch")))
        finally:
            loop.close()
    if listener is None and not stop.is_set():
        logger.error("Every watched camera was given up on (see above); nothing left to run.")
        return 1
    while not stop.is_set():
        stop.wait(1)

    if listener is not None:
        listener.join(timeout=25)
        if state["proc"] is not None and state["proc"].poll() is None:
            kill_group(signal.SIGKILL)
    logger.info("Stopped.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Tapo camera recordings. Run mode comes from 'mode' in the config "
                    "(cron | sweep | hybrid) unless overridden here.")
    parser.add_argument("--mode", choices=VALID_MODES, help="override the configured run mode")
    parser.add_argument("--once", action="store_true",
                        help="one-shot full scan of every camera and exit, whatever the mode (use this from cron in hybrid mode)")
    parser.add_argument("--cam", metavar="camN", help="only process this camera slot, e.g. cam1")
    parser.add_argument("--start", default="", metavar="UTC_ISO",
                        help="with --cam: start of an event window, e.g. 2026-10-15T03:20:00Z (empty = full scan of the camera)")
    parser.add_argument("--end", default="", metavar="UTC_ISO", help="with --cam: end of the event window")
    return parser.parse_args()


def window_from_args(start_iso: str, end_iso: str) -> Optional[Tuple[int, int]]:
    """--start/--end as (start, end) epoch seconds, or None when neither was given."""
    def parse(value: str) -> Optional[int]:
        if not value:
            return None
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).timestamp())
    start, end = parse(start_iso), parse(end_iso)
    if start is None and end is None:
        return None
    return (start if start is not None else end, end if end is not None else start)


def resolve_target(args: argparse.Namespace) -> Tuple[Optional[int], Optional[List[str]]]:
    """Turn --cam/--start/--end into (camera number, dates) or (None, None) for all cameras."""
    if not args.cam:
        if args.start or args.end:
            logger.error("--start/--end require --cam.")
            sys.exit(2)
        return None, None
    m = re.fullmatch(r"cam([1-4])", args.cam.strip().lower())
    if not m or int(m.group(1)) > len(ip_addresses) or not ip_addresses[int(m.group(1)) - 1]:
        logger.error("--cam must name a configured camera slot (cam1..cam%d with an IP set).", len(ip_addresses))
        sys.exit(2)
    only_cam = int(m.group(1))
    dates = None
    if args.start or args.end:
        try:
            dates = dates_for_window(args.start, args.end)
        except ValueError:
            logger.error("--start/--end must look like 2026-10-15T03:20:00Z (UTC).")
            sys.exit(2)
    return only_cam, dates


if __name__ == "__main__":
    args = parse_args()
    effective_mode = args.mode or run_mode
    only_cam, dates = resolve_target(args)
    if only_cam is not None:
        TARGET_WINDOW = window_from_args(args.start, args.end)

    if only_cam is None and not args.once and effective_mode in DAEMON_MODES:
        sys.exit(run_daemon_mode(effective_mode))

    loop = asyncio.get_event_loop()
    logger.info("Running in event loop with nest_asyncio")
    try:
        loop.run_until_complete(main(only_cam, dates))
    except KeyboardInterrupt:
        logger.info("Script interrupted by user.")
    finally:
        loop.close()

    if only_cam is not None and RUN_STATS["deferred"]:
        logger.info("%d clip(s) deferred (still recording or camera busy); exiting with code %d.",
                    RUN_STATS["deferred"], EXIT_TEMPFAIL)
        sys.exit(EXIT_TEMPFAIL)
