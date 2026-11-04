"""Shared Feetech motor communication for the leader and follower arms."""

from threading import RLock

from serial.tools import list_ports

from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.robots.utils import ensure_safe_goal_position
from lerobot.utils.decorators import check_if_not_connected


class MotorDriver:

    def __init__(self, port, *, motors, calibration=None, num_read_retries=0, max_relative_target=None):
        """Create a bus using the motor definitions supplied by the arm."""
        if not motors:
            raise ValueError("At least one motor must be defined for the bus")
        if port == "auto":
            port = self.find_port(motors)
        self.bus = FeetechMotorsBus(port=port, motors=motors, calibration=calibration)
        self.num_read_retries = num_read_retries
        self.max_relative_target = max_relative_target
        self._lock = RLock()

    @staticmethod
    def find_port(motors):
        """Find the unique port whose bus handshake matches the expected motors.

        Probing does not change motor settings or disable torque. Identical arms
        cannot be distinguished by their motor IDs/models; specify a port when
        more than one attached bus matches.
        """
        if not motors:
            raise ValueError("At least one motor must be defined for port discovery")

        matches = []
        failures = []
        for port in sorted(list_ports.comports(), key=lambda item: item.device):
            bus = FeetechMotorsBus(port=port.device, motors=motors)
            try:
                bus.connect()
            except (OSError, RuntimeError) as exc:
                failures.append(f"{port.device}: {exc}")
            else:
                matches.append(port.device)
            finally:
                # A failed handshake can leave the serial port open too.
                if bus.is_connected:
                    bus.disconnect(disable_torque=False)

        if not matches:
            details = "; ".join(failures) or "No serial ports were detected."
            raise ConnectionError(f"No serial port matched the configured motors. {details}")

        return matches

    @property
    def is_connected(self):
        return self.bus.is_connected

    @property
    def is_calibrated(self):
        return self.bus.is_calibrated

    @property
    def action_features(self):
        return {f"{motor}.pos": float for motor in self.bus.motors}

    def connect(self):
        with self._lock:
            self.bus.connect()

    @check_if_not_connected
    def disconnect(self, disable_torque=True):
        with self._lock:
            self.bus.disconnect(disable_torque)

    @check_if_not_connected
    def _read(self, register):
        with self._lock:
            return self.bus.sync_read(register, num_retry=self.num_read_retries)

    def get_position(self):
        return self._read("Present_Position")

    def get_velocity(self):
        """Read every motor's velocity in native Feetech register units."""
        return self._read("Present_Velocity")

    def get_acceleration(self):
        """Read every motor's configured acceleration, not measured acceleration."""
        return self._read("Acceleration")

    def get_current(self):
        return self._read("Present_Current")

    @check_if_not_connected
    def set_position(self, positions):
        """Write targets keyed by motor name; caller applies movement limits."""
        with self._lock:
            self.bus.sync_write("Goal_Position", positions)

    def get_action(self):
        """Read all bus positions using LeRobot's ``<motor>.pos`` keys."""
        return {f"{motor}.pos": value for motor, value in self.get_position().items()}

    @check_if_not_connected
    def send_action(self, action):
        """Send position targets, applying the arm's configured relative limit."""
        positions = {key.removesuffix(".pos"): value for key, value in action.items() if key.endswith(".pos")}
        if positions:
            if self.max_relative_target is not None:
                present = self.get_position()
                positions = ensure_safe_goal_position(
                    {motor: (goal, present[motor]) for motor, goal in positions.items()},
                    self.max_relative_target,
                )
            self.set_position(positions)
        return {f"{motor}.pos": value for motor, value in positions.items()}

    @check_if_not_connected
    def send_velocity(self, velocities):
        """Write targets keyed by motor name in native Feetech velocity units.

        The arm owns operating-mode configuration. In position mode this register
        controls movement speed; constant-speed operation requires velocity mode.
        """
        with self._lock:
            self.bus.sync_write("Goal_Velocity", velocities)
        return dict(velocities)

    def setup_motors(self):
        """Assign the configured IDs with one motor connected at a time."""
        for motor in reversed(self.bus.motors):
            input(f"Connect the controller board to the '{motor}' motor only and press enter.")
            self.bus.setup_motor(motor)
            print(f"'{motor}' motor id set to {self.bus.motors[motor].id}")
