"""Check that CameraSource captures real frames, without WebRTC.

Run from the repo root:
    python -m RobotFiles.helpers.CameraTester                      # USB camera 0
    python -m RobotFiles.helpers.CameraTester --source 2           # another /dev/videoN
    python -m RobotFiles.helpers.CameraTester --backend picamera2  # Pi ribbon camera
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

import cv2

from RobotFiles.WebRTC import CameraSource


async def run(source, backend: str, seconds: float, output: Path) -> bool:
    camera = CameraSource(source=source, backend=backend).start()
    try:
        deadline = time.monotonic() + 5.0
        while not camera.ready and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not camera.ready:
            print("FAIL: no frame within 5 s")
            return False

        # Count distinct frames to measure the real capture rate.
        frames = 0
        last = None
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            frame = camera.latest_frame()
            if frame is not None and frame is not last:
                frames += 1
                last = frame
            await asyncio.sleep(0.002)

        print(f"OK: frame shape {last.shape}, dtype {last.dtype}, ~{frames / seconds:.1f} fps")
        cv2.imwrite(str(output), last)
        print(f"Saved snapshot to {output.resolve()}")
        return True
    finally:
        await camera.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test CameraSource capture")
    parser.add_argument("--source", default="0", help="Camera index (0, 1, ...) or stream URL")
    parser.add_argument("--backend", default="opencv", choices=["opencv", "picamera2"])
    parser.add_argument("--seconds", type=float, default=3.0, help="How long to measure FPS")
    parser.add_argument("--output", type=Path, default=Path("snapshot.jpg"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    source = int(args.source) if args.source.isdigit() else args.source
    ok = asyncio.run(run(source, args.backend, args.seconds, args.output))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
