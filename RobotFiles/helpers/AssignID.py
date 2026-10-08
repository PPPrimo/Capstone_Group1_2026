import argparse

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--new-id", type=int, required=True)
    args = parser.parse_args()

    motors = {
        "chassis_motor": Motor(
            id=args.new_id,
            model="sts3215",
            norm_mode=MotorNormMode.RANGE_M100_100,
        )
    }

    bus = FeetechMotorsBus(port=args.port, motors=motors)

    try:
        # Automatically finds the one connected motor's current ID and baud rate,
        # then changes them to the values expected by `motors`.
        bus.setup_motor("chassis_motor")
        print(f"Motor successfully configured as ID {args.new_id}")
    finally:
        if bus.is_connected:
            bus.disconnect(disable_torque=False)


if __name__ == "__main__":
    main()