"""
tapo_patches.py
---------------
Extends pytapo's Convert and Downloader classes with local customisations
so that upgrading the pytapo library never overwrites these changes.

Customisations applied:
  Convert
    - generate_thumbnail() method added via monkey-patch

  CustomDownloader (subclass of pytapo Downloader)
    - Predefined filename format  (<date>_<HH-MM>_<Xm-Ys>.mp4)
    - Thumbnail generation after every successful save
    - Short-segment skip  (< 3 s)
    - Respects overwriteFiles flag
    - Uses timeCorrection directly (no FRESH_RECORDING_TIME_SECONDS offset)
    - Accepts thumbnail_* and max_retries kwargs
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime

from pytapo.media_stream.convert import Convert
from pytapo.media_stream.downloader import Downloader


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
    """
    Drop-in replacement for pytapo's Downloader that adds:
      - Predefined filename derived from the recording's local timestamp
      - Thumbnail generation after save
      - Short-segment (< 3 s) skipping
      - overwriteFiles support
      - Direct timeCorrection usage (no FRESH_RECORDING_TIME_SECONDS offset)

    Extra constructor kwargs (all optional, ignored by parent):
      thumbnail_height           int    default 100
      thumbnail_time_percentage  float  default 0.1
      thumbnail_quality          int    default 2
      force_regenerate_thumbnails bool  default False
      max_retries                int    default 3
    """

    def __init__(self, tapo, start_time, end_time, time_correction,
                 outputDirectory="./", fileName=None, overwriteFiles=False,
                 window_size=None, padding=None,
                 # thumbnail / retry kwargs consumed here, not passed to super
                 thumbnail_height=100,
                 thumbnail_time_percentage=0.1,
                 thumbnail_quality=2,
                 force_regenerate_thumbnails=False,
                 max_retries=3,
                 **kwargs):

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

    def _thumbnail_path(self, video_path):
        return os.path.join(
            os.path.dirname(video_path),
            "thumbnails",
            os.path.splitext(os.path.basename(video_path))[0] + ".jpg",
        )

    async def _maybe_generate_thumbnail(self, convert, video_path):
        """Yield status dicts and generate (or skip) the thumbnail."""
        thumbnail_path = self._thumbnail_path(video_path)
        if self.force_regenerate_thumbnails or not os.path.exists(thumbnail_path):
            yield {"currentAction": "Generating thumbnail", "fileName": video_path,
                   "progress": 0, "total": 0}
            await convert.generate_thumbnail(
                video_path,
                thumbnail_path,
                height=self.thumbnail_height,
                time_percentage=self.thumbnail_time_percentage,
                quality=self.thumbnail_quality,
                max_retries=self.max_retries,
            )
        else:
            yield {"currentAction": "Skipping thumbnail generation (exists)",
                   "fileName": video_path, "progress": 0, "total": 0}

    async def download(self, retry=False):  # noqa: C901  (complexity is inherited)
        downloading = True
        while downloading:
            dateStart = datetime.utcfromtimestamp(int(self.startTime)).strftime(
                "%Y-%m-%d %H_%M_%S"
            )
            dateEnd = datetime.utcfromtimestamp(int(self.endTime)).strftime(
                "%Y-%m-%d %H_%M_%S"
            )
            segmentLength = self.endTime - self.startTime

            # ── short-segment skip ──────────────────────────────────────────
            if segmentLength < 3:
                fileName = (
                    self.outputDirectory + str(dateStart) + "-" + dateEnd + ".mp4"
                    if self.fileName is None
                    else os.path.join(self.outputDirectory, self.fileName)
                )
                yield {"currentAction": "Skipping (duration too short)",
                       "fileName": fileName, "progress": 0, "total": 0}
                return

            # ── resolve filename ────────────────────────────────────────────
            if self.fileName is None:
                fileName = self.outputDirectory + str(dateStart) + "-" + dateEnd + ".mp4"
            else:
                fileName = os.path.join(self.outputDirectory, self.fileName)

            thumbnailName = self._thumbnail_path(fileName)

            # ── still recording? ────────────────────────────────────────────
            if datetime.now().timestamp() - self.timeCorrection < self.endTime:
                yield {"currentAction": "Recording in progress",
                       "fileName": fileName, "progress": 0, "total": 0}
                downloading = False

            # ── already on disk ─────────────────────────────────────────────
            elif os.path.isfile(fileName) and not self.overwriteFiles:
                yield {"currentAction": "Skipping",
                       "fileName": fileName, "progress": 0, "total": 0}
                downloading = False

            # ── actually download ────────────────────────────────────────────
            else:
                from pytapo.media_stream._utils import StreamType

                convert = Convert()
                if self.audio_sample_rate is None:
                    self.audio_sample_rate = await self._get_audio_sample_rate()

                mediaSession = self.tapo.getMediaSession(StreamType.Download)
                if retry:
                    mediaSession.set_window_size(50)
                else:
                    mediaSession.set_window_size(self.window_size)

                async with mediaSession:
                    payload = json.dumps({
                        "type": "request",
                        "seq": 1,
                        "params": {
                            "playback": {
                                "client_id": self.tapo.getUserID(),
                                "channels": [0, 1],
                                "scale": "1/1",
                                "start_time": str(self.startTime),
                                "end_time": str(self.endTime),
                                "event_type": [1, 2],
                            },
                            "method": "get",
                        },
                    })

                    dataChunks = 0
                    currentAction = "Retrying" if retry else "Downloading"
                    downloadedFull = False

                    async for resp in mediaSession.transceive(payload):
                        if resp.mimetype == "video/mp2t":
                            dataChunks += 1
                            convert.write(
                                resp.plaintext,
                                resp.audioPayload,
                                resp.audioPayloadType,
                                self.audio_sample_rate,
                            )
                            detectedLength = convert.getLength()
                            if detectedLength is False:
                                yield {"currentAction": currentAction, "fileName": fileName,
                                       "progress": 0, "total": segmentLength}
                                detectedLength = 0
                            else:
                                yield {"currentAction": currentAction, "fileName": fileName,
                                       "progress": detectedLength, "total": segmentLength}

                            if (detectedLength > segmentLength + self.padding) or (
                                retry and detectedLength >= segmentLength
                            ):
                                downloadedFull = True
                                yield {"currentAction": "Converting", "fileName": fileName,
                                       "progress": 0, "total": 0}
                                await convert.save(fileName, segmentLength)
                                async for s in self._maybe_generate_thumbnail(convert, fileName):
                                    yield s
                                downloading = False
                                break

                        elif resp.mimetype == "application/json":
                            try:
                                json_data = json.loads(resp.plaintext.decode())
                                if (
                                    "type" in json_data
                                    and json_data["type"] == "notification"
                                    and json_data.get("params", {}).get("event_type") == "stream_status"
                                    and json_data["params"].get("status") == "finished"
                                ):
                                    downloadedFull = True
                                    yield {"currentAction": "Converting", "fileName": fileName,
                                           "progress": 0, "total": 0}
                                    await convert.save(fileName, convert.getLength())
                                    async for s in self._maybe_generate_thumbnail(convert, fileName):
                                        yield s
                                    downloading = False
                                    break
                            except json.JSONDecodeError:
                                self.tapo.logger.debugLog("Unable to parse JSON sent from device")

                    if downloading:
                        if not downloadedFull and not retry:
                            yield {"currentAction": "Retrying", "fileName": fileName,
                                   "progress": 0, "total": 0}
                            retry = True
                        else:
                            detectedLength = convert.getLength()
                            if detectedLength >= segmentLength - 5:
                                downloadedFull = True
                                yield {"currentAction": "Converting [shorter]",
                                       "fileName": fileName, "progress": 0, "total": 0}
                                await convert.save(fileName, segmentLength)
                                async for s in self._maybe_generate_thumbnail(convert, fileName):
                                    yield s
                            else:
                                yield {"currentAction": "Giving up", "fileName": fileName,
                                       "progress": 0, "total": 0}
                            downloading = False
