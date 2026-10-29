"""Camera capture and a continuous RTC video track (Python 3.11+).

OpenCV: USB webcams on Windows/Linux, V4L2 cameras, or a stream URL.
Picamera2: optional Raspberry Pi CSI backend, installed through Raspberry Pi OS.
No camera is opened on import. Run this file to connect RTC with camera capture.
"""
import asyncio
import logging
import threading
import time
from fractions import Fraction

import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

import cv2
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
CAMERA_SOURCE = 0
CAMERA_BACKEND = "opencv"  # "picamera2" for a Raspberry Pi CSI camera.
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 480
VIDEO_FPS = 30


class CameraSource:
    """Owns capture in one thread; latest_frame() never waits for the camera.

    A source is confirmed by a successfully captured frame, not just an open device.
    Failed capture is retried; frames older than stale_after become unavailable.
    Call start() explicitly and await close() when the source is no longer needed.
    """
    def __init__(self, source=CAMERA_SOURCE, backend=CAMERA_BACKEND, width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS, stale_after=1.0):
        if backend not in ("opencv", "picamera2"):
            raise ValueError("Camera backend must be opencv or picamera2")
        if width <= 0 or height <= 0 or fps <= 0 or stale_after <= 0:
            raise ValueError("Video dimensions, FPS and stale_after must be positive")
        self.source, self.backend = source, backend
        self.width, self.height, self.fps = width, height, fps
        self.stale_after = stale_after
        self._frame = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            if self._stop.is_set():
                raise RuntimeError("Previous camera capture is still stopping")
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture, name="robot-camera", daemon=True)
        self._thread.start()
        return self

    def latest_frame(self):
        """Return the latest BGR uint8 frame, or None while missing/stale"""
        with self._lock:
            if self._stop.is_set() or time.monotonic() - self._frame_time > self.stale_after:
                return None
            return self._frame

    @property
    def ready(self):
        return self.latest_frame() is not None

    def _open(self): # TODO: not reviewed 
        """Open the camera device; returns the handle, or None if it is unavailable."""
        if self.backend == "picamera2":
            from picamera2 import Picamera2  # Only present on Raspberry Pi OS.
            camera = Picamera2()
            # Picamera2's "RGB888" is stored in BGR order, which is what OpenCV/aiortc expect.
            camera.configure(camera.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls={"FrameRate": self.fps},
            ))
            camera.start()
            return camera

        camera = cv2.VideoCapture(self.source)
        if not camera.isOpened():
            camera.release()
            return None
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        camera.set(cv2.CAP_PROP_FPS, self.fps)
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Keep latency low: don't queue old frames.
        return camera

    def _read(self, camera):
        """Blocking read of one BGR uint8 frame, or None on failure."""
        if self.backend == "picamera2":
            return camera.capture_array()
        ok, frame = camera.read()
        return frame if ok else None

    def _release(self, camera):
        try:
            if self.backend == "picamera2":
                camera.stop()
                camera.close()
            else:
                camera.release()
        except Exception:
            logger.exception("Error releasing camera")

    def _capture(self): # TODO: not reviewed 
        """Capture thread: keep the newest frame in self._frame, reopening the camera on failure."""
        while not self._stop.is_set():
            try:
                camera = self._open()
            except ImportError:
                logger.error("picamera2 is not installed; use backend='opencv' or install python3-picamera2")
                return
            except Exception:
                logger.exception("Failed to open camera %r", self.source)
                camera = None
            if camera is None:
                logger.warning("Camera %r unavailable; retrying in 1 s", self.source)
                self._stop.wait(1.0)
                continue

            logger.info("Camera %r opened with %s backend", self.source, self.backend)
            try:
                while not self._stop.is_set():
                    frame = self._read(camera)
                    if frame is None:
                        logger.warning("Camera read failed; reopening")
                        break
                    with self._lock:
                        self._frame = frame
                        self._frame_time = time.monotonic()
            except Exception:
                logger.exception("Camera capture error; reopening")
            finally:
                self._release(camera)
            self._stop.wait(1.0)

    async def close(self):
        self._stop.set()
        with self._lock:
            self._frame = None
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2)
            if self._thread.is_alive():
                logger.warning("Camera driver is blocked; capture thread will exit when the driver returns")


class SwitchableVideoTrack(VideoStreamTrack):
    """One negotiated track: blank frames until the supplied source has fresh video.

    Sources implement nonblocking latest_frame() -> BGR uint8 ndarray | None.
    This track does not own or close the source; the caller manages its lifetime.
    Set the source from the RTC event loop. No SDP renegotiation is required.
    """
    def __init__(self, source=None, width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS):
        super().__init__()
        if width <= 0 or height <= 0 or width % 2 or height % 2 or not 0 < fps <= 120:
            raise ValueError("Use positive even dimensions and FPS between 0 and 120")
        self.source = source
        self.width, self.height, self.fps = width, height, fps
        self.using_placeholder = True
        self._blank = np.zeros((height, width, 3), dtype=np.uint8)
        self._start = None
        self._index = 0

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError

        now = time.monotonic()
        if self._start is None:
            self._start = now

        self._index = max(
            self._index,
            int((now - self._start) * self.fps),
        )
        await asyncio.sleep(
            max(0, self._start + self._index / self.fps - now)
        )

        if self.readyState != "live":
            raise MediaStreamError

        image = (
            self.source.latest_frame()
            if self.source is not None
            else None
        )

        # Show the timestamp when no fresh camera frame is available.
        if image is None:
            image = self._blank.copy()
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            cv2.putText(
                image,
                timestamp,
                (20, self.height // 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

        frame = VideoFrame.from_ndarray(image, format="bgr24")
        frame = frame.reformat(
            width=self.width,
            height=self.height,
            format="yuv420p",
        )
        frame.pts = round(self._index * 90000 / self.fps)
        frame.time_base = Fraction(1, 90000)

        self._index += 1
        return frame
'''
example usage:
    camera = CameraSource().start()
    rtc = RobotRTC(video_source=camera)
    await rtc.connect()
or add video later:
    rtc = RobotRTC(video_source=None)
    camera = CameraSource().start()
    await rtc.connect()
    rtc.set_video_source(camera)
terminate:
    await rtc.close()
    await camera.close()
'''


