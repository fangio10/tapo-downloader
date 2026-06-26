import nest_asyncio
import logging
import json
import psutil
import glob
import aiohttp
import time
import ipaddress
from datetime import datetime, timedelta
import pytz
import sys
import os
import asyncio
from functools import wraps
from typing import Dict, List, Optional, Callable
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

# Constants
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".tapo-downloader")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
COMPLETED_FOLDERS_FILE = os.path.join(CONFIG_DIR, "completed_folders.json")
THUMBNAILS_SUBDIR = "thumbnails"
DIR_PERMISSIONS = 0o777
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
    "freshness_seconds": 0  # User-defined override for timeCorrection
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
                logger.error("Field '%s' in %s must be of type %s.", field, CONFIG_FILE, expected_type.__name__)
                sys.exit(1)

        optional_fields = {
            "regenerate_thumbnails": bool,
            "force_regenerate_thumbnails": bool,
            "timezone": str
        }
        for field, expected_type in optional_fields.items():
            if field not in config:
                config[field] = DEFAULT_CONFIG[field]
                logger.info("Setting default '%s' to %s in %s.", field, config[field], CONFIG_FILE)
            if not isinstance(config[field], expected_type):
                logger.error("Field '%s' in %s must be of type %s.", field, CONFIG_FILE, expected_type.__name__)
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

def save_completed_folders(completed_folders: Dict) -> None:
    try:
        with open(COMPLETED_FOLDERS_FILE, "w") as f:
            json.dump(completed_folders, f, indent=2)
        logger.info("Updated completed folders file at %s", COMPLETED_FOLDERS_FILE)
    except Exception as e:
        logger.error("Failed to save completed folders to %s: %s", COMPLETED_FOLDERS_FILE, str(e))

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

def check_disk_space(output_dir: str) -> None:
    try:
        disk = psutil.disk_usage(output_dir)
        free_mb = disk.free / (1024 * 1024)
        logger.info("Disk space: %d MB free in %s", free_mb, output_dir)
        if free_mb < 1000:
            logger.warning("Low disk space (<1GB) in %s.", output_dir)
    except Exception as e:
        logger.error("Failed to check disk space for %s: %s", output_dir, str(e))

def build_video_path(recording_dir: str, filename: str) -> str:
    return os.path.join(recording_dir, filename)

def build_thumbnail_path(recording_dir: str, filename: str) -> str:
    return os.path.join(recording_dir, THUMBNAILS_SUBDIR, os.path.splitext(filename)[0] + ".jpg")

def check_existing_file(filepath: str) -> bool:
    return os.path.exists(filepath)

@handle_exceptions("get_recording_days")
async def get_recording_days(tapo: Tapo, ip_address: str, output_dir: str, completed_folders: Dict) -> List[str]:
    recording_days = []
    today = datetime.now(timezone).date()
    two_days_ago = today - timedelta(days=2)

    for days_back in range(max_days):
        check_date = today - timedelta(days=days_back)
        date_str = check_date.strftime("%Y%m%d")
        day_dir = os.path.join(output_dir, check_date.strftime("%Y-%m-%d"))
        folder_key = f"{ip_address}:{day_dir}"

        if folder_key in completed_folders and check_date < two_days_ago:
            logger.info("Camera %s: Skipping completed folder %s", ip_address, day_dir)
            continue

        try:
            recordings = await tapo.getRecordings(date_str)
            if recordings:
                recording_days.append(date_str)
                logger.info("Camera %s: Found recordings for %s", ip_address, date_str)
        except TypeError:
            recordings = tapo.getRecordings(date_str)
            if recordings:
                recording_days.append(date_str)
                logger.info("Camera %s: Found recordings for %s (non-async)", ip_address, date_str)
        except Exception as e:
            logger.error("Camera %s: Error checking recordings for %s: %s", ip_address, date_str, str(e))

    return sorted(recording_days, reverse=True)

async def process_recording(
    tapo: Tapo,
    recording: Dict,
    date: str,
    time_correction: int,
    output_dir: str,
    ip_address: str,
    start_time: float,
    idx: int
) -> bool:
    logger.info("Camera %s: Processing recording %d for date %s", ip_address, idx + 1, date)
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

        logger.info("Camera %s: Processing recording: start=%s, end=%s", ip_address, start_time_rec, end_time)
        try:
            utc_dt = datetime.utcfromtimestamp(start_time_rec).replace(tzinfo=pytz.UTC)
            local_dt = utc_dt.astimezone(timezone)
            day_dir_name = local_dt.strftime("%Y-%m-%d")
            duration_seconds = end_time - start_time_rec
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            duration_str = f"{minutes}m-{seconds}s"
            filename = f"{local_dt.strftime('%Y-%m-%d_%H-%M')}_{duration_str}.mp4"
            logger.info("Camera %s: Generated filename %s for timestamp %s", ip_address, filename, start_time_rec)
        except (ValueError, TypeError) as e:
            logger.error("Camera %s: Failed to process timestamp %s: %s", ip_address, start_time_rec, str(e))
            day_dir_name = "unknown"
            filename = f"recording_{idx+1}.mp4"

        recording_dir = os.path.join(output_dir, day_dir_name)
        os.makedirs(recording_dir, exist_ok=True)
        try:
            os.chmod(recording_dir, DIR_PERMISSIONS)
        except Exception as e:
            logger.error("Camera %s: Failed to set permissions for %s: %s", ip_address, recording_dir, str(e))

        video_path = build_video_path(recording_dir, filename)
        thumbnail_path = build_thumbnail_path(recording_dir, filename)

        if check_existing_file(video_path) and not config.get("overwrite_files", False):
            logger.info("Camera %s: Skipping download: file %s already exists", ip_address, video_path)
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

        logger.info("Camera %s: Initiating download for recording %s to %s", ip_address, key, video_path)
        try:
            # Use freshness_seconds if non-zero, otherwise use camera-provided time_correction
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
            last_status = None
            async for status in downloader.download():
                last_status = status
                status_string = f"{status['currentAction']} {status.get('fileName', 'unknown')}"
                if status["progress"] > 0:
                    status_string += f": {round(status['progress'], 2)} / {status['total']}"
                else:
                    status_string += "..."
                logger.info("Camera %s: %s", ip_address, status_string)

            actual_file = last_status.get('fileName') if last_status else None
            if actual_file and os.path.exists(actual_file):
                logger.info("Camera %s: Finished downloading to %s", ip_address, actual_file)
                thumbnail_file = build_thumbnail_path(recording_dir, filename)
                if os.path.exists(thumbnail_file):
                    logger.info("Camera %s: Thumbnail created at %s", ip_address, thumbnail_file)
                else:
                    logger.warning("Camera %s: Thumbnail not found at %s", ip_address, thumbnail_file)
            else:
                logger.warning("Camera %s: Expected file %s not found after download", ip_address, actual_file)
        except Exception as e:
            logger.error("Camera %s: Download failed for %s: %s", ip_address, key, str(e))
            actual_file = last_status.get('fileName') if last_status else None
            if actual_file and os.path.exists(actual_file):
                os.remove(actual_file)
                logger.info("Camera %s: Removed partial file %s", ip_address, actual_file)
    return False

@handle_exceptions("download_recordings_for_day")
async def download_recordings_for_day(
    tapo: Tapo,
    date: str,
    time_correction: int,
    output_dir: str,
    ip_address: str,
    start_time: float,
    completed_folders: Dict
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
            logger.info("Camera %s: Skipping completed folder %s", ip_address, day_dir)
            return False

        logger.info("Camera %s: Getting recordings for %s", ip_address, date)
        try:
            recordings = await tapo.getRecordings(date)
        except TypeError:
            recordings = tapo.getRecordings(date)

        if not recordings:
            logger.warning("Camera %s: No recordings found for %s", ip_address, date)
            return False

        logger.info("Camera %s: Found %d recordings for %s", ip_address, len(recordings), date)
        for idx, recording in enumerate(recordings):
            elapsed_seconds = time.time() - start_time
            if elapsed_seconds > timeout_seconds:
                logger.info("Camera %s: Timeout reached (%d minutes). Aborting download.",
                            ip_address, config["timeout_minutes"])
                return True

            logger.debug("Camera %s: Processing recording %d: %s", ip_address, idx + 1, recording)
            try:
                await process_recording(tapo, recording, date, time_correction, output_dir, ip_address, start_time, idx)
            except Exception as e:
                logger.error("Camera %s: Failed to process recording %d for %s: %s", ip_address, idx + 1, date, str(e))

        if folder_date < two_days_ago:
            completed_folders[folder_key] = {"completed": True, "date": date}
            save_completed_folders(completed_folders)
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

@handle_exceptions("process_camera")
async def process_camera(ip_address: str, camera_number: int, base_download_dir: str, username: str, password: str, start_time: float) -> bool:
    output_dir = os.path.join(base_download_dir, f"cam{camera_number}")
    os.makedirs(output_dir, exist_ok=True)
    try:
        os.chmod(output_dir, DIR_PERMISSIONS)
    except Exception as e:
        logger.error("Camera %s: Failed to set permissions for %s: %s", camera_number, output_dir, str(e))

    completed_folders = load_completed_folders()
    check_disk_space(output_dir)
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
        return False

    logger.info("Camera %s: Authenticating and fetching camera info", camera_number)
    try:
        basic_info = await tapo.getBasicInfo()
        logger.info("Camera %s: Authentication successful. Camera info: %s", camera_number, basic_info)
    except TypeError:
        basic_info = tapo.getBasicInfo()
        logger.info("Camera %s: Authentication successful (non-async). Camera info: %s", camera_number, basic_info)
    except Exception as e:
        logger.error("Camera %s: Authentication failed for %s: %s", camera_number, ip_address, str(e))
        return False

    logger.info("Camera %s: Getting time correction", camera_number)
    try:
        time_correction = await tapo.getTimeCorrection()
    except TypeError:
        time_correction = tapo.getTimeCorrection()
    except Exception as e:
        logger.error("Camera %s: Failed to get time correction for %s: %s", camera_number, ip_address, str(e))
        time_correction = 0
    logger.info("Camera %s: Time correction: %s", camera_number, time_correction)

    logger.info("Camera %s: Fetching days with recordings (up to %d days)", camera_number, max_days)
    recording_days = await get_recording_days(tapo, ip_address, output_dir, completed_folders)
    if not recording_days:
        logger.warning("Camera %s: No days with recordings found", camera_number)
    else:
        logger.info("Camera %s: Found recordings for %d days: %s", camera_number, len(recording_days), recording_days)

        for date in recording_days:
            should_exit = await download_recordings_for_day(tapo, date, time_correction, output_dir, ip_address, start_time, completed_folders)
            if should_exit:
                logger.info("Camera %s: Exiting due to timeout", camera_number)
                return True

    video_files = glob.glob(os.path.join(output_dir, "**", "*.mp4"), recursive=True)
    logger.info("Camera %s: Final files in %s: %d files", camera_number, output_dir, len(video_files))

    if regenerate_thumbnails:
        await regenerate_missing_thumbnails(output_dir, camera_number, video_files)

    return False

async def main():
    start_time = time.time()
    async with aiohttp.ClientSession() as session:
        tasks = []
        task_indices = []
        logger.info("Processing IP addresses: %s", ip_addresses)
        for idx, ip_address in enumerate(ip_addresses):
            if not ip_address:
                logger.info("Skipping empty IP at position %d (would be cam%d)", idx, idx + 1)
                continue
            try:
                ipaddress.IPv4Address(ip_address)
            except ValueError:
                logger.info("Skipping invalid IP %s at position %d (would be cam%d)", ip_address, idx, idx + 1)
                continue
            camera_number = idx + 1
            tasks.append(process_camera(ip_address, camera_number, base_download_dir, username, password, start_time))
            task_indices.append(idx)

        if not tasks:
            logger.error("No valid IP addresses to process.")
            return

        logger.info("Starting concurrent processing for %d cameras: %s", len(tasks), [ip for ip in ip_addresses if ip])
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

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    logger.info("Running in event loop with nest_asyncio")
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Script interrupted by user.")
    finally:
        loop.close()
