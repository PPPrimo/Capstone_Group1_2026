import { initLiveWebRTC } from '/api/webrtc/live_webrtc.js?v=20261022-5';

const CONFIG = {
    robotName: 'CapstoneBot',
    robotRole: 'Base',
    iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] //{ urls: 'stun:stun.l.google.com:19302' }
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
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else init();
