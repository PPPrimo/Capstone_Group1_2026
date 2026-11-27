// Freezes the browser's leader arm (Web Serial) while the robot's follower is stopped by its collision guard.
// Same behaviour as LeaderLock in RobotFiles/Drivers/LeaderArm/LeaderLock.py, which does this for the Python leader:
//   - every leader joint except the tripped one is held where it was at the trip (torque on, goal = that pose)
//   - the tripped joint gets a one-way wall at its trip angle: pushing further toward the obstacle switches its
//     torque on so it springs back to the wall; moving the other way is free
//   - when the robot leaves "stopped", every joint goes limp again
// Safeguards: holding strength capped at torquePct (Torque_Limit is a RAM register, restored by close()),
// and a held joint lets go if it is forced hard (over k_giveWayMa) or leaned on for k_leanS seconds.
//
// update() must be called from the same loop that reads the leader (stream() in leader_serial.js),
// because the serial link can only handle one request at a time.

// Must match MOTOR_NAMES in leader_serial.js: joint name -> servo ID 1..6.
const MOTOR_NAMES = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper'];

// STS3215 registers used here.
const REG_TORQUE_ENABLE = 40;     // 1 byte: 1 = torque on, 0 = limp
const REG_GOAL_POSITION = 42;     // 2 bytes
const REG_TORQUE_LIMIT = 48;      // 2 bytes, 0-1000 (1000 = 100%)
const REG_PRESENT_POSITION = 56;  // 2 bytes
const REG_PRESENT_CURRENT = 69;   // 2 bytes
const READ = 2;
const WRITE = 3;

const k_maPerCount = 6.5;   // mA per Present_Current count
const k_onDeg = 0.5;        // the wall switches on this far past it
const k_offDeg = 0.5;       // and off this far back on the free side
const k_pushMa = 150;       // current that counts as someone leaning on a held joint
const k_leanS = 3.0;        // seconds of leaning before a held joint lets go
const k_giveWayMa = 700;    // current at which a held joint lets go at once
const k_avgN = 3;           // readings averaged; the browser loop runs at ~10 Hz, so this is ~0.3 s

function toBytes(value) {
    // A 2-byte register value, low byte first.
    return [value & 255, (value >> 8) & 255];
}

function fromBytes(bytes) {
    return bytes[0] | (bytes[1] << 8);
}

export class LeaderLock {
    constructor(device, torquePct = 50) {
        this.device = device;           // the SerialLeader from leader_serial.js
        this.torquePct = torquePct;     // holding strength while locked, % of max
        this.limits = null;             // original Torque_Limit per joint, restored by close()
        this.joint = null;              // tripped joint while locked, else null
        this.sign = 0;                  // +1/-1: blocked direction of the tripped joint
        this.wallDeg = null;            // wall angle in degrees (compared with getPositions())
        this.wallRaw = null;            // the same angle as a raw servo value (written as the goal)
        this.engaged = new Set();       // joints with torque on
        this.letGo = new Set();         // joints released by give-way / lean timeout
        this.leanSince = {};            // joint -> time someone started leaning on it
        this.history = {};              // recent current readings per joint
        for (const m of MOTOR_NAMES) {
            this.history[m] = [];
        }
    }

    id(m) {
        return MOTOR_NAMES.indexOf(m) + 1;
    }

    async start() {
        // Remember each joint's Torque_Limit, then cap it at torquePct. Torque stays off until a stop.
        this.limits = {};
        for (const m of MOTOR_NAMES) {
            this.limits[m] = fromBytes(await this.device.request(this.id(m), READ, [REG_TORQUE_LIMIT, 2], 2));
        }
        for (const m of MOTOR_NAMES) {
            await this.device.request(this.id(m), WRITE, [REG_TORQUE_LIMIT, ...toBytes(Math.round(this.torquePct * 10))]);
        }
        return this;
    }

    async close() {
        // Leader limp again and the original limits back. Call before the serial port closes.
        await this.release();
        if (this.limits !== null) {
            for (const m of MOTOR_NAMES) {
                await this.device.request(this.id(m), WRITE, [REG_TORQUE_LIMIT, ...toBytes(this.limits[m])]);
            }
        }
    }

    async release() {
        // Every joint limp again.
        for (const m of this.engaged) {
            await this.device.request(this.id(m), WRITE, [REG_TORQUE_ENABLE, 0]);
        }
        if (this.joint !== null) {
            console.info('Leader unlocked');
        }
        this.engaged.clear();
        this.letGo.clear();
        this.leanSince = {};
        for (const m of MOTOR_NAMES) {
            this.history[m] = [];
        }
        this.joint = null;
    }

    async rawPosition(m) {
        // Present_Position as a raw servo value (bit 15 is the sign).
        const raw = fromBytes(await this.device.request(this.id(m), READ, [REG_PRESENT_POSITION, 2], 2));
        if (raw & 0x8000) {
            return -(raw & 0x7fff);
        }
        return raw;
    }

    async currentMa(m) {
        // Present_Current in mA. Bit 15 is the direction; only the size matters.
        const raw = fromBytes(await this.device.request(this.id(m), READ, [REG_PRESENT_CURRENT, 2], 2));
        return (raw & 0x7fff) * k_maPerCount;
    }

    async hold(motors, goalsRaw) {
        // Torque on for these joints, each held at its raw goal. Goal first, so nothing jumps.
        for (const m of motors) {
            let goal = goalsRaw[m];
            if (goal < 0) {
                goal = (-goal & 0x7fff) | 0x8000;
            }
            await this.device.request(this.id(m), WRITE, [REG_GOAL_POSITION, ...toBytes(goal)]);
            await this.device.request(this.id(m), WRITE, [REG_TORQUE_ENABLE, 1]);
            this.engaged.add(m);
        }
    }

    async free(m, reason = null, letGo = false) {
        // Torque off for one joint.
        await this.device.request(this.id(m), WRITE, [REG_TORQUE_ENABLE, 0]);
        this.engaged.delete(m);
        this.history[m] = [];
        delete this.leanSince[m];
        if (letGo) {
            this.letGo.add(m);
        }
        if (reason !== null) {
            console.warn(`Leader ${m} released: ${reason}`);
        }
    }

    async update(status, positions) {
        // status: the robot's guard status from telemetry, or null. positions: getPositions() in degrees.

        // 1. Not stopped: make sure the leader is free, then nothing else to do.
        let stopped = false;
        if (status && status.state === 'stopped' && status.joint) {
            stopped = true;
        }
        if (!stopped) {
            if (this.joint !== null) {
                await this.release();
            }
            return;
        }

        // 2. A new stop: hold every joint except the tripped one where it is now.
        if (status.joint !== this.joint) {
            await this.release();
            this.joint = status.joint;
            this.sign = 1;
            if (status.direction === -1) {
                this.sign = -1;
            }
            this.wallDeg = positions[this.joint];
            this.wallRaw = await this.rawPosition(this.joint);
            const others = [];
            const goals = {};
            for (const m of MOTOR_NAMES) {
                if (m !== this.joint) {
                    others.push(m);
                    goals[m] = await this.rawPosition(m);
                }
            }
            await this.hold(others, goals);
            console.warn(`Leader locked: only ${this.joint} can move, and only away from the collision`);
        }

        // 3. Safety: let a held joint go if it is forced hard, or leaned on for too long.
        const now = performance.now() / 1000;
        for (const m of [...this.engaged]) {   // a copy, because free() removes joints from engaged
            this.history[m].push(await this.currentMa(m));
            if (this.history[m].length > k_avgN) {
                this.history[m].shift();       // keep only the most recent readings
            }
            let sum = 0;
            for (const value of this.history[m]) {
                sum = sum + value;
            }
            const average = sum / this.history[m].length;

            if (average > k_giveWayMa) {
                await this.free(m, `gave way at ${Math.round(average)} mA`, true);
            } else {
                if (average > k_pushMa) {
                    if (!(m in this.leanSince)) {
                        this.leanSince[m] = now;    // leaning started now
                    }
                } else {
                    this.leanSince[m] = now;        // not leaning: restart the clock
                }
                if (now - this.leanSince[m] > k_leanS) {
                    await this.free(m, `pushed for ${k_leanS} s`, true);
                }
            }
        }

        // 4. One-way wall on the tripped joint.
        const j = this.joint;
        const past = (positions[j] - this.wallDeg) * this.sign;   // > 0 = pushed further toward the obstacle
        if (this.letGo.has(j) && past < -k_offDeg) {
            this.letGo.delete(j);   // back on the free side: the wall re-arms
        }
        if (this.engaged.has(j)) {
            if (past < -k_offDeg) {
                await this.free(j);   // moved back to the free side
            }
        } else if (!this.letGo.has(j) && past > k_onDeg) {
            const goal = {};
            goal[j] = this.wallRaw;
            await this.hold([j], goal);   // pushed past the wall: spring back to it
        }
    }
}
