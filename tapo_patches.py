"""
tapo_patches.py
---------------
Local customisations on top of pytapo, kept thin so that upgrading pytapo
(pip install -U pytapo) keeps working. The download logic itself (the fast
"download" request the Tapo app uses, stall timeouts, camera-busy handling,
retry and fall-back to real-time playback) comes from pytapo's own Downloader;
this file only adds:

  Convert
    - generate_thumbnail() method added via monkey-patch

  CustomDownloader (subclass of pytapo Downloader)
    - download_method: "download" (about 10x real time, what the Tapo app uses)
      or "playback" (real time). Needs a pytapo with the `method` option; on an
      older pytapo it is ignored and playback is used.
    - FRESH_RECORDING_TIME_SECONDS = 0: a clip counts as finished as soon as the
      camera's own clock passes its end time (pytapo's default adds 60 s)
    - segments shorter than 3 s are skipped
    - the clip is written to <day folder>/.partial/ and moved into place only
      once complete, so a run that is killed mid-save never leaves a truncated
      file under its final name (the downloader treats existing files as done)
    - a thumbnail is generated after every successful save; a thumbnail problem
      is reported but never discards the clip
"""

import asyncio
import inspect
import logging
import os
import subprocess
from datetime import datetime, timezone

from pytapo.media_stream.convert import Convert
from pytapo.media_stream.downloader import Downloader

log = logging.getLogger("tapo_patches")


# ---------------------------------------------------------------------------
# Patch 1 – add generate_thumbnail to Convert
# ---------------------------------------------------------------------------

async def _generate_thumbnail(
    self,
    video_path,
    thumbnail_path,
    height=100,
    time_percentage=0.1,
    quality=2,
    max_retries=3,
):
    """Generate a JPEG thumbnail for *video_path* and save it to *thumbnail_path*."""
    for attempt in range(max_retries + 1):
        try:
            thumbnail_dir = os.path.dirname(thumbnail_path)
            os.makedirs(thumbnail_dir, exist_ok=True)
            os.chmod(thumbnail_dir, 0o777)

            thumbnail_time = 5.0  # fallback
            try:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v", "fatal",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        video_path,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                thumbnail_time = float(result.stdout) * time_percentage
            except Exception as e:
                print(
                    f"Warning: Failed to probe video duration for {video_path}. "
                    f"Using fallback thumbnail time of 5 s. Error: {e}"
                )

            cmd = (
                f'ffmpeg -ss {thumbnail_time} -i "{video_path}" -vframes 1 '
                f'-vf scale=-1:{height} -q:v {quality} -f image2 '
                f'"{thumbnail_path}" -y >{os.devnull} 2>&1'
            )
            retcode = os.system(cmd)
            if retcode != 0:
                raise Exception(f"FFmpeg failed with return code {retcode}")
            if not os.path.exists(thumbnail_path):
                raise Exception("Thumbnail file was not created")
            return

        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                continue
            if os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
            raise Exception(
                f"Failed to generate thumbnail after {max_retries} attempts: {e}"
            )


# Attach to the class so every Convert instance gets the method.
Convert.generate_thumbnail = _generate_thumbnail


# ---------------------------------------------------------------------------
# Patch 2 – CustomDownloader subclass
# ---------------------------------------------------------------------------

class CustomDownloader(Downloader):
    """Drop-in replacement for pytapo's Downloader (see the module docstring).

    Extra constructor kwargs (all optional):
      thumbnail_height            int    default 100
      thumbnail_time_percentage   float  default 0.1
      thumbnail_quality           int    default 2
      force_regenerate_thumbnails bool   default False
      max_retries                 int    thumbnail retries, default 3
      download_method             str    "download" (default) or "playback"
    """

    FRESH_RECORDING_TIME_SECONDS = 0

    def __init__(self, tapo, start_time, end_time, time_correction,
                 outputDirectory="./", fileName=None, overwriteFiles=False,
                 window_size=None, padding=None,
                 thumbnail_height=100,
                 thumbnail_time_percentage=0.1,
                 thumbnail_quality=2,
                 force_regenerate_thumbnails=False,
                 max_retries=3,
                 download_method="download",
                 **kwargs):

        if "method" in inspect.signature(Downloader.__init__).parameters:
            kwargs["method"] = download_method
        elif download_method == "download":
            log.warning("This pytapo has no fast download method (pip install -U pytapo); using real-time playback.")

        super().__init__(
            tapo=tapo,
            startTime=start_time,
            endTime=end_time,
            timeCorrection=time_correction,
            outputDirectory=outputDirectory,
            fileName=fileName,
            overwriteFiles=overwriteFiles,
            window_size=window_size,
            padding=padding,
            **kwargs,
        )
        self.thumbnail_height = thumbnail_height
        self.thumbnail_time_percentage = thumbnail_time_percentage
        self.thumbnail_quality = thumbnail_quality
        self.force_regenerate_thumbnails = force_regenerate_thumbnails
        self.max_retries = max_retries

    @staticmethod
    def _thumbnail_path(video_path):
        return os.path.join(
            os.path.dirname(video_path),
            "thumbnails",
            os.path.splitext(os.path.basename(video_path))[0] + ".jpg",
        )

    @staticmethod
    def _remove_partials(partial_dir, name):
        """Delete leftovers of an interrupted run (the clip and pytapo's .ts / audio temp files)."""
        try:
            for entry in os.listdir(partial_dir):
                if entry == name or entry.startswith(name + "."):
                    os.remove(os.path.join(partial_dir, entry))
        except OSError:
            pass

    async def download(self, retry=False):
        segment_length = self.endTime - self.startTime
        final_dir = self.outputDirectory
        if self.fileName is None:
            start = datetime.fromtimestamp(int(self.startTime), timezone.utc).strftime("%Y-%m-%d %H_%M_%S")
            end = datetime.fromtimestamp(int(self.endTime), timezone.utc).strftime("%Y-%m-%d %H_%M_%S")
            self.fileName = f"{start}-{end}.mp4"
        name = self.fileName
        final_path = os.path.join(final_dir, name)

        if segment_length < 3:
            yield {"currentAction": "Skipping (duration too short)", "fileName": final_path,
                   "progress": 0, "total": 0}
            return
        if os.path.isfile(final_path) and not self.overwriteFiles:
            yield {"currentAction": "Skipping", "fileName": final_path, "progress": 0, "total": 0}
            return

        partial_dir = os.path.join(final_dir, ".partial")
        partial_path = os.path.join(partial_dir, name)
        os.makedirs(partial_dir, exist_ok=True)
        self._remove_partials(partial_dir, name)
        self.outputDirectory = partial_dir + os.sep   # pytapo writes <outputDirectory><fileName>
        self.overwriteFiles = True                    # the partial name is ours; the final-name check was done above
        saved = False
        try:
            async for status in super().download(retry):
                out = dict(status)
                if out.get("fileName") == partial_path:
                    out["fileName"] = final_path
                yield out

            if os.path.isfile(partial_path):
                os.replace(partial_path, final_path)   # atomic: the clip appears complete or not at all
                saved = True
        finally:
            self.outputDirectory = final_dir
            self._remove_partials(partial_dir, name)
            try:
                os.rmdir(partial_dir)   # only succeeds when empty
            except OSError:
                pass

        if saved:
            thumbnail_path = self._thumbnail_path(final_path)
            if self.force_regenerate_thumbnails or not os.path.exists(thumbnail_path):
                yield {"currentAction": "Generating thumbnail", "fileName": final_path, "progress": 0, "total": 0}
                try:
                    await Convert().generate_thumbnail(
                        final_path,
                        thumbnail_path,
                        height=self.thumbnail_height,
                        time_percentage=self.thumbnail_time_percentage,
                        quality=self.thumbnail_quality,
                        max_retries=self.max_retries,
                    )
                except Exception as e:
                    # The clip is safely saved; a missing thumbnail is reported by the caller and can be rebuilt later.
                    log.warning("Thumbnail for %s failed: %s", final_path, e)
                    yield {"currentAction": "Thumbnail failed", "fileName": final_path, "progress": 0, "total": 0}
            else:
                yield {"currentAction": "Skipping thumbnail generation (exists)", "fileName": final_path,
                       "progress": 0, "total": 0}
