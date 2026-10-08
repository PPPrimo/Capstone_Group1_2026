// live_webrtc.js
(() => {
    'use strict';

    const CONFIG = {
        robotName: 'CapstoneBot',
        robotRole: 'Base',
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    };

    function init() {
        const el = document.getElementById('liveData');
        const toggle = document.getElementById('tbt');
        const setText = text => { if (el) el.textContent = text; };
        if (!toggle) { setText('Missing switch #tbt.'); return; }

        let session = null;
        let reconnectTimer = null;
        let reconnectDelay = 500;
        let paused = false;
        let authBlocked = false;
        let video = document.getElementById('remoteVideo');

        function enabled() { //reviewed
            if (!['Control_Through_Server', 'Direct_Control'].includes(toggle.value)) return false;
            if (paused) return false;
            if (authBlocked) return false;
            return true;
        }
            
        function current(s) {return session === s && enabled();}

        function stopSession() { 
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
            const old = session;
            session = null;
            if (!old) return;

            old.abort.abort();
            clearTimeout(old.deadline);
            clearTimeout(old.disconnectTimer);
            if (old.ws) {
                old.ws.onclose = null;
                old.ws.onerror = null;
                old.ws.close();
            }
            if (old.pc) old.pc.close();
            for (const track of old.stream.getTracks()) track.stop();
            if (video) video.srcObject = null;
        }

        function fail(s, message) { //reviewed
            if (!current(s)) return;
            stopSession();
            setText(`${message} Reconnecting...`);
            if (!enabled()) return;
            reconnectTimer = setTimeout(
                () => {reconnectTimer = null; connect();}, 
                reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, 4000);
        }

        function deadline(s, milliseconds, message) { //reviewed
            clearTimeout(s.deadline);
            s.deadline = setTimeout(() => fail(s, message), milliseconds);
        }

        function waitForIce(s) { //reviewed
            return new Promise((resolve, reject) => {
                const pc = s.pc;
                let timer;

                function finish(error) {
                    clearTimeout(timer);
                    pc.removeEventListener('icegatheringstatechange', changed);
                    s.abort.signal.removeEventListener('abort', aborted);
                    if (error) reject(error); else resolve();
                }

                function changed() {
                    if (pc.iceGatheringState === 'complete') finish();
                }

                function aborted() { finish(new Error('Connection cancelled')); }

                if (s.abort.signal.aborted) { aborted(); return; }
                if (pc.iceGatheringState === 'complete') { resolve(); return; }

                pc.addEventListener('icegatheringstatechange', changed);
                s.abort.signal.addEventListener('abort', aborted, { once: true });
                timer = setTimeout(() => finish(new Error('ICE gathering timed out')), 15000);
                changed();
            });
        }

        async function showMessage(s, data) { //reviewed
            if (!current(s)) return;
            const receivedAt = Date.now() / 1000;
            if (data instanceof Blob) data = await data.text();
            else if (data instanceof ArrayBuffer) data = new TextDecoder().decode(data);
            if (!current(s)) return;

            let obj;
            try { obj = JSON.parse(data); }
            catch { setText(String(data)); return; }

            const output = { data: obj, received_at: receivedAt };

            // Timestamp must be Unix seconds. Clock offset affects this measurement.
            if (obj && typeof obj.timestamp === 'number' && Number.isFinite(obj.timestamp)) {
                s.latencies.push(receivedAt - obj.timestamp);
                if (s.latencies.length > 1000) s.latencies.shift();
                output.AverageLatency = s.latencies.reduce((a, b) => a + b, 0) / s.latencies.length;
                output.MaximumLatency = Math.max(...s.latencies);
                output.MinimumLatency = Math.min(...s.latencies);
            }

            setText(JSON.stringify(output, null, 2));
        }

        function setupChannel(s, channel) {
            channel.binaryType = 'arraybuffer';
            channel.onopen = () => { if (current(s)) setText('DataChannel open. Waiting for data...')};
            channel.onmessage = (event) => {showMessage(s, event.data).catch(
                (error) => {if (current(s)) setText(`Cannot decode received data: ${error.message}`)}
                )
            };
            channel.onclose = () => fail(s, 'DataChannel closed.');
            // channel.onerror = () => fail(s, 'DataChannel failed.');
            channel.onerror = (event) => {
                const error = event.error; // RTCError when the browser provides it

                console.error("DataChannel error", {
                    event,
                    name: error?.name,
                    message: error?.message,
                    detail: error?.errorDetail,
                    sctpCauseCode: error?.sctpCauseCode,
                    channelState: channel.readyState,
                    connectionState: s.pc.connectionState,
                    iceState: s.pc.iceConnectionState,
                });

                fail(s, `DataChannel failed: ${error?.message || error?.errorDetail || "unknown reason"}`);
            };
        }

        function attachVideo(s, track) { //reviewed
            if (!current(s) || track.kind !== 'video') return;
            if (!video) {
                video = getElementById('remoteVideo')
            }
            if (!video) {
                fail(s, 'Video stream element does exist in html');
            }

            video.controls = true;
            s.stream.addTrack(track);
            video.srcObject = s.stream;
            video.play().catch(() => {
                if (current(s)) setText('Video received. Press Play to display it.');
            });
            track.addEventListener('ended', () => fail(s, 'Robot video ended.'), { once: true });
        }

        async function connect() {
            if (!enabled() || session) return;
            if (!CONFIG.robotName || !CONFIG.robotRole) {
                setText('Configure robotName and robotRole before connecting.');
                return;
            }

            const s = {
                pc: null,
                mode: toggle.value,
                ws: null,
                abort: new AbortController(),
                stream: new MediaStream(),
                latencies: [],
                answerReceived: false
            };
            session = s;

            try {
                const pc = s.pc = new RTCPeerConnection({ iceServers: CONFIG.iceServers });
                pc.addTransceiver('video', { direction: 'recvonly' });
                s.channel = pc.createDataChannel('teleop', { ordered: false, maxRetransmits: 0 });
                setupChannel(s, s.channel);

                pc.ondatachannel = (event) => { if (current(s)) setupChannel(s, event.channel); };
                pc.ontrack = event => attachVideo(s, event.track);
                pc.onconnectionstatechange = () => {
                    if (!current(s)) return;
                    if (pc.connectionState === 'connected') {
                        clearTimeout(s.deadline);
                        clearTimeout(s.disconnectTimer);
                        s.disconnectTimer = null;
                        s.deadline = null;
                        reconnectDelay = 500;
                        setText('RTC connected.');
                    } else if (pc.connectionState === 'disconnected') {
                        setText('RTC interrupted. Waiting for recovery...');
                        if (!s.disconnectTimer) s.disconnectTimer = setTimeout(() => fail(s, 'RTC recovery timed out.'), 5000);
                    } else if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
                        fail(s, 'RTC connection ended.');
                    }
                };

                deadline(s, 20000, 'Offer creation timed out.');
                setText('Creating offer and gathering ICE candidates...');
                await pc.setLocalDescription(await pc.createOffer());
                await waitForIce(s);
                if (!current(s)) return;

                // Gather ICE before opening signaling to avoid the server's offer-receive timeout.
                const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
                const path = `/api/webrtc/client/offer/${encodeURIComponent(CONFIG.robotName)}/${encodeURIComponent(CONFIG.robotRole)}`;
                const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
                const ws = s.ws = new WebSocket(`${protocol}//${location.host}${path}`);

                deadline(s, 40000, 'Signaling timed out.');
                ws.onopen = () => {
                    if (!current(s)) { ws.close(); return; }
                    ws.send(JSON.stringify({ type: 'offer', sdp: pc.localDescription.sdp, request_id: requestId, direct_control: s.mode === 'Direct_Control', robot_name: CONFIG.robotName, robot_role: CONFIG.robotRole }));
                    setText('Offer sent. Waiting for server answer...');
                };

                ws.onmessage = async event => {
                    if (!current(s)) return;
                    try {
                        const msg = JSON.parse(event.data);
                        if (msg.type === 'error') {
                            if (msg.retryable === false) {
                                stopSession();
                                setText(msg.error || 'Server rejected the offer');
                                return;
                            }
                            throw new Error(msg.error || 'Server rejected the offer');
                        }
                        if (msg.type !== 'answer' || typeof msg.sdp !== 'string') throw new Error('Invalid server answer');
                        if (msg.request_id !== undefined && msg.request_id !== requestId) throw new Error('Answer request ID mismatch');
                        if (s.answerReceived) return;

                        s.answerReceived = true;
                        await pc.setRemoteDescription({ type: 'answer', sdp: msg.sdp });
                        if (!current(s)) return;

                        if (pc.connectionState !== 'connected') deadline(s, 30000, 'RTC connection timed out.');
                        else clearTimeout(s.deadline);
                        if (ws.readyState === WebSocket.OPEN) ws.close(1000);
                    } catch (error) { fail(s, error.message); }
                };

                ws.onclose = event => {
                    if (!current(s)) return;
                    if (event.code === 4401 || event.code === 4403) {
                        authBlocked = true;
                        stopSession();
                        setText('Unauthorized. Log in, then refresh the page.');
                    } else if (!s.answerReceived) {
                        fail(s, 'Signaling closed before receiving an answer.');
                    }
                };
                
                //ws error do not block rtc
                ws.onerror = () => {
                    if (current(s) && !s.answerReceived) fail(s, 'Signaling connection failed.');
                };
            } catch (error) { fail(s, error.message); }
        }

        function syncMode() {
            if (enabled() && session && session.mode === toggle.value) return;
            stopSession();
            reconnectDelay = 500;
            connect();
        }

        window.liveWebRTC = {
            refresh: syncMode,
            sendControl(message) {
                const s = session;
                if (!s || !current(s) || s.channel.readyState !== 'open') return false;
                s.channel.send(JSON.stringify(message));
                return true;
            }
        };

        toggle.addEventListener('change', syncMode);
        toggle.addEventListener('input', syncMode);

        const cleanup = () => { paused = true; stopSession(); };
        window.addEventListener('beforeunload', cleanup);
        window.addEventListener('pagehide', cleanup);
        window.addEventListener('pageshow', () => { paused = false; syncMode(); });
        syncMode();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
    else init();
})();