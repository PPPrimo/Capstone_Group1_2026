"""Camera capture and a continuous RTC video track (Python 3.11+).


"""
import asyncio
import logging
import threading
import time
from datetime import datetime
from fractions import Fraction

import cv2
import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

logger = logging.getLogger(__name__)
CAMERA_SOURCE = 0
CAMERA_BACKEND = "opencv"  # "picamera2" for the Raspberry Pi ribbon camera.
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480
VIDEO_FPS = 30
STALE_AFTER = 1.0  # Seconds before an old frame is treated as missing.


class CameraSource:

    def __init__(self, source=CAMERA_SOURCE, backend=CAMERA_BACKEND, width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS):
        if backend not in ("opencv", "picamera2"):
            raise ValueError("Camera backend must be opencv or picamera2")
        self.source, self.backend = source, backend
        self.width, self.height, self.fps = width, height, fps
        self.name = "ribbon camera" if backend == "picamera2" else f"USB camera {source}"
        self._camera = None
        self._frame = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        if self.backend == "picamera2":
            from picamera2 import Picamera2  # Only present on Raspberry Pi OS.
            self._camera = Picamera2()
            # Picamera2's "RGB888" is stored in BGR order, which is what OpenCV and aiortc expect.
            self._camera.configure(self._camera.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls={"FrameRate": self.fps},
            ))
            self._camera.start()
        else:
            self._camera = cv2.VideoCapture(self.source)
            if not self._camera.isOpened():
                self._camera = None
                raise RuntimeError("cannot open device")
            self._camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self._camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self._camera.set(cv2.CAP_PROP_FPS, self.fps)
        logger.info("%s opened", self.name)

    def capture(self):
        """Grab one frame from the open camera; returns a BGR uint8 numpy array, or None on failure."""
        if self.backend == "picamera2":
            return self._camera.capture_array()
        ok, frame = self._camera.read()
        return frame if ok else None

    def release(self):
        if self._camera is None:
            return
        if self.backend == "picamera2":
            self._camera.stop()
            self._camera.close()
        else:
            self._camera.release()
        self._camera = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()
        return self

    def latest_frame(self):
        """Return the newest frame, or None if there is no frame from the last STALE_AFTER seconds."""
        with self._lock:
            if time.monotonic() - self._frame_time > STALE_AFTER:
                return None
            return self._frame

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
                        self._frame, self._frame_time = frame, time.monotonic()
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
            self._frame, self._frame_time = None, 0.0


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
                frame = cv2.resize(frame, (camera.width, height))
            panels.append(frame)
        return np.hstack(panels)


class SwitchableVideoTrack(VideoStreamTrack):
    """One negotiated track: a timestamp placeholder until the supplied source has fresh video.

    Sources implement nonblocking latest_frame() -> BGR uint8 ndarray | None.
    Camera frames are sent at their own size; width/height only set the placeholder's size.
    This track does not own or close the source; the caller manages its lifetime.
    Set the source from the RTC event loop. No SDP renegotiation is required.
    """
    def __init__(self, source=None, width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS):
        super().__init__()
        if width <= 0 or height <= 0 or width % 2 or height % 2 or not 0 < fps <= 120:
            raise ValueError("Use positive even dimensions and FPS between 0 and 120")
        self.source = source
        self.width, self.height, self.fps = width, height, fps
        self._blank = np.zeros((height, width, 3), dtype=np.uint8)
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

        # Show the timestamp when no fresh camera frame is available.
        if image is None:
            image = self._blank.copy()
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            cv2.putText(image, timestamp, (20, self.height // 2), cv2.FONT_HERSHEY_SIMPLEX,
                        1.0, (255, 255, 255), 2, cv2.LINE_AA)

        frame = VideoFrame.from_ndarray(image, format="bgr24").reformat(format="yuv420p")
        frame.pts = round(self._index * 90000 / self.fps)
        frame.time_base = Fraction(1, 90000)

        self._index += 1
        return frame
'''
example usage:
    camera = CameraSource().start()
    rtc = RobotRTC(video_source=camera)
    await rtc.connect()
both cameras in one video:
    usb = CameraSource(source=0, backend="opencv").start()
    ribbon = CameraSource(backend="picamera2").start()
    rtc = RobotRTC(video_source=SideBySide(usb, ribbon))
terminate:
    await rtc.close()
    await usb.close()
    await ribbon.close()
'''
