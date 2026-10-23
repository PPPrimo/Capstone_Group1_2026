// Browser WebRTC implementation, served through the authenticated module route.
export function initLiveWebRTC(CONFIG, { el, toggle, retry, master, latency, video }) {
    const setText = text => { if (el) el.textContent = text; };
    if (!toggle) { setText('Missing switch #tbt.'); return; }
    toggle.value = 'None';
    retry.checked = true;
    master.checked = false;

    let session = null;
    let reconnectTimer = null;
    let reconnectDelay = 500;
    let paused = false;
    let authBlocked = false;

    function enabled() { //reviewed
        if (toggle.value === 'None') return false;
        if (paused) return false;
        if (authBlocked) return false;
        return true;
    }
        
    function current(s) {
        return session === s && enabled() && s !== null && session.mode === toggle.value;
    }

    function stopSession() { 
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
        const old = session;
        session = null;
        if (!old) return;

        old.abort.abort();
        clearTimeout(old.deadline);
        clearTimeout(old.disconnectTimer);
        if (latency) latency.textContent = 'Latency: N/A';
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
        console.error('WebRTC connection failed:', message);
        stopSession();
        setText(`${message} ${retry.checked ? 'Reconnecting...' : 'Enable Retry connection to reconnect.'}`);
        if (!enabled() || !retry.checked) return;
        reconnectTimer = setTimeout(
            () => {reconnectTimer = null; connect(s.mode=="Direct_Control");}, 
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
            timer = setTimeout(() => {
                // Like aiortc, stop waiting for STUN and use available candidates.
                const hasCandidates = /^a=candidate:/m.test(pc.localDescription?.sdp || '');
                if (hasCandidates) console.warn('ICE gathering incomplete; using available candidates.');
                finish(hasCandidates ? undefined : new Error('ICE gathering timed out without candidates'));
            }, 5000);
            changed();
        });
    }

    async function showMessage(s, data, channel) { //reviewed
        if (!current(s)) return;
        if (data instanceof Blob) data = await data.text();
        else if (data instanceof ArrayBuffer) data = new TextDecoder().decode(data);
        if (!current(s)) return;

        let obj;
        try { obj = JSON.parse(data); }
        catch {
            if (latency) latency.textContent = 'Latency: N/A';
            setText(String(data)); return;
        }

        if (latency) latency.textContent = Number.isFinite(obj?.latency_avg_ms) && obj.latency_avg_ms >= 0
            ? `Latency: ${obj.latency_avg_ms.toFixed(2)} ms` : 'Latency: N/A';

        if (obj && typeof obj.timestamp === 'number' && Number.isFinite(obj.timestamp)) {
            if (channel.readyState === 'open') {
                window.liveWebRTC.sendControl({ timestamp: obj.timestamp });
            }
        }

        setText(JSON.stringify(obj, null, 2));
    }

    function setupChannel(s, channel) { //reviewed
        //setup the call back functions related to a channel (this is where the recieved data got used, right now it only displays them)
        channel.binaryType = 'arraybuffer';
        channel.onopen = () => { if (current(s)) setText('DataChannel open. Waiting for data...')};
        channel.onmessage = (event) => {
            console.log("Received:", channel.label, event.data);
            showMessage(s, event.data, channel).catch(
            (error) => {if (current(s)) setText(`Cannot decode received data: ${error.message}`)}
            )
        };
        channel.onclose = () => fail(s, 'DataChannel closed.');
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

    function sendMasterMode(s) {
        if (current(s) && s.masterChannel?.readyState === 'open') {
            s.masterChannel.send(String(master.checked));
        }
    }

    function attachVideo(s, track) { //reviewed
        if (!current(s) || track.kind !== 'video') return;
        if (!video) {
            video = document.getElementById('remoteVideo')
        }
        if (!video) {
            fail(s, 'Video stream element does exist in html');
            return;
        }

        video.controls = true;
        s.stream.addTrack(track);
        video.srcObject = s.stream;
        video.play().catch(() => {
            if (current(s)) setText('Video received. Press Play to display it.');
        });
        track.addEventListener('ended', () => fail(s, 'Robot video ended.'), { once: true });
    }

    async function connect(directControl = false) {  //reviewed
        //creates an offer and recieves an answer, establish the connection in the end
        const mode = directControl ? 'Direct_Control' : 'Control_Through_Server';
        if (!enabled() || session) return;
        if (!CONFIG.robotName || !CONFIG.robotRole) {
            setText('Configure robotName and robotRole before connecting.');
            return;
        }

        const s = {
            mode,
            pc: null,
            ws: null,
            abort: new AbortController(),
            stream: new MediaStream(),
            answerReceived: false
        };
        session = s;

        try {
            const pc = s.pc = new RTCPeerConnection({ iceServers: CONFIG.iceServers });
            //debuging messages to find out issues with ICE
            pc.onicecandidate = event => {
                console.log(
                    "Candidate:",
                    event.candidate?.candidate ?? "Gathering complete"
                );
            };
            pc.onicecandidateerror = event => {
                console.warn("STUN/TURN error:", {
                    url: event.url,
                    code: event.errorCode,
                    text: event.errorText
                });
            };
            pc.onicegatheringstatechange = () => {
                console.log("Gathering state:", pc.iceGatheringState);
            };
            //end
            pc.addTransceiver('video', { direction: 'recvonly' });
            //ensures creation of teleop channel
            s.channel = pc.createDataChannel('teleop', { ordered: false, maxRetransmits: 0 });
            setupChannel(s, s.channel);
            if (directControl) {
                s.masterChannel = pc.createDataChannel('master', { ordered: true });
                s.masterChannel.onopen = () => sendMasterMode(s);
            }
            //also handles additional channel if remote peer's offer contained it
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
                    retry.checked = false;
                    setText('RTC connected.');
                } else if (pc.connectionState === 'disconnected') {
                    setText('RTC interrupted. Waiting for recovery...');
                    if (!s.disconnectTimer) s.disconnectTimer = setTimeout(() => fail(s, 'RTC recovery timed out.'), 1000);
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
                ws.send(JSON.stringify({
                        type: 'offer', 
                        sdp: pc.localDescription.sdp, 
                        request_id: requestId,
                        direct_control: directControl,
                        robot_name: CONFIG.robotName,
                        robot_role: CONFIG.robotRole
                }));
                setText('Offer sent. Waiting for server answer...');
            };

            ws.onmessage = async event => {
                if (!current(s)) return;
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'error') throw new Error(msg.error || 'Server rejected the offer');
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

    function syncMode() { //reviewed
        //upon each call, checks for connection state
        if (current(session)) return; //checks if the existing connection is correct
        stopSession();
        reconnectDelay = 500;
        if (toggle.value === 'None') {
            setText('No connection selected.');
            return;
        }
        if (enabled()) {
            if (toggle.value === 'Control_Through_Server') connect(false);
            else connect(true);
        }
    }

    window.liveWebRTC = {
        refresh: syncMode,
        sendControl(message) {
            const s = session;
            if (!master.checked || !s || !current(s) || s.channel.readyState !== 'open') return false;
            // Carry the gate with each command; teleop is unordered and may drop packets.
            s.channel.send(JSON.stringify(s.mode === 'Control_Through_Server'
                ? { master_mode: true, command: message } : message));
            return true;
        }
    };

    toggle.addEventListener('change', syncMode);
    toggle.addEventListener('input', syncMode);
    master.addEventListener('change', () => sendMasterMode(session));
    retry.addEventListener('change', () => {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
        if (retry.checked) syncMode();
    });
    window.addEventListener('pageshow', () => { paused = false; syncMode(); });
    syncMode();

    const cleanup = () => { paused = true; stopSession(); };
    window.addEventListener('beforeunload', cleanup);
    window.addEventListener('pagehide', cleanup);
}
