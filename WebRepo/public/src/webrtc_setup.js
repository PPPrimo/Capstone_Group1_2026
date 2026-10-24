import { initLiveWebRTC } from '/api/webrtc/live_webrtc.js?v=20261023-3';

const CONFIG = {
    robotName: 'CapstoneBot',
    robotRole: 'Base',
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
