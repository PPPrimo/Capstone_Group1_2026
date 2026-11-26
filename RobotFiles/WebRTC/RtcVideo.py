"""Grabs frames from the USB cameras and keeps a WebRTC video track going (Python 3.11+)"""
import asyncio
import logging
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

logger = logging.getLogger(__name__)
CAMERA_SOURCE = 0
# Requirements want at least 640x480 per camera (k_resMin) and 20 fps (k_fpsMin)
# 24 fps gives some room for dropped frames and is 20% less encoding than 30
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480
VIDEO_FPS = 24
k_fpsMin = 20
k_videoStale = 1.0  # (s) frames older than this count as frozen video

# Bitrate budget per 640x480 camera, capping it keeps network queues short (k_videoLatency = 300 ms)
MAX_BITRATE_PER_CAMERA = 500_000
START_BITRATE_PER_CAMERA = 300_000
MIN_BITRATE_PER_CAMERA = 100_000


def limit_video_bitrate(cameras=1, maximum=None, start=None, minimum=None):
    """Caps aiortc's VP8/H.264 bitrate, only affects encoders made after this runs

    The per-camera budget gets multiplied by `cameras` since side-by-side cameras share one frame,
    so call it before RobotRTC.connect()
    aiortc has no setting for this, so we overwrite the limits its encoders read instead
    """
    maximum = maximum or MAX_BITRATE_PER_CAMERA * cameras
    start = start or START_BITRATE_PER_CAMERA * cameras
    minimum = minimum or MIN_BITRATE_PER_CAMERA * cameras
    if not 0 < minimum <= start <= maximum:
        raise ValueError("Need 0 < minimum <= start <= maximum")
    from aiortc.codecs import h264, vpx
    for codec in (vpx, h264):
        if all(hasattr(codec, name) for name in ("MIN_BITRATE", "DEFAULT_BITRATE", "MAX_BITRATE")):
            codec.MIN_BITRATE, codec.DEFAULT_BITRATE, codec.MAX_BITRATE = minimum, start, maximum
        else:
            logger.warning("aiortc %s has no bitrate constants; video bitrate is not capped", codec.__name__)
    logger.info("Video bitrate limited to %d-%d kbps", minimum // 1000, maximum // 1000)


limit_video_bitrate()


def find_usb_cameras(root="/sys/class/video4linux"):
    """Finds the plugged-in USB cameras and returns their /dev/videoN numbers, one per camera"""
    root = Path(root)
    if not root.is_dir():
        return []
    found = []
    for node in root.glob("video*"):
        try:
            if "/usb" not in str((node / "device").resolve()):
                continue
            if (node / "index").read_text().strip() != "0":  # 0 is the actual video stream, others are metadata
                continue
        except OSError:
            continue
        found.append(int(node.name.removeprefix("video")))
    return sorted(found)


class CameraSource:

    def __init__(self, source=CAMERA_SOURCE, width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS):
        self.source = source
        self.width, self.height, self.fps = width, height, fps
        self.name = f"USB camera {source}"
        self._camera = None
        self._m_scene = None
        self._m_sceneTime = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        self._camera = cv2.VideoCapture(self.source)
        if not self._camera.isOpened():
            self._camera = None
            raise RuntimeError("cannot open device")
        self._camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._camera.set(cv2.CAP_PROP_FPS, self.fps)
        logger.info("%s opened", self.name)

    def capture(self):
        """Grabs one frame at the configured size (BGR), or None if the read fails"""
        ok, frame = self._camera.read()
        if not ok:
            return None
        # Some cameras ignore the size we ask for, so shrink anything bigger
        if frame.shape[:2] != (self.height, self.width):
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        return frame

    def release(self):
        if self._camera is not None:
            self._camera.release()
            self._camera = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()
        return self

    def latest_frame(self):
        """Newest frame, or None if nothing came in during the last k_videoStale seconds"""
        with self._lock:
            if time.monotonic() - self._m_sceneTime > k_videoStale:
                return None
            return self._m_scene

    @property
    def ready(self):
        return self.latest_frame() is not None

    def _run(self):
        while not self._stop.is_set():
            try:
                self.open()
                while not self._stop.is_set():
                    frame = self.capture()
                    if frame is None:
                        raise RuntimeError("read failed")
                    with self._lock:
                        self._m_scene, self._m_sceneTime = frame, time.monotonic()
            except Exception as error:
                logger.warning("%s: %s; retrying in 1 s", self.name, error)
            finally:
                self.release()
            self._stop.wait(1.0)

    async def close(self):
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2)
        with self._lock:
            self._m_scene, self._m_sceneTime = None, 0.0


class SideBySide:

    def __init__(self, *cameras):
        self.cameras = cameras

    def latest_frame(self):
        frames = [camera.latest_frame() for camera in self.cameras]
        if all(frame is None for frame in frames):
            return None
        height = self.cameras[0].height
        panels = []
        for camera, frame in zip(self.cameras, frames):
            if frame is None:
                frame = np.zeros((height, camera.width, 3), dtype=np.uint8)
            elif frame.shape[:2] != (height, camera.width):
                frame = cv2.resize(frame, (camera.width, height), interpolation=cv2.INTER_AREA)
            panels.append(frame)
        return np.hstack(panels)


class SwitchableVideoTrack(VideoStreamTrack):
    """One video track that shows a placeholder until the source has fresh frames

    A source just needs a nonblocking latest_frame() that gives a BGR array or None
    Camera frames go out at their own size, width/height only size the placeholder
    The track never closes the source, whoever made it handles that
    Swap the source from the RTC event loop, no SDP renegotiation needed
    """
    def __init__(self, source=None, width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS):
        super().__init__()
        if width <= 0 or height <= 0 or width % 2 or height % 2 or not 0 < fps <= 120:
            raise ValueError("Use positive even dimensions and FPS between 0 and 120")
        self.source = source
        self.width, self.height, self.fps = width, height, fps
        self._placeholder = None
        self._placeholder_text = None
        self._start = None
        self._index = 0

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError

        now = time.monotonic()
        if self._start is None:
            self._start = now
        self._index = max(self._index, int((now - self._start) * self.fps))
        await asyncio.sleep(max(0, self._start + self._index / self.fps - now))

        if self.readyState != "live":
            raise MediaStreamError

        image = self.source.latest_frame() if self.source is not None else None

        # No fresh frame, so show something nobody will mistake for live video (hazard H-2)
        if image is None:
            image = self._placeholder_frame()

        frame = VideoFrame.from_ndarray(image, format="bgr24").reformat(format="yuv420p")
        frame.pts = round(self._index * 90000 / self.fps)
        frame.time_base = Fraction(1, 90000)

        self._index += 1
        return frame

    def _placeholder_frame(self):
        text = datetime.now().strftime("%H:%M:%S")
        if text != self._placeholder_text:
            image = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            for line, y in (("NO VIDEO", self.height // 2 - 20), (text, self.height // 2 + 30)):
                cv2.putText(image, line, (20, y), cv2.FONT_HERSHEY_SIMPLEX,
                            1.0, (255, 255, 255), 2, cv2.LINE_AA)
            self._placeholder, self._placeholder_text = image, text
        return self._placeholder
'''
example usage:
    camera = CameraSource().start()
    rtc = RobotRTC(video_source=camera)
    await rtc.connect()
two USB cameras in one video (each panel stays 640x480):
    limit_video_bitrate(cameras=2)
    front = CameraSource(source=0).start()
    side = CameraSource(source=2).start()
    rtc = RobotRTC(video_source=SideBySide(front, side))
terminate:
    await rtc.close()
    await front.close()
    await side.close()
'''
