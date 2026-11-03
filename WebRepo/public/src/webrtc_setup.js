import { initLiveWebRTC } from '/api/webrtc/live_webrtc.js?v=20261102-2';

const CONFIG = {
    robotName: '',
    robotRole: '',
};

function init() {
    initLiveWebRTC(CONFIG, {
        el: document.getElementById('liveData'),
        toggle: document.getElementById('tbt'),
        retry: document.getElementById('retryConnection'),
        master: document.getElementById('masterMode'),
        latency: document.getElementById('latency'),
        video: document.getElementById('remoteVideo')
    });

    const name = document.getElementById('robotName');
    const role = document.getElementById('robotRole');
    const status = document.getElementById('robotStatus');
    const mode = document.getElementById('tbt');
    const master = document.getElementById('masterMode');
    let robots = [];
    let socket = null;
    let reconnectTimer = null;
    let reconnectDelay = 500;
    let paused = false;

    function fill(select, values, placeholder) {
        const selected = select.value;
        select.replaceChildren(new Option(placeholder, ''), ...values.map(value => new Option(value, value)));
        select.value = values.includes(selected) ? selected : '';
        select.disabled = values.length === 0;
    }

    function selectionChanged() {
        const robot = robots.find(robot => robot.robot_name === name.value && robot.robot_role === role.value);
        const connected = robot?.status === 'connected';
        const previousName = CONFIG.robotName;
        const previousRole = CONFIG.robotRole;
        CONFIG.robotName = connected ? name.value : '';
        CONFIG.robotRole = connected ? role.value : '';
        mode.disabled = master.disabled = !connected;
        if (!connected) master.checked = false;
        status.textContent = robot ? {
            connected: 'Connected', lost: 'Lost — waiting for recovery...', confirmed_lost: 'Confirmed lost'
        }[robot.status] : (robots.length ? 'Select a robot and role.' : 'Waiting for a robot to connect...');
        if (CONFIG.robotName !== previousName || CONFIG.robotRole !== previousRole) window.liveWebRTC.refresh();
    }

    function updateRoles() {
        fill(role, robots.filter(robot => robot.robot_name === name.value).map(robot => robot.robot_role), 'Select role');
        selectionChanged();
    }

    function connectStatus() {
        if (paused || socket) return;
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        socket = new WebSocket(`${protocol}//${location.host}/api/webrtc/robots`);
        socket.onmessage = event => {
            const message = JSON.parse(event.data);
            robots = message.robots;
            reconnectDelay = 500;
            fill(name, [...new Set(robots.map(robot => robot.robot_name))].sort(), 'Select robot');
            updateRoles();
        };
        socket.onclose = event => {
            socket = null;
            if (paused) return;
            if (event.code === 4401 || event.code === 4403) {
                status.textContent = 'Log in again to view your robots.';
                return;
            }
            status.textContent = 'Robot list disconnected. Reconnecting...';
            reconnectTimer = setTimeout(connectStatus, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, 4000);
        };
    }

    name.addEventListener('change', updateRoles);
    role.addEventListener('change', selectionChanged);
    window.addEventListener('pagehide', () => {
        paused = true;
        clearTimeout(reconnectTimer);
        socket?.close();
    });
    window.addEventListener('pageshow', () => { paused = false; connectStatus(); });
    connectStatus();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else init();
