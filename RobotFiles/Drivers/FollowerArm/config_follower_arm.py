

from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig

from lerobot.robots.config import RobotConfig


@dataclass
class FollowerArmConfig:
    """Base configuration class for SO Follower robots."""

    # Port to connect to the arm
    port: str

    disable_torque_on_disconnect: bool = True

    # `max_relative_target` limits the magnitude of the relative positional target vector for safety purposes.
    # Set this to a positive scalar to have the same value for all motors, or a dictionary that maps motor
    # names to the max_relative_target value for that motor.
    max_relative_target: float | dict[str, float] | None = None

    # cameras
    cameras: dict[str, CameraConfig] = field(default_factory=dict)

    # Set to `True` for backward compatibility with previous policies/dataset
    use_degrees: bool = True

    # Position-mode PID gains written to Feetech STS3215 motors at connect time.
    position_p_coefficient: int = 16
    position_i_coefficient: int = 0
    position_d_coefficient: int = 32

    # Number of extra attempts when a `sync_read` of the motors fails. Feetech buses can occasionally
    # return a corrupted status packet ("Incorrect status packet!"), especially when several joints move
    # at once, which otherwise aborts the control loop. Retries are immediate (no sleep) and only happen on
    # failure, so the steady-state read cost is unchanged.
    num_read_retries: int = 2


@RobotConfig.register_subclass("follower_arm")
@dataclass
class FollowerRobotConfig(RobotConfig, FollowerArmConfig):
    pass
