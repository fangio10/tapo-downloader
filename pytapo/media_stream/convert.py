import io
import subprocess
import os
import datetime
import tempfile
import aiofiles
import ffmpeg
import asyncio

class Convert:
    def __init__(self):
        self.stream = None
        self.writer = io.BytesIO()
        self.audioWriter = io.BytesIO()
        self.known_lengths = {}
        self.addedChunks = 0
        self.lengthLastCalculatedAtChunk = 0

    # cuts and saves the video
    async def save(self, fileLocation, fileLength, method="ffmpeg"):
        if method == "ffmpeg":
            tempVideoFileLocation = fileLocation + ".ts"
            async with aiofiles.open(tempVideoFileLocation, "wb") as file:
                await file.write(self.writer.getvalue())
            tempAudioFileLocation = fileLocation + ".alaw"
            async with aiofiles.open(tempAudioFileLocation, "wb") as file:
                await file.write(self.audioWriter.getvalue())

            cmd = 'ffmpeg -ss 00:00:00 -i "{inputVideoFile}" -f alaw -ar 8000 -i "{inputAudioFile}" -t {videoLength} -y -c:v copy -c:a aac -map 0:v:0 -map 1:a:0 "{outputFile}" >{devnull} 2>&1'.format(
                inputVideoFile=tempVideoFileLocation,
                inputAudioFile=tempAudioFileLocation,
                outputFile=fileLocation,
                videoLength=str(datetime.timedelta(seconds=fileLength)),
                devnull=os.devnull,
            )
            os.system(cmd)

            os.remove(tempVideoFileLocation)
            os.remove(tempAudioFileLocation)
        else:
            raise Exception("Method not supported")

    # calculates ideal refresh interval for a real time estimate of downloaded data
    def getRefreshIntervalForLengthEstimate(self):
        if self.addedChunks < 100:
            return 50
        elif self.addedChunks < 1000:
            return 250
        elif self.addedChunks < 10000:
            return 5000
        else:
            return self.addedChunks / 2

    # calculates real stream length, hard on processing since it has to go through all the frames
    def calculateLength(self):
        detectedLength = False
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(self.writer.getvalue())
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "fatal",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        tmp.name,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                detectedLength = float(result.stdout)
                self.known_lengths[self.addedChunks] = detectedLength
                self.lengthLastCalculatedAtChunk = self.addedChunks
            os.unlink(tmp.name)
        except Exception as e:
            print("")
            print(e)
            print("Warning: Could not calculate length from stream.")
            pass
        return detectedLength

    # returns length of video, can return an estimate which is usually very close
    def getLength(self, exact=False):
        if bool(self.known_lengths) is True:
            lastKnownChunk = list(self.known_lengths)[-1]
            lastKnownLength = self.known_lengths[lastKnownChunk]
        if (
            exact
            or not self.known_lengths
            or self.addedChunks
            > self.lengthLastCalculatedAtChunk
            + self.getRefreshIntervalForLengthEstimate()
            or lastKnownLength == 0
        ):
            calculatedLength = self.calculateLength()
            if calculatedLength is not False:
                return calculatedLength
            else:
                if bool(self.known_lengths) is True:
                    bytesPerChunk = lastKnownChunk / lastKnownLength
                    return self.addedChunks / bytesPerChunk
        else:
            bytesPerChunk = lastKnownChunk / lastKnownLength
            return self.addedChunks / bytesPerChunk
        return False

    def write(self, data: bytes, audioData: bytes):
        self.addedChunks += 1
        return self.writer.write(data) and self.audioWriter.write(audioData)

    async def generate_thumbnail(self, video_path, thumbnail_path, height=100, time_percentage=0.1, quality=2, max_retries=3):
        for attempt in range(max_retries + 1):
            try:
                # Ensure thumbnail directory exists
                thumbnail_dir = os.path.dirname(thumbnail_path)
                os.makedirs(thumbnail_dir, exist_ok=True)
                os.chmod(thumbnail_dir, 0o777)

                # Default thumbnail time (fallback)
                thumbnail_time = 5.0

                # Attempt to probe video duration
                try:
                    result = subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "fatal",
                            "-show_entries",
                            "format=duration",
                            "-of",
                            "default=noprint_wrappers=1:nokey=1",
                            video_path,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    duration = float(result.stdout)
                    thumbnail_time = duration * time_percentage
                except Exception as e:
                    print(f"Warning: Failed to probe video duration for {video_path}. Using fallback thumbnail time of 5 seconds. Error: {str(e)}")
                    # Fallback to 5 seconds already set in thumbnail_time

                # Construct FFmpeg command for thumbnail generation
                cmd = (
                    f'ffmpeg -ss {thumbnail_time} -i "{video_path}" -vframes 1 -vf scale=-1:{height} '
                    f'-q:v {quality} -f image2 "{thumbnail_path}" -y >{os.devnull} 2>&1'
                )

                # Execute FFmpeg command
                retcode = os.system(cmd)
                if retcode != 0:
                    raise Exception(f"FFmpeg failed with return code {retcode}")

                # Verify thumbnail was created
                if not os.path.exists(thumbnail_path):
                    raise Exception("Thumbnail file was not created")

                return
            except Exception as e:
                error_msg = str(e)
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
                raise Exception(f"Failed to generate thumbnail after {max_retries} attempts: {error_msg}")