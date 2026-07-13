# Tapo Downloader

A background archiving tool that continuously downloads recordings from
TP-Link Tapo cameras and organizes them into a clean, per-day folder
structure with generated thumbnails and detection-type labels.

Tapo Downloader is a headless archiver — it does not have a viewer UI.
Pair it with [Tapo Streamer](https://github.com/fangio10/tapo-streamer)
for a native desktop viewer: point Tapo Streamer's archive directory at
the same folder this tool writes to, and it will give you a live 4-camera
grid, an archive browser, and a synchronized motion-event timeline built
on top of the clips this tool downloads.

## Features

- **Multi-camera support** — polls up to 4 Tapo cameras concurrently
  from a single config file.
- **Continuous, incremental archiving** — designed to be run on a
  schedule (e.g. via cron or a scheduled task); skips clips it has
  already downloaded and tracks fully-processed day-folders so it
  doesn't re-scan old, completed days.
- **Detection-type labeling** — recordings are matched against the
  camera's event log so filenames reflect what actually triggered them
  (`person`, `vehicle`, `pet`, `line_crossing`, generic `motion`, etc.)
  instead of a generic label.
- **Thumbnail generation** — extracts a representative JPEG thumbnail
  for every clip (configurable height, sample point, and quality), with
  options to regenerate missing or all thumbnails without re-downloading
  video.
- **Robust downloads** — retries failed/partial downloads, skips clips
  shorter than 3 seconds, avoids re-downloading in-progress recordings,
  and works around known Tapo TLS/cipher quirks out of the box.
- **Predictable archive layout** — clips are written to
  `<download_directory>/camN/YYYY-MM-DD/` with human-readable filenames
  containing the date, time, and duration, and thumbnails in a parallel
  `thumbnails/` subfolder — the exact structure Tapo Streamer expects.

## Requirements

- Python 3
- [FFmpeg](https://ffmpeg.org/) (`ffmpeg` and `ffprobe`) installed and
  on your `PATH` — used for converting the raw stream and generating
  thumbnails
- One or more TP-Link Tapo cameras with a local/cloud account
  configured via the official Tapo app
- Python packages listed in `requirements.txt`

## Installation

```
git clone <this-repo-url>
cd tapo-downloader
pip install -r requirements.txt
python3 tapo-downloader.py
```

On first run the script creates a default config file for you to edit
(see below) and exits early if required fields are missing or invalid.

## Configuration

On first launch, Tapo Downloader creates a config directory:

- `~/.tapo-downloader/config.json` — main settings
- `~/.tapo-downloader/completed_folders.json` — internal tracking of
  fully-archived day-folders, so old days aren't rescanned forever

Example `config.json`:

```json
{
  "ip_addresses": ["192.168.1.100", "", "", ""],
  "username": "cloud_username",
  "password": "cloud_password",
  "download_directory": "/nas/tapo/",
  "max_days": 30,
  "timezone": "Australia/Sydney",
  "timeout_minutes": 55,
  "regenerate_thumbnails": false,
  "force_regenerate_thumbnails": false,
  "thumbnail_height": 100,
  "thumbnail_time_percentage": 0.1,
  "thumbnail_quality": 2,
  "max_retries": 3
}
```

| Field | Description |
|---|---|
| `ip_addresses` | Up to 4 camera IPs. Empty strings are skipped (camera slot not in use). |
| `username` / `password` | Your Tapo cloud account credentials. |
| `download_directory` | Root folder for the archive. Must exist and be writable. |
| `max_days` | How many days back to check for recordings each run. |
| `timezone` | Used to bucket recordings into local-date folders. |
| `timeout_minutes` | Soft time budget per run before the script stops starting new downloads (useful when run on a schedule). |
| `regenerate_thumbnails` | If true, sweep existing videos at the end of a run and fill in any missing thumbnails. |
| `force_regenerate_thumbnails` | If true, regenerate thumbnails even if one already exists. |
| `thumbnail_height` | Thumbnail height in pixels (width scales automatically). |
| `thumbnail_time_percentage` | Where in the clip to grab the thumbnail frame, as a fraction of duration (e.g. `0.1` = 10% in). |
| `thumbnail_quality` | FFmpeg JPEG quality scale (2 = best, 31 = worst). |
| `max_retries` | Retry attempts for downloads and thumbnail generation. |

## Usage

Run it directly:

```
python3 tapo-downloader.py
```

Or on a schedule, e.g. via cron every hour:

```
0 * * * * cd /path/to/tapo-downloader && python3 tapo-downloader.py >> /var/log/tapo-downloader.log 2>&1
```

Each run:

1. Connects to every configured camera concurrently.
2. Walks back through the last `max_days` days looking for recordings,
   skipping day-folders already marked complete.
3. For each recording, matches it against the camera's event log to
   determine detection type, then downloads it (skipping ones already
   on disk) with a generated thumbnail.
4. Stops starting new downloads once `timeout_minutes` is reached, so a
   scheduled run won't overrun into the next one.

## Archive layout

```
<download_directory>/
  cam1/
    2026-07-12/
      2026-07-12_14-32_0m-45s_person.mp4
      thumbnails/
        2026-07-12_14-32_0m-45s_person.jpg
    2026-07-13/
      ...
  cam2/
    ...
```

This layout matches what
[Tapo Streamer](https://github.com/fangio10/tapo-streamer) expects for
its archive browser and Events view — just set Tapo Streamer's archive
directory to the same path as `download_directory` here.

## Building a standalone executable

A PyInstaller spec file (`tapo-downloader.spec`) is included:

```
pip install pyinstaller
pyinstaller tapo-downloader.spec
```

## Related project

- [Tapo Streamer](https://github.com/fangio10/tapo-streamer) — the
  desktop viewer companion app. It reads the archive this tool produces
  and adds live viewing, PTZ control, archive browsing, and a
  motion-event timeline. Tapo Downloader does the downloading; Tapo
  Streamer does the viewing.

## Disclaimer

This is an independent, unofficial project and is not affiliated with
or endorsed by TP-Link.
