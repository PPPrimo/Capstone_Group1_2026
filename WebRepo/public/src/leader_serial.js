const MOTOR_NAMES = ['shoulder_pan', 'shoulder_lift', 'elbow_flex', 'wrist_flex', 'wrist_roll', 'gripper'];
const word = (bytes, offset = 0) => bytes[offset] | (bytes[offset + 1] << 8);
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

export class LeaderSerialError extends Error {}

// Feetech STS3215 protocol 0, matching the Python leader's default degree mode.
export class SerialLeader {
    constructor(port) {
        this.port = port;
        this.buffer = [];
        this.ranges = [];
    }

    async connect() {
        await this.port.open({ baudRate: 1000000 });
        this.reader = this.port.readable.getReader();
        this.writer = this.port.writable.getWriter();
        for (let id = 1; id <= MOTOR_NAMES.length; id++) {
            if (word(await this.request(id, 2, [3, 2], 2)) !== 777) {
                throw new LeaderSerialError(`Motor ${id} is not an STS3215.`);
            }
            const limits = await this.request(id, 2, [9, 4], 4);
            const min = word(limits), max = word(limits, 2);
            if (max <= min) throw new LeaderSerialError(`Calibrate leader motor ${id} with Python first.`);
            this.ranges.push({ min, max });
        }
        for (let id = 1; id <= MOTOR_NAMES.length; id++) {
            await this.request(id, 3, [40, 0]); // Disable leader torque, as Python configure() does.
            await this.request(id, 3, [55, 0]); // Unlock configuration registers.
            await this.request(id, 3, [33, 0]); // Position mode.
            const [phase] = await this.request(id, 2, [18, 1], 1);
            if (phase & 16) await this.request(id, 3, [18, phase & ~16]);
        }
        return this;
    }

    async getPositions() {
        const positions = {};
        for (let index = 0; index < MOTOR_NAMES.length; index++) {
            const raw = word(await this.request(index + 1, 2, [56, 2], 2));
            const value = raw & 0x8000 ? -(raw & 0x7fff) : raw;
            const { min, max } = this.ranges[index];
            positions[MOTOR_NAMES[index]] = index === 5
                ? (Math.min(max, Math.max(min, value)) - min) * 100 / (max - min)
                : (value - (min + max) / 2) * 360 / 4095;
        }
        return positions;
    }

    async request(id, instruction, parameters, size = 0) {
        if (this.closing) throw new LeaderSerialError('Leader disconnected.');
        const packet = [255, 255, id, parameters.length + 2, instruction, ...parameters];
        packet.push(~packet.slice(2).reduce((sum, byte) => sum + byte, 0) & 255);
        this.writing = this.writer.write(Uint8Array.from(packet));
        await this.writing;
        if (this.closing) throw new LeaderSerialError('Leader disconnected.');
        let timer;
        this.reading = this.readPacket(id, size);
        try {
            return await Promise.race([
                this.reading,
                new Promise((_, reject) => {
                    timer = setTimeout(() => reject(new LeaderSerialError(`No response from motor ${id}.`)), 250);
                })
            ]);
        } finally {
            clearTimeout(timer);
        }
    }

    async readPacket(id, size) {
        while (true) {
            while (this.buffer.length >= 6) {
                if (this.buffer[0] !== 255 || this.buffer[1] !== 255 || this.buffer[3] < 2) {
                    this.buffer.shift();
                    continue;
                }
                const length = this.buffer[3] + 4;
                if (this.buffer.length < length) break;
                const packet = this.buffer.splice(0, length);
                if (packet[2] !== id) continue;
                if ((packet.slice(2).reduce((sum, byte) => sum + byte, 0) & 255) !== 255) {
                    throw new LeaderSerialError(`Invalid checksum from motor ${id}.`);
                }
                if (packet[4] !== 0) throw new LeaderSerialError(`Motor ${id} reported error ${packet[4]}.`);
                if (length !== size + 6) throw new LeaderSerialError(`Unexpected response from motor ${id}.`);
                return packet.slice(5, -1);
            }
            const { value, done } = await this.reader.read();
            if (done) throw new LeaderSerialError('Leader disconnected.');
            this.buffer.push(...value);
        }
    }

    close() {
        this.closing ??= (async () => {
            if (this.reader) {
                await this.reader.cancel();
                await this.reading?.catch(() => {}); // A cancelled or timed-out read is already handled by its caller.
                this.reader.releaseLock();
            }
            await this.writing?.catch(() => {});
            this.writer?.releaseLock();
            if (this.port.readable || this.port.writable) await this.port.close();
        })();
        return this.closing;
    }
}

export function initSerialLeader(onConnected) {
    const button = document.getElementById('leaderConnect');
    const status = document.getElementById('leaderStatus');
    const master = document.getElementById('masterMode');
    if (!navigator.serial) {
        button.disabled = true;
        status.textContent = 'Web Serial requires a supported browser over HTTPS or localhost.';
        return { connected: () => false };
    }
    let leader = null;
    let connecting = false;
    let stopped = false;

    function expected(error) {
        return error instanceof LeaderSerialError || error instanceof DOMException;
    }

    async function stream(device) {
        try {
            while (leader === device) {
                const positions = await device.getPositions();
                if (leader !== device) break;
                window.liveWebRTC.sendControl({
                    timestamp: Date.now() / 1000,
                    leadState: { motors: Object.fromEntries(Object.entries(positions).map(([motor, position]) => [motor, { position }])) }
                });
                await sleep(100);
            }
        } catch (error) {
            if (leader === device) status.textContent = error.message;
            if (!expected(error)) throw error;
        } finally {
            if (leader === device) leader = null;
            master.checked = false;
            await device.close();
            button.textContent = 'Connect leader';
        }
    }

    async function connect(ports) {
        if (connecting || leader || stopped) return;
        connecting = true;
        button.disabled = true;
        try {
            for (const port of ports) {
                if (stopped) return;
                const device = new SerialLeader(port);
                try {
                    await device.connect();
                } catch (error) {
                    await device.close();
                    if (!expected(error)) throw error;
                    status.textContent = error.message;
                    continue;
                }
                if (stopped) { await device.close(); return; }
                leader = device;
                button.textContent = 'Disconnect leader';
                status.textContent = 'Leader connected — 6 motors';
                onConnected();
                void stream(device);
                return;
            }
        } finally {
            connecting = false;
            button.disabled = false;
        }
    }

    button.addEventListener('click', async () => {
        if (leader) {
            stopped = true;
            const device = leader;
            leader = null;
            master.checked = false;
            await device.close();
            button.textContent = 'Connect leader';
            status.textContent = 'Leader disconnected.';
            return;
        }
        try {
            const port = await navigator.serial.requestPort();
            stopped = false;
            await connect([port]);
        } catch (error) {
            status.textContent = error.name === 'NotFoundError' ? 'No leader port selected.' : error.message;
            if (!expected(error)) throw error;
        }
    });
    const scan = () => navigator.serial.getPorts().then(connect).catch(error => {
        status.textContent = error.message;
        if (!expected(error)) throw error;
    });
    navigator.serial.addEventListener('connect', scan);
    window.addEventListener('pagehide', () => {
        stopped = true;
        const device = leader;
        leader = null;
        master.checked = false;
        if (device) void device.close();
    });
    window.addEventListener('pageshow', () => { stopped = false; void scan(); });
    void scan();
    return { connected: () => leader !== null };
}
