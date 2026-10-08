from __future__ import annotations

import argparse
import time
from types import TracebackType
from typing import Literal, Self

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode


RotationMode = Literal["speed", "degree", "step"]


class STS3215Motor:
    """Own one STS3215 connection and provide three rotation modes."""

    STEPS_PER_REVOLUTION = 4096
    MIN_SIGNED_VALUE = -0x7FFF
    MAX_SIGNED_VALUE = 0x7FFF

    def __init__(
        self,
        port: str,
        motor_id: int,
        *,
        motor_name: str = "motor",
        motor_model: str = "sts3215",
    ) -> None:
        self.port = port
        self.motor_id = int(motor_id)
        self.motor_name = motor_name
        self.bus = FeetechMotorsBus(
            port=port,
            motors={
                motor_name: Motor(
                    id=self.motor_id,
                    model=motor_model,
                    norm_mode=MotorNormMode.RANGE_M100_100,
                )
            },
        )

    @property
    def is_connected(self) -> bool:
        return self.bus.is_connected

    def connect(self) -> None:
        if not self.bus.is_connected:
            self.bus.connect()

    def disconnect(self) -> None:
        if not self.bus.is_connected:
            return

        try:
            self.stop()
        finally:
            self.bus.disconnect(disable_torque=True)

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.disconnect()

    @classmethod
    def degrees_to_steps(cls, degrees: float) -> int:
        return round(float(degrees) * cls.STEPS_PER_REVOLUTION / 360.0)

    @classmethod
    def steps_to_degrees(cls, steps: int) -> float:
        return float(steps) * 360.0 / cls.STEPS_PER_REVOLUTION

    @classmethod
    def _validate_signed_value(cls, value: int, label: str) -> int:
        value = int(value)
        if not cls.MIN_SIGNED_VALUE <= value <= cls.MAX_SIGNED_VALUE:
            raise ValueError(
                f"{label}={value} is outside the STS3215 signed range "
                f"[{cls.MIN_SIGNED_VALUE}, {cls.MAX_SIGNED_VALUE}]."
            )
        return value

    def _ensure_connected(self) -> None:
        if not self.bus.is_connected:
            self.connect()

    def _set_operating_mode(self, target_mode: OperatingMode) -> None:
        """Change EEPROM mode only when required, then enable torque."""
        self._ensure_connected()
        current_mode = self.bus.read(
            "Operating_Mode",
            self.motor_name,
            normalize=False,
        )

        if current_mode != target_mode.value:
            self.bus.disable_torque(self.motor_name)
            self.bus.write(
                "Operating_Mode",
                self.motor_name,
                target_mode.value,
                normalize=False,
            )

        self.bus.enable_torque(self.motor_name)

    def stop(self) -> None:
        """Stop velocity motion. Safe to call during cleanup."""
        if not self.bus.is_connected:
            return

        try:
            operating_mode = self.bus.read(
                "Operating_Mode",
                self.motor_name,
                normalize=False,
            )
            if operating_mode == OperatingMode.VELOCITY.value:
                self.bus.write(
                    "Goal_Velocity",
                    self.motor_name,
                    0,
                    normalize=False,
                )
        except Exception:
            # Cleanup must still proceed to torque disable/disconnect.
            pass

    def _wait_until_stopped(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive.")

        # Give the servo time to transition from idle to moving.
        time.sleep(0.03)
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            moving = self.bus.read(
                "Moving",
                self.motor_name,
                normalize=False,
            )
            if moving == 0:
                return
            time.sleep(0.02)

        self.bus.disable_torque(self.motor_name)
        raise TimeoutError(
            f"Motor ID {self.motor_id} did not finish within {timeout_s:.2f}s. "
            "Torque was disabled."
        )

    def rotate(
        self,
        mode: RotationMode,
        value: float,
        *,
        duration_s: float = 1.0,
        movement_speed: int = 300,
        timeout_s: float = 10.0,
    ) -> None:
        """Rotate using raw speed, relative degrees, or relative steps.

        Args:
            mode: ``"speed"``, ``"degree"``, or ``"step"``.
            value: Signed raw velocity, relative degrees, or relative steps.
            duration_s: Speed-mode run duration before an automatic stop.
            movement_speed: Positive raw speed for degree/step movements.
            timeout_s: Maximum wait for degree/step completion.
        """
        normalized_mode = str(mode).lower()

        if normalized_mode == "speed":
            raw_speed = self._validate_signed_value(round(value), "speed")
            if duration_s <= 0:
                raise ValueError("duration_s must be positive in speed mode.")

            self._set_operating_mode(OperatingMode.VELOCITY)
            try:
                self.bus.write(
                    "Goal_Velocity",
                    self.motor_name,
                    raw_speed,
                    normalize=False,
                )
                time.sleep(duration_s)
            finally:
                self.bus.write(
                    "Goal_Velocity",
                    self.motor_name,
                    0,
                    normalize=False,
                )
            return

        if normalized_mode == "degree":
            relative_steps = self.degrees_to_steps(value)
        elif normalized_mode == "step":
            relative_steps = round(value)
        else:
            raise ValueError(
                f"Unknown mode {mode!r}; expected 'speed', 'degree', or 'step'."
            )

        relative_steps = self._validate_signed_value(relative_steps, "steps")
        if relative_steps == 0:
            return

        movement_speed = abs(
            self._validate_signed_value(int(movement_speed), "movement_speed")
        )
        if movement_speed == 0:
            raise ValueError("movement_speed must be greater than zero.")

        self._set_operating_mode(OperatingMode.STEP)
        self.bus.write(
            "Goal_Velocity",
            self.motor_name,
            movement_speed,
            normalize=False,
        )
        self.bus.write(
            "Goal_Position",
            self.motor_name,
            relative_steps,
            normalize=False,
        )
        self._wait_until_stopped(timeout_s)

    def rotate_speed(self, speed: int, *, duration_s: float = 1.0) -> None:
        self.rotate("speed", speed, duration_s=duration_s)

    def rotate_degrees(
        self,
        degrees: float,
        *,
        movement_speed: int = 300,
        timeout_s: float = 10.0,
    ) -> None:
        self.rotate(
            "degree",
            degrees,
            movement_speed=movement_speed,
            timeout_s=timeout_s,
        )

    def rotate_steps(
        self,
        steps: int,
        *,
        movement_speed: int = 300,
        timeout_s: float = 10.0,
    ) -> None:
        self.rotate(
            "step",
            steps,
            movement_speed=movement_speed,
            timeout_s=timeout_s,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rotate one STS3215 using LeRobot speed, degree, or step mode."
    )
    parser.add_argument("--port", required=True, help="Motor bus port, e.g. COM5")
    parser.add_argument("--id", required=True, type=int, help="STS3215 motor ID")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("speed", "degree", "step"),
    )
    parser.add_argument("--value", required=True, type=float)
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--movement-speed", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    with STS3215Motor(args.port, args.id) as motor:
        motor.rotate(
            args.mode,
            args.value,
            duration_s=args.duration,
            movement_speed=args.movement_speed,
            timeout_s=args.timeout,
        )


if __name__ == "__main__":
    main()