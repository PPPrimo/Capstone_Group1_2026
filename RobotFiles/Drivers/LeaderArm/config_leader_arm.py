

from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig

@dataclass
class LeaderArmConfig:
    """Base configuration class for SO Leader teleoperators."""

    # Port to connect to the arm
    port: str

    # Whether to use degrees for angles
    use_degrees: bool = True

    # Number of extra attempts when a `sync_read` of the motors fails. Feetech buses can occasionally
    # return a corrupted status packet ("Incorrect status packet!"), especially when several joints move
    # at once, which otherwise aborts the teleoperation loop. Retries are immediate (no sleep) and only
    # happen on failure, so the steady-state read cost is unchanged.
    num_read_retries: int = 2


@TeleoperatorConfig.register_subclass("leader_arm")
@dataclass
class LeaderTeleopConfig(TeleoperatorConfig, LeaderArmConfig):
    pass
