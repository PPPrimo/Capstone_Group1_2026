from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

import cv2

from RobotFiles.WebRTC import CameraSource, SideBySide
from RobotFiles.WebRTC.RtcVideo import k_fpsMin


async def run(cameras: list[CameraSource], seconds: float, output: Path) -> bool:
    for camera in cameras:
        camera.start()
    try:
        deadline = time.monotonic() + 5.0
        while not all(camera.ready for camera in cameras) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        # Count distinct frames per camera to measure the real capture rate.
        counts = [0] * len(cameras)
        last = [None] * len(cameras)
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            for i, camera in enumerate(cameras):
                frame = camera.latest_frame()
                if frame is not None and frame is not last[i]:
                    counts[i] += 1
                    last[i] = frame
            await asyncio.sleep(0.002)

        ok = True
        for camera, frame, count in zip(cameras, last, counts):
            if frame is None:
                print(f"FAIL {camera.name}: no frames")
                ok = False
            else:
                fps = count / seconds
                status = "OK  " if fps >= k_fpsMin else "FAIL"
                ok = ok and fps >= k_fpsMin
                # Brightness: 0 = black, 255 = white; a normally lit room is roughly 80-150.
                print(f"{status} {camera.name}: {frame.shape}, ~{fps:.1f} fps (min {k_fpsMin}), brightness {frame.mean():.0f}/255")

        picture = SideBySide(*cameras).latest_frame()
        if picture is not None:
            cv2.imwrite(str(output), picture)
            print(f"Saved snapshot to {output.resolve()}")
        return ok
    finally:
        for camera in cameras:
            await camera.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test camera capture")
    parser.add_argument("--source", nargs="+", default=["0"], help="USB camera indexes (0, 1, ...) or device paths")
    parser.add_argument("--seconds", type=float, default=3.0, help="How long to measure FPS")
    parser.add_argument("--output", type=Path, default=Path("snapshot.jpg"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cameras = [CameraSource(source=int(source) if source.isdigit() else source) for source in args.source]
    ok = asyncio.run(run(cameras, args.seconds, args.output))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
