import os
import json
from datetime import datetime
from pytapo.media_stream.convert import Convert

class Downloader:
    def __init__(
        self,
        tapo,
        start_time,
        end_time,
        time_correction,
        outputDirectory,
        fileName=None,
        max_retries=3,
        thumbnail_height=100,
        thumbnail_time_percentage=0.1,
        thumbnail_quality=2,
        force_regenerate_thumbnails=False,
        overwriteFiles=False,
        window_size=100,
        padding=0
    ):
        self.tapo = tapo
        self.startTime = start_time
        self.endTime = end_time
        self.timeCorrection = time_correction
        self.outputDirectory = outputDirectory
        self.fileName = fileName
        self.max_retries = max_retries
        self.thumbnail_height = thumbnail_height
        self.thumbnail_time_percentage = thumbnail_time_percentage
        self.thumbnail_quality = thumbnail_quality
        self.force_regenerate_thumbnails = force_regenerate_thumbnails
        self.overwriteFiles = overwriteFiles
        self.window_size = window_size
        self.padding = padding

    async def download(self, retry=False):
        downloading = True
        while downloading:
            dateStart = datetime.utcfromtimestamp(int(self.startTime)).strftime(
                "%Y-%m-%d %H_%M_%S"
            )
            dateEnd = datetime.utcfromtimestamp(int(self.endTime)).strftime(
                "%Y-%m-%d %H_%M_%S"
            )
            segmentLength = self.endTime - self.startTime
            
            # Skip files with duration less than 3 seconds
            if segmentLength < 3:
                currentAction = "Skipping (duration too short)"
                fileName = (
                    self.outputDirectory + str(dateStart) + "-" + dateEnd + ".mp4"
                    if self.fileName is None
                    else os.path.join(self.outputDirectory, self.fileName)
                )
                yield {
                    "currentAction": currentAction,
                    "fileName": fileName,
                    "progress": 0,
                    "total": 0,
                }
                downloading = False
                return

            if self.fileName is None:
                fileName = (
                    self.outputDirectory + str(dateStart) + "-" + dateEnd + ".mp4"
                )
            else:
                fileName = os.path.join(self.outputDirectory, self.fileName)
            thumbnailName = os.path.join(
                self.outputDirectory, "thumbnails",
                os.path.splitext(os.path.basename(fileName))[0] + ".jpg"
            )
            if (
                datetime.now().timestamp()
                - self.timeCorrection
                < self.endTime
            ):
                currentAction = "Recording in progress"
                yield {
                    "currentAction": currentAction,
                    "fileName": fileName,
                    "progress": 0,
                    "total": 0,
                }
                downloading = False
            elif os.path.isfile(fileName) and not self.overwriteFiles:
                currentAction = "Skipping"
                yield {
                    "currentAction": currentAction,
                    "fileName": fileName,
                    "progress": 0,
                    "total": 0,
                }
                downloading = False
            else:
                convert = Convert()
                mediaSession = self.tapo.getMediaSession()
                if retry:
                    mediaSession.set_window_size(50)
                else:
                    mediaSession.set_window_size(self.window_size)
                async with mediaSession:
                    payload = {
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
                    }

                    payload = json.dumps(payload)
                    dataChunks = 0
                    if retry:
                        currentAction = "Retrying"
                    else:
                        currentAction = "Downloading"
                    downloadedFull = False
                    async for resp in mediaSession.transceive(payload):
                        if resp.mimetype == "video/mp2t":
                            dataChunks += 1
                            convert.write(resp.plaintext, resp.audioPayload)
                            detectedLength = convert.getLength()
                            if detectedLength is False:
                                yield {
                                    "currentAction": currentAction,
                                    "fileName": fileName,
                                    "progress": 0,
                                    "total": segmentLength,
                                }
                                detectedLength = 0
                            else:
                                yield {
                                    "currentAction": currentAction,
                                    "fileName": fileName,
                                    "progress": detectedLength,
                                    "total": segmentLength,
                                }
                            if (detectedLength > segmentLength + self.padding) or (
                                retry and detectedLength >= segmentLength
                            ):
                                downloadedFull = True
                                currentAction = "Converting"
                                yield {
                                    "currentAction": currentAction,
                                    "fileName": fileName,
                                    "progress": 0,
                                    "total": 0,
                                }
                                await convert.save(fileName, segmentLength)
                                # Generate thumbnail only if forced or doesn't exist
                                if self.force_regenerate_thumbnails or not os.path.exists(thumbnailName):
                                    currentAction = "Generating thumbnail"
                                    yield {
                                        "currentAction": currentAction,
                                        "fileName": fileName,
                                        "progress": 0,
                                        "total": 0,
                                    }
                                    await convert.generate_thumbnail(
                                        fileName,
                                        thumbnailName,
                                        height=self.thumbnail_height,
                                        time_percentage=self.thumbnail_time_percentage,
                                        quality=self.thumbnail_quality,
                                        max_retries=self.max_retries
                                    )
                                else:
                                    currentAction = "Skipping thumbnail generation (exists)"
                                    yield {
                                        "currentAction": currentAction,
                                        "fileName": fileName,
                                        "progress": 0,
                                        "total": 0,
                                    }
                                downloading = False
                                break
                        elif resp.mimetype == "application/json":
                            try:
                                json_data = json.loads(resp.plaintext.decode())
                                if (
                                    "type" in json_data
                                    and json_data["type"] == "notification"
                                    and "params" in json_data
                                    and "event_type" in json_data["params"]
                                    and json_data["params"]["event_type"] == "stream_status"
                                    and "status" in json_data["params"]
                                    and json_data["params"]["status"] == "finished"
                                ):
                                    downloadedFull = True
                                    currentAction = "Converting"
                                    yield {
                                        "currentAction": currentAction,
                                        "fileName": fileName,
                                        "progress": 0,
                                        "total": 0,
                                    }
                                    await convert.save(fileName, convert.getLength())
                                    # Generate thumbnail only if forced or doesn't exist
                                    if self.force_regenerate_thumbnails or not os.path.exists(thumbnailName):
                                        currentAction = "Generating thumbnail"
                                        yield {
                                            "currentAction": currentAction,
                                            "fileName": fileName,
                                            "progress": 0,
                                            "total": 0,
                                        }
                                        await convert.generate_thumbnail(
                                            fileName,
                                            thumbnailName,
                                            height=self.thumbnail_height,
                                            time_percentage=self.thumbnail_time_percentage,
                                            quality=self.thumbnail_quality,
                                            max_retries=self.max_retries
                                        )
                                    else:
                                        currentAction = "Skipping thumbnail generation (exists)"
                                        yield {
                                            "currentAction": currentAction,
                                            "fileName": fileName,
                                            "progress": 0,
                                            "total": 0,
                                        }
                                    downloading = False
                                    break
                            except json.JSONDecodeError:
                                self.tapo.debugLog("Unable to parse JSON sent from device")
                    if downloading:
                        if not downloadedFull and not retry:
                            currentAction = "Retrying"
                            yield {
                                "currentAction": currentAction,
                                "fileName": fileName,
                                "progress": 0,
                                "total": 0,
                            }
                            retry = True
                        else:
                            detectedLength = convert.getLength()
                            if detectedLength >= segmentLength - 5:
                                downloadedFull = True
                                currentAction = "Converting [shorter]"
                                yield {
                                    "currentAction": currentAction,
                                    "fileName": fileName,
                                    "progress": 0,
                                    "total": 0,
                                }
                                await convert.save(fileName, segmentLength)
                                # Generate thumbnail only if forced or doesn't exist
                                if self.force_regenerate_thumbnails or not os.path.exists(thumbnailName):
                                    currentAction = "Generating thumbnail"
                                    yield {
                                        "currentAction": currentAction,
                                        "fileName": fileName,
                                        "progress": 0,
                                        "total": 0,
                                    }
                                    await convert.generate_thumbnail(
                                        fileName,
                                        thumbnailName,
                                        height=self.thumbnail_height,
                                        time_percentage=self.thumbnail_time_percentage,
                                        quality=self.thumbnail_quality,
                                        max_retries=self.max_retries
                                    )
                                else:
                                    currentAction = "Skipping thumbnail generation (exists)"
                                    yield {
                                        "currentAction": currentAction,
                                        "fileName": fileName,
                                        "progress": 0,
                                        "total": 0,
                                    }
                            else:
                                currentAction = "Giving up"
                                yield {
                                    "currentAction": currentAction,
                                    "fileName": fileName,
                                    "progress": 0,
                                    "total": 0,
                                }
                            downloading = False