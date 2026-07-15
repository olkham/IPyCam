// IP Camera Web UI JavaScript

/**
 * Stream state management
 */
// Which transport is being previewed: 'rtc' (go2rtc WebRTC iframe),
// 'native_rtc' (Python native WebRTC), or 'mjpeg' (native MJPEG).
let currentTransport = 'rtc';
let streamingMode = 'go2rtc';   // 'go2rtc', 'native_webrtc', or 'mjpeg' (server mode)
let mjpegUrl = '';
let nativeWebRTCAvailable = false;
let nativePeerConnection = null;

/**
 * Which stream the preview shows: 'main' (full resolution) or 'sub' (lower).
 * Applies to whichever transport is active. Native WebRTC is main-only
 * server-side, so 'sub' is ignored (and its button disabled) for that
 * transport -- see transportSupportsSub(). Persisted across reloads.
 */
let currentQuality = 'main';
try {
    const stored = localStorage.getItem('ipycam_stream_quality')
        || localStorage.getItem('ipycam_mjpeg_quality');  // migrate old key
    if (stored === 'main' || stored === 'sub') {
        currentQuality = stored;
    }
} catch (e) {
    // localStorage unavailable (e.g. private browsing) -- default stands.
}

// Transports that can serve the lower-resolution sub stream. Native WebRTC
// (aiortc) publishes only the main track, so it is main-only.
function transportSupportsSub(transport) {
    return transport === 'rtc' || transport === 'mjpeg';
}

// The quality actually rendered for the current transport: 'sub' is coerced to
// 'main' on transports that don't support it, without losing the user's choice.
function effectiveQuality() {
    return transportSupportsSub(currentTransport) ? currentQuality : 'main';
}

/**
 * Video upload state management
 */
let videoUploadMode = false;
let currentVideoFile = null;

/**
 * PTZ Control Functions
 */
let ptzMoveTimeout = null;

function ptzMove(pan, tilt) {
    // Clear any pending stop
    if (ptzMoveTimeout) {
        clearTimeout(ptzMoveTimeout);
        ptzMoveTimeout = null;
    }
    
    fetch('/api/ptz', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'move', pan: pan, tilt: tilt, zoom: 0 })
    })
    .catch(err => console.error('PTZ error:', err));
}

function ptzStop() {
    // Small delay to avoid rapid start/stop
    ptzMoveTimeout = setTimeout(() => {
        fetch('/api/ptz', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'stop' })
        })
        .catch(err => console.error('PTZ error:', err));
        ptzMoveTimeout = null;
    }, 50);
}

function ptzZoom(delta) {
    fetch('/api/ptz', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'zoom', delta: delta })
    })
    .then(r => r.json())
    .then(data => {
        if (data.zoom !== undefined) {
            document.getElementById('zoom-slider').value = data.zoom * 100;
        }
    })
    .catch(err => console.error('PTZ error:', err));
}

function ptzZoomTo(percent) {
    const value = parseFloat(percent) / 100;
    fetch('/api/ptz', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'zoom_to', value: value })
    })
    .catch(err => console.error('PTZ error:', err));
}

function ptzHome() {
    fetch('/api/ptz', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'home' })
    })
    .then(r => r.json())
    .then(data => {
        document.getElementById('zoom-slider').value = 0;
    })
    .catch(err => console.error('PTZ error:', err));
}

function updatePtzStatus() {
    fetch('/api/ptz')
        .then(r => r.json())
        .then(data => {
            if (data.zoom !== undefined) {
                document.getElementById('zoom-slider').value = data.zoom * 100;
            }
        })
        .catch(() => {});
}

/**
 * Switch the preview transport: 'rtc' (go2rtc), 'native_rtc' (Python WebRTC),
 * or 'mjpeg'. The Main/Sub selection (currentQuality) is preserved across
 * transports. Ignores transports the server marked unavailable.
 */
function switchTransport(type) {
    if (type !== 'rtc' && type !== 'native_rtc' && type !== 'mjpeg') return;

    const btn = document.querySelector(`#transport-switcher .btn-stream[data-type="${type}"]`);
    if (btn && btn.classList.contains('disabled')) return;
    if (type === 'rtc' && streamingMode !== 'go2rtc') return;  // go2rtc not available

    currentTransport = type;
    applyPreview();
}

/**
 * Switch the Main/Sub stream for the active transport. No-op for 'sub' on
 * main-only transports (native WebRTC). Persists the choice.
 */
function switchQuality(quality) {
    if (quality !== 'main' && quality !== 'sub') return;
    if (quality === 'sub' && !transportSupportsSub(currentTransport)) return;

    currentQuality = quality;
    try {
        localStorage.setItem('ipycam_stream_quality', quality);
    } catch (e) {
        // localStorage unavailable -- selection still applies for this session.
    }
    applyPreview();
}

/**
 * Render the preview for the current (transport, quality) pair and refresh the
 * switcher button states + indicators. Every preview change flows through here
 * so transport and quality switches stay consistent.
 */
function applyPreview() {
    const iframe = document.getElementById('preview-iframe');
    const mjpegImg = document.getElementById('preview-mjpeg');
    const nativeVideo = document.getElementById('preview-native-rtc');
    const mainStream = iframe.dataset.mainStream;
    const subStream = iframe.dataset.subStream;
    const apiUrl = iframe.dataset.apiUrl;
    const quality = effectiveQuality();

    // Tear down any native WebRTC connection when leaving that transport.
    if (currentTransport !== 'native_rtc' && nativePeerConnection) {
        nativePeerConnection.close();
        nativePeerConnection = null;
    }

    clearResolutionIndicator();  // stale until the new frame's metadata loads

    if (currentTransport === 'mjpeg') {
        iframe.style.display = 'none';
        nativeVideo.style.display = 'none';
        nativeVideo.srcObject = null;
        mjpegImg.style.display = 'block';
        mjpegImg.src = buildMjpegUrl(quality);
    } else if (currentTransport === 'native_rtc') {
        iframe.style.display = 'none';
        mjpegImg.style.display = 'none';
        mjpegImg.src = '';  // stop MJPEG loading
        nativeVideo.style.display = 'block';
        startNativeWebRTC(nativeVideo);  // main-only server-side
    } else {  // 'rtc' -- go2rtc WebRTC iframe
        mjpegImg.style.display = 'none';
        mjpegImg.src = '';
        nativeVideo.style.display = 'none';
        nativeVideo.srcObject = null;
        iframe.style.display = 'block';
        iframe.src = `${apiUrl}/stream.html?src=${quality === 'sub' ? subStream : mainStream}`;
    }

    updateTransportButtons();
    updateQualityButtons();
    updateStreamModeIndicator();
}

/**
 * Mark the active transport button.
 */
function updateTransportButtons() {
    document.querySelectorAll('#transport-switcher .btn-stream').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.type === currentTransport);
    });
}

/**
 * Mark the active Main/Sub button and disable Sub on main-only transports.
 */
function updateQualityButtons() {
    const supportsSub = transportSupportsSub(currentTransport);
    const active = effectiveQuality();
    document.querySelectorAll('#quality-switcher .btn-stream').forEach(btn => {
        const q = btn.dataset.quality;
        btn.classList.toggle('active', q === active);
        if (q === 'sub') {
            btn.classList.toggle('disabled', !supportsSub);
            btn.title = supportsSub ? '' : 'Sub stream not available for native WebRTC (main-only)';
        }
    });
}

/**
 * Build the MJPEG stream URL for the given quality ('main' or 'sub').
 * Reuses the base URL discovered from /api/config (or a same-origin
 * fallback) and appends the ?stream=sub selector for the sub stream.
 */
function buildMjpegUrl(quality) {
    const base = mjpegUrl || (window.location.origin + '/stream.mjpeg');
    if (quality !== 'sub') {
        return base;
    }
    return base + (base.includes('?') ? '&' : '?') + 'stream=sub';
}

/**
 * Start native WebRTC connection
 */
async function startNativeWebRTC(videoElement) {
    try {
        nativePeerConnection = new RTCPeerConnection({
            sdpSemantics: 'unified-plan',
            iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
        });
        
        // Handle incoming video track
        nativePeerConnection.addEventListener('track', (evt) => {
            if (evt.track.kind === 'video') {
                videoElement.srcObject = evt.streams[0];
            }
        });
        
        // Add transceiver to receive video
        nativePeerConnection.addTransceiver('video', { direction: 'recvonly' });
        
        // Create offer
        const offer = await nativePeerConnection.createOffer();
        await nativePeerConnection.setLocalDescription(offer);
        
        // Wait for ICE gathering
        await new Promise((resolve) => {
            if (nativePeerConnection.iceGatheringState === 'complete') {
                resolve();
            } else {
                nativePeerConnection.addEventListener('icegatheringstatechange', () => {
                    if (nativePeerConnection.iceGatheringState === 'complete') {
                        resolve();
                    }
                });
            }
        });
        
        // Send offer to server
        const response = await fetch('/api/webrtc/offer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sdp: nativePeerConnection.localDescription.sdp,
                type: nativePeerConnection.localDescription.type
            })
        });
        
        if (!response.ok) {
            throw new Error('Failed to connect');
        }
        
        const answer = await response.json();
        await nativePeerConnection.setRemoteDescription(new RTCSessionDescription(answer));
        
    } catch (err) {
        console.error('Native WebRTC error:', err);
        // Fall back to MJPEG on error
        if (nativeWebRTCAvailable) {
            switchTransport('mjpeg');
        }
    }
}

/**
 * Check if WebRTC/go2rtc is available and fallback to MJPEG if not
 */
function checkStreamAvailability() {
    fetch('/api/config')
        .then(r => r.json())
        .then(config => {
            streamingMode = config.streaming_mode || 'go2rtc';
            nativeWebRTCAvailable = config.webrtc_native_available || false;
            
            // Use full MJPEG URL from config, or construct from current location
            if (config.mjpeg_url && config.mjpeg_url.startsWith('http')) {
                mjpegUrl = config.mjpeg_url;
            } else {
                mjpegUrl = window.location.origin + '/stream.mjpeg';
            }
            
            // Update MJPEG URL display (both text and href)
            const mjpegUrlEl = document.getElementById('mjpeg-url');
            if (mjpegUrlEl) {
                mjpegUrlEl.textContent = mjpegUrl;
                mjpegUrlEl.href = mjpegUrl;
            }
            
            // Update mode indicator
            updateStreamModeIndicator();
            
            // Configure UI based on streaming mode, then pick a default transport.
            if (streamingMode === 'go2rtc') {
                // Full functionality available - go2rtc WebRTC is superior, disable native WebRTC
                enableStreamButtons(['rtc', 'mjpeg']);
                disableStreamButtons(['native_rtc'], 'go2rtc WebRTC is active (better performance)');
                switchTransport('rtc');
            } else if (streamingMode === 'native_webrtc') {
                // Native WebRTC mode - disable go2rtc buttons, default to MJPEG (faster start)
                disableStreamButtons(['rtc'], 'go2rtc WebRTC unavailable');
                enableStreamButtons(['native_rtc', 'mjpeg']);
                switchTransport('mjpeg');
            } else {
                // MJPEG-only mode
                disableStreamButtons(['rtc'], 'WebRTC unavailable - go2rtc not running');
                if (nativeWebRTCAvailable) {
                    enableStreamButtons(['native_rtc', 'mjpeg']);
                } else {
                    disableStreamButtons(['native_rtc'], 'Install aiortc for native WebRTC');
                }
                switchTransport('mjpeg');
            }
        })
        .catch(err => console.error('Failed to check stream availability:', err));
}

/**
 * Enable stream buttons by type
 */
function enableStreamButtons(types) {
    types.forEach(type => {
        document.querySelectorAll(`.btn-stream[data-type="${type}"]`).forEach(btn => {
            btn.classList.remove('disabled');
            btn.title = '';
        });
    });
}

/**
 * Disable stream buttons by type
 */
function disableStreamButtons(types, reason) {
    types.forEach(type => {
        document.querySelectorAll(`.btn-stream[data-type="${type}"]`).forEach(btn => {
            btn.classList.add('disabled');
            btn.title = reason;
        });
    });
}

/**
 * Update the stream mode indicator badge
 * Shows current view type and server streaming mode
 */
function updateStreamModeIndicator() {
    const indicator = document.getElementById('stream-mode-indicator');
    if (!indicator) return;
    
    // Show what the user is currently viewing (transport + main/sub).
    const qualitySuffix = effectiveQuality() === 'sub' ? ' (Sub)' : '';

    if (currentTransport === 'mjpeg') {
        // Differentiate between go2rtc MJPEG and native Python MJPEG
        if (streamingMode === 'go2rtc') {
            indicator.textContent = 'go2rtc MJPEG' + qualitySuffix;
            indicator.className = 'stream-mode-indicator mode-go2rtc';
            indicator.title = 'Viewing go2rtc MJPEG stream';
        } else {
            indicator.textContent = 'Python Native MJPEG' + qualitySuffix;
            indicator.className = 'stream-mode-indicator mode-mjpeg';
            indicator.title = 'Viewing Python native MJPEG stream';
        }
    } else if (currentTransport === 'native_rtc') {
        indicator.textContent = 'Python Native WebRTC';
        indicator.className = 'stream-mode-indicator mode-native-webrtc';
        indicator.title = 'Viewing Python native WebRTC stream (aiortc)';
    } else {  // 'rtc'
        indicator.textContent = 'go2rtc WebRTC' + qualitySuffix;
        indicator.className = 'stream-mode-indicator mode-go2rtc';
        indicator.title = 'Viewing go2rtc WebRTC stream';
    }
}

/**
 * Update the small resolution readout next to the stream mode indicator.
 * The preview is CSS-scaled to fit its container, so switching Main/Sub
 * doesn't visibly change on screen -- this surfaces the actual served
 * dimensions (MJPEG <img> naturalWidth, or WebRTC <video> videoWidth) so
 * the toggle is observable. `label` is the transport name shown (e.g.
 * 'MJPEG' / 'WebRTC'). Not shown for the go2rtc iframe (unreadable).
 */
function updateResolutionIndicator(width, height, label) {
    const el = document.getElementById('resolution-indicator');
    if (!el) return;

    if (!width || !height) {
        el.style.display = 'none';
        return;
    }

    const qualityLabel = effectiveQuality() === 'sub' ? 'Sub' : 'Main';
    el.textContent = `${label} ${width}×${height} (${qualityLabel})`;
    el.style.display = '';
}

/**
 * Hide the resolution readout immediately (e.g. while a new frame is loading,
 * or after switching transport) so it doesn't show a stale resolution until
 * the new frame's metadata loads.
 */
function clearResolutionIndicator() {
    const el = document.getElementById('resolution-indicator');
    if (el) el.style.display = 'none';
}

/**
 * Local recording control (step 4.4). Toggles POST /api/recording/start and
 * /api/recording/stop; the button's live state is otherwise kept in sync by
 * updateStats() reading the /api/stats "recording" block.
 */
let isRecording = false;

function toggleRecording() {
    const endpoint = isRecording ? '/api/recording/stop' : '/api/recording/start';
    const btn = document.getElementById('record-btn');
    if (btn) btn.disabled = true;
    fetch(endpoint, { method: 'POST' })
        .then(r => r.json())
        .then(data => { updateRecordingButton(data.recording); })
        .catch(err => console.error('Recording error:', err))
        .finally(() => { if (btn) btn.disabled = false; });
}

function updateRecordingButton(recording) {
    isRecording = Boolean(recording);
    const btn = document.getElementById('record-btn');
    if (!btn) return;
    btn.textContent = isRecording ? '■ Stop' : '● REC';
    btn.classList.toggle('recording-active', isRecording);
    btn.title = isRecording ? 'Recording -- click to stop' : 'Start local recording';
}

/**
 * Format large numbers with units (k, M, B)
 */
function formatNumber(num) {
    if (num === '-' || num === undefined || num === null) return '-';
    const n = parseInt(num);
    if (isNaN(n)) return '-';
    
    if (n >= 1000000000) {
        return (n / 1000000000).toFixed(1) + 'B';
    } else if (n >= 1000000) {
        return (n / 1000000).toFixed(1) + 'M';
    } else if (n >= 1000) {
        return (n / 1000).toFixed(1) + 'k';
    }
    return n.toString();
}

/**
 * Format uptime in human-readable units
 */
function formatUptime(seconds) {
    if (!seconds || seconds === '-') return '-';
    const s = parseInt(seconds);
    if (isNaN(s)) return '-';
    
    const years = Math.floor(s / 31536000);
    const days = Math.floor((s % 31536000) / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    const secs = s % 60;
    
    if (years > 0) {
        return `${years}y ${days}d`;
    } else if (days > 0) {
        return `${days}d ${hours}h`;
    } else if (hours > 0) {
        return `${hours}h ${minutes}m`;
    } else if (minutes > 0) {
        return `${minutes}m ${secs}s`;
    } else {
        return `${secs}s`;
    }
}

/**
 * Update stats display from server
 */
function updateStats() {
    fetch('/api/stats')
        .then(r => r.json())
        .then(data => {
            // Show stats based on what the user is currently viewing
            let fps, frames, uptime;
            
            if (currentTransport === 'mjpeg') {
                // Viewing MJPEG - show MJPEG stats
                fps = data.mjpeg_fps ?? data.actual_fps ?? '-';
                frames = data.mjpeg_frames_sent ?? data.frames_sent ?? '-';
                uptime = data.mjpeg_elapsed_time ?? data.elapsed_time;
            } else if (currentTransport === 'native_rtc') {
                // Viewing native WebRTC - show WebRTC stats
                fps = data.webrtc_fps ?? data.actual_fps ?? '-';
                frames = data.webrtc_frames_sent ?? data.frames_sent ?? '-';
                uptime = data.webrtc_elapsed_time ?? data.elapsed_time;
            } else {
                // Viewing go2rtc - show primary stats
                fps = data.actual_fps ?? '-';
                frames = data.frames_sent ?? '-';
                uptime = data.elapsed_time;
            }
            
            document.getElementById('fps').textContent = fps;
            document.getElementById('frames').textContent = formatNumber(frames);
            document.getElementById('uptime').textContent = formatUptime(uptime);
            document.getElementById('dropped').textContent = data.dropped_frames || '0';
            document.getElementById('status').className = 'status ' + (data.is_streaming ? 'online' : 'offline');
            
            // Update streaming mode if changed
            if (data.streaming_mode && data.streaming_mode !== streamingMode) {
                streamingMode = data.streaming_mode;
                updateStreamModeIndicator();
            }

            // Reflect recorder state on the record button.
            if (data.recording) {
                updateRecordingButton(data.recording.recording);
            }
        })
        .catch(err => {
            console.error('Failed to fetch stats:', err);
            document.getElementById('status').className = 'status offline';
        });
}

/**
 * Paged settings (step 4.3): Display / Stream / Identity / User tabs.
 *
 * Each tab is a plain CSS-toggled panel (see .tab-btn/.tab-panel in
 * style.css). Every editable field is declared once in SETTINGS_FIELDS so
 * loading, saving, and validating a tab all walk the same list instead of
 * three separate hand-written field lists getting out of sync.
 */
const SETTINGS_FIELDS = {
    display: [
        { id: 'disp_flip', key: 'flip', type: 'checkbox' },
        { id: 'disp_mirror', key: 'mirror', type: 'checkbox' },
        { id: 'disp_rotation', key: 'rotation', type: 'int' },
        { id: 'disp_show_timestamp', key: 'show_timestamp', type: 'checkbox' },
        { id: 'disp_timestamp_format', key: 'timestamp_format', type: 'text' },
        { id: 'disp_timestamp_position', key: 'timestamp_position', type: 'text' },
    ],
    stream: [
        { id: 'stream_main_width', key: 'main_width', type: 'int' },
        { id: 'stream_main_height', key: 'main_height', type: 'int' },
        { id: 'stream_main_fps', key: 'main_fps', type: 'int' },
        { id: 'stream_main_bitrate', key: 'main_bitrate', type: 'text' },
        { id: 'stream_main_stream_name', key: 'main_stream_name', type: 'text' },
        { id: 'stream_sub_width', key: 'sub_width', type: 'int' },
        { id: 'stream_sub_height', key: 'sub_height', type: 'int' },
        { id: 'stream_sub_fps', key: 'sub_fps', type: 'int' },
        { id: 'stream_sub_bitrate', key: 'sub_bitrate', type: 'text' },
        { id: 'stream_sub_stream_name', key: 'sub_stream_name', type: 'text' },
        { id: 'stream_hw_accel', key: 'hw_accel', type: 'text' },
    ],
    identity: [
        { id: 'ident_name', key: 'name', type: 'text' },
        { id: 'ident_manufacturer', key: 'manufacturer', type: 'text' },
        { id: 'ident_model', key: 'model', type: 'text' },
    ],
};

// Display-only fields: rendered disabled, never sent on save (ports and
// serial/firmware are not in EDITABLE_FIELDS -- see ipycam/config.py).
const READONLY_FIELDS = [
    { id: 'stream_onvif_port', key: 'onvif_port' },
    { id: 'stream_rtsp_port', key: 'rtsp_port' },
    { id: 'stream_rtmp_port', key: 'rtmp_port' },
    { id: 'stream_go2rtc_api_port', key: 'go2rtc_api_port' },
    { id: 'ident_serial_number', key: 'serial_number' },
    { id: 'ident_firmware_version', key: 'firmware_version' },
];

// Client-side mirrors of the server-side rules in CameraConfig._validate_update.
// These exist only for a nicer UX (catch obvious mistakes before the round
// trip) -- apply_updates() on the server is always the source of truth and
// re-validates every field regardless of what passes here.
const VALID_ROTATIONS = [0, 90, 180, 270];
const VALID_HW_ACCEL = ['auto', 'nvenc', 'qsv', 'cpu'];
const VALID_TIMESTAMP_POSITIONS = ['top-left', 'top-right', 'bottom-left', 'bottom-right'];
const BITRATE_RE = /^\d+[KMG]?$/;
const MIN_FPS = 1, MAX_FPS = 120;
const MAX_WIDTH = 7680, MAX_HEIGHT = 4320;

/**
 * Switch the visible settings tab. Toggles .active on both the tab button
 * and its matching panel; does not touch PTZ/preview/stats, which live
 * outside the settings card.
 */
function switchSettingsTab(tab) {
    document.querySelectorAll('#settings-tabs .tab-btn').forEach(btn => {
        const isActive = btn.dataset.tab === tab;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', String(isActive));
    });
    document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.dataset.tabPanel === tab);
    });
}

function setFieldValue(field, value) {
    const el = document.getElementById(field.id);
    if (!el) return;
    if (field.type === 'checkbox') {
        el.checked = Boolean(value);
    } else if (el.tagName === 'SELECT') {
        for (const opt of el.options) {
            opt.selected = String(opt.value) === String(value);
        }
    } else {
        el.value = (value === undefined || value === null) ? '' : value;
    }
}

function getFieldValue(field) {
    const el = document.getElementById(field.id);
    if (!el) return undefined;
    if (field.type === 'checkbox') return el.checked;
    if (field.type === 'int') {
        const n = parseInt(el.value, 10);
        return Number.isNaN(n) ? el.value : n;
    }
    return el.value;
}

/**
 * Populate every settings tab (editable + read-only fields, and the User
 * tab's auth-state banner + username) from the /api/config payload. Called
 * from loadConfig() so the tabs stay in sync with whatever the rest of the
 * page already fetched -- no separate request.
 */
function populateSettingsForm(config) {
    Object.values(SETTINGS_FIELDS).flat().forEach(field => {
        setFieldValue(field, config[field.key]);
    });

    READONLY_FIELDS.forEach(field => {
        const el = document.getElementById(field.id);
        if (el) el.value = (config[field.key] === undefined || config[field.key] === null) ? '' : config[field.key];
    });

    const authValueEl = document.getElementById('auth-state-value');
    if (authValueEl) {
        authValueEl.textContent = config.auth_enabled ? 'Enabled' : 'Disabled';
        authValueEl.style.color = config.auth_enabled ? '#00ff88' : '#888';
    }
    // Prefill the username (never the password -- the server never sends it).
    const usernameEl = document.getElementById('user_username');
    if (usernameEl && document.activeElement !== usernameEl) {
        usernameEl.value = config.username || '';
    }
}

/**
 * Client-side validation mirroring CameraConfig._validate_update. Returns a
 * list of human-readable error strings (empty = passes).
 */
function validateTabFields(tab, values) {
    const errors = [];
    if (tab === 'display') {
        if (!VALID_ROTATIONS.includes(values.rotation)) errors.push('rotation must be 0, 90, 180, or 270');
        if (!VALID_TIMESTAMP_POSITIONS.includes(values.timestamp_position)) errors.push('invalid timestamp position');
        if (!values.timestamp_format || !String(values.timestamp_format).trim()) errors.push('timestamp format cannot be empty');
    } else if (tab === 'stream') {
        if (!(values.main_width >= 1 && values.main_width <= MAX_WIDTH)) errors.push('main width out of range');
        if (!(values.main_height >= 1 && values.main_height <= MAX_HEIGHT)) errors.push('main height out of range');
        if (!(values.main_fps >= MIN_FPS && values.main_fps <= MAX_FPS)) errors.push('main fps out of range');
        if (!BITRATE_RE.test(values.main_bitrate)) errors.push('main bitrate format invalid (e.g. 8M, 512K)');
        if (!(values.sub_width >= 1 && values.sub_width <= MAX_WIDTH)) errors.push('sub width out of range');
        if (!(values.sub_height >= 1 && values.sub_height <= MAX_HEIGHT)) errors.push('sub height out of range');
        if (!(values.sub_fps >= MIN_FPS && values.sub_fps <= MAX_FPS)) errors.push('sub fps out of range');
        if (!BITRATE_RE.test(values.sub_bitrate)) errors.push('sub bitrate format invalid (e.g. 1M, 512K)');
        if (!VALID_HW_ACCEL.includes(values.hw_accel)) errors.push('invalid hardware acceleration option');
        if (!values.main_stream_name || !String(values.main_stream_name).trim()) errors.push('main stream name cannot be empty');
        if (!values.sub_stream_name || !String(values.sub_stream_name).trim()) errors.push('sub stream name cannot be empty');
    } else if (tab === 'identity') {
        if (!values.name || !String(values.name).trim()) errors.push('name cannot be empty');
        if (!values.manufacturer || !String(values.manufacturer).trim()) errors.push('manufacturer cannot be empty');
        if (!values.model || !String(values.model).trim()) errors.push('model cannot be empty');
    }
    return errors;
}

/**
 * Render the /api/config POST response (applied/rejected/restart_needed)
 * into a tab's inline status line, then clear it after a few seconds.
 */
function reportSettingsResult(statusEl, data) {
    if (!statusEl) return;
    if (data.success) {
        let msg = '';
        if (data.applied && data.applied.length) {
            msg += `Saved: ${data.applied.join(', ')}. `;
        }
        if (data.rejected && data.rejected.length) {
            msg += `Rejected: ${data.rejected.join(', ')}. `;
        }
        if (data.restart_needed) {
            msg += data.restarted ? 'Stream restarted.' : 'Stream restarting...';
        }
        statusEl.textContent = msg.trim() || 'Saved!';
        statusEl.style.color = (data.rejected && data.rejected.length) ? '#ffaa00' : '#00ff88';
    } else {
        statusEl.textContent = 'Error: ' + (data.error || 'unknown error');
        statusEl.style.color = '#e94560';
    }
    setTimeout(() => {
        statusEl.textContent = '';
        statusEl.style.color = '#888';
    }, 5000);
}

/**
 * Collect and save one settings tab's editable fields via the existing
 * /api/config endpoint (CameraConfig.apply_updates). Only that tab's own
 * fields are sent -- e.g. saving Display never touches stream/identity
 * fields -- and credentials are never included here (see saveCredentials).
 */
function saveSettingsTab(tab) {
    const fields = SETTINGS_FIELDS[tab];
    if (!fields) return;

    const values = {};
    fields.forEach(field => { values[field.key] = getFieldValue(field); });

    const statusEl = document.getElementById(`status-${tab}`);
    const errors = validateTabFields(tab, values);
    if (errors.length > 0) {
        if (statusEl) {
            statusEl.textContent = 'Fix: ' + errors.join('; ');
            statusEl.style.color = '#e94560';
        }
        return;
    }

    if (statusEl) {
        statusEl.textContent = 'Saving...';
        statusEl.style.color = '#888';
    }

    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(values)
    })
    .then(r => r.json())
    .then(data => reportSettingsResult(statusEl, data))
    .catch(err => {
        if (statusEl) {
            statusEl.textContent = 'Error: ' + err.message;
            statusEl.style.color = '#e94560';
        }
    });
}

/**
 * Save (or clear) the HTTP Basic auth credential pair via the dedicated
 * /api/credentials endpoint. username/password are deliberately NOT part of
 * SETTINGS_FIELDS/apply_updates -- see CameraConfig.set_credentials.
 */
function saveCredentials() {
    const username = document.getElementById('user_username').value;
    const password = document.getElementById('user_password').value;
    const confirmPassword = document.getElementById('user_password_confirm').value;
    const statusEl = document.getElementById('status-user');

    // Client-side checks for UX only -- the server re-validates (e.g. empty
    // username with a non-empty password is rejected) and is authoritative.
    if (password !== confirmPassword) {
        if (statusEl) {
            statusEl.textContent = 'Passwords do not match';
            statusEl.style.color = '#e94560';
        }
        return;
    }
    if ((username && !password) || (!username && password)) {
        if (statusEl) {
            statusEl.textContent = 'Both username and password are required (or leave both blank to disable auth)';
            statusEl.style.color = '#e94560';
        }
        return;
    }

    if (statusEl) {
        statusEl.textContent = 'Saving...';
        statusEl.style.color = '#888';
    }

    fetch('/api/credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password })
    })
    .then(r => r.json().then(data => ({ ok: r.ok, data })))
    .then(({ ok, data }) => {
        if (ok && data.success) {
            const authValueEl = document.getElementById('auth-state-value');
            if (authValueEl) {
                authValueEl.textContent = data.auth_enabled ? 'Enabled' : 'Disabled';
                authValueEl.style.color = data.auth_enabled ? '#00ff88' : '#888';
            }
            // Never keep the password in the DOM/memory longer than needed.
            document.getElementById('user_password').value = '';
            document.getElementById('user_password_confirm').value = '';
            if (statusEl) {
                statusEl.textContent = data.auth_enabled
                    ? 'Credentials saved. Auth enabled -- you may be prompted to log in.'
                    : 'Credentials cleared. Auth disabled.';
                statusEl.style.color = '#00ff88';
            }
        } else {
            if (statusEl) {
                statusEl.textContent = 'Error: ' + (data.error || 'unknown error');
                statusEl.style.color = '#e94560';
            }
        }
    })
    .catch(err => {
        if (statusEl) {
            statusEl.textContent = 'Error: ' + err.message;
            statusEl.style.color = '#e94560';
        }
    });
}

/**
 * Load current configuration and update form
 */
function loadConfig() {
    fetch('/api/config')
        .then(r => r.json())
        .then(config => {
            // Populate the Display/Stream/Identity/User settings tabs (step 4.3).
            populateSettingsForm(config);

            // Video upload mode
            videoUploadMode = config.video_upload_mode || false;
            const isVideoSource = config.source_type === 'video_file';
            
            // Show/hide video upload section
            const uploadSection = document.getElementById('video-upload-section');
            if (uploadSection) {
                uploadSection.style.display = (isVideoSource && videoUploadMode) ? 'block' : 'none';
            }
            
            // Update source info if video loaded
            if (config.current_video) {
                updateSourceInfo(config.current_video);
            }
            
            // Show video error if any
            if (config.video_error) {
                showUploadError(config.video_error);
            }
        })
        .catch(err => console.error('Failed to load config:', err));
}

/**
 * Initialize video upload functionality
 */
function initVideoUpload() {
    const uploadZone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('video-file-input');
    
    if (!uploadZone || !fileInput) return;
    
    // Click to browse
    uploadZone.addEventListener('click', () => fileInput.click());
    
    // File selected via input
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            uploadVideoFile(e.target.files[0]);
        }
    });
    
    // Drag and drop
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadZone.classList.add('drag-over');
    });
    
    uploadZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
    });
    
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('drag-over');
        
        if (e.dataTransfer.files.length > 0) {
            uploadVideoFile(e.dataTransfer.files[0]);
        }
    });
}

/**
 * Upload a video file to the server
 */
function uploadVideoFile(file) {
    // Validate file type
    const validExtensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg', '.3gp'];
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    
    if (!validExtensions.includes(ext)) {
        showUploadError(`Invalid file type: ${ext}. Supported formats: ${validExtensions.join(', ')}`);
        return;
    }
    
    // Hide previous messages
    hideUploadMessages();
    
    // Show progress
    const progressSection = document.getElementById('upload-progress');
    const progressFill = document.getElementById('progress-fill');
    const progressText = document.getElementById('progress-text');
    
    progressSection.style.display = 'block';
    progressFill.style.width = '0%';
    progressText.textContent = 'Preparing upload...';
    
    // Create form data
    const formData = new FormData();
    formData.append('video', file, file.name);
    
    // Create XHR for progress tracking
    const xhr = new XMLHttpRequest();
    
    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const percent = Math.round((e.loaded / e.total) * 100);
            progressFill.style.width = percent + '%';
            progressText.textContent = `Uploading: ${percent}% (${formatFileSize(e.loaded)} / ${formatFileSize(e.total)})`;
        }
    });
    
    xhr.addEventListener('load', () => {
        progressSection.style.display = 'none';
        
        if (xhr.status === 200) {
            try {
                const response = JSON.parse(xhr.responseText);
                if (response.success) {
                    showUploadSuccess(`Video uploaded: ${response.filename}`);
                    updateSourceInfo(response.filename);
                    currentVideoFile = response.filename;
                } else {
                    showUploadError(response.error || 'Upload failed');
                }
            } catch (e) {
                showUploadError('Invalid server response');
            }
        } else {
            try {
                const response = JSON.parse(xhr.responseText);
                showUploadError(response.error || `Upload failed (${xhr.status})`);
            } catch (e) {
                showUploadError(`Upload failed with status ${xhr.status}`);
            }
        }
    });
    
    xhr.addEventListener('error', () => {
        progressSection.style.display = 'none';
        showUploadError('Network error - upload failed');
    });
    
    xhr.addEventListener('abort', () => {
        progressSection.style.display = 'none';
        showUploadError('Upload cancelled');
    });
    
    xhr.open('POST', '/api/video/upload');
    xhr.send(formData);
}

/**
 * Format file size for display
 */
function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

/**
 * Show upload error message
 */
function showUploadError(message) {
    hideUploadMessages();
    const errorEl = document.getElementById('upload-error');
    if (errorEl) {
        errorEl.textContent = '⚠️ ' + message;
        errorEl.style.display = 'block';
        // Auto-hide after 10 seconds
        setTimeout(() => {
            errorEl.style.display = 'none';
        }, 10000);
    }
}

/**
 * Show upload success message
 */
function showUploadSuccess(message) {
    hideUploadMessages();
    const successEl = document.getElementById('upload-success');
    if (successEl) {
        successEl.textContent = '✓ ' + message;
        successEl.style.display = 'block';
        // Auto-hide after 5 seconds
        setTimeout(() => {
            successEl.style.display = 'none';
        }, 5000);
    }
}

/**
 * Hide all upload messages
 */
function hideUploadMessages() {
    const errorEl = document.getElementById('upload-error');
    const successEl = document.getElementById('upload-success');
    if (errorEl) errorEl.style.display = 'none';
    if (successEl) successEl.style.display = 'none';
}

/**
 * Update source info display
 */
function updateSourceInfo(filename) {
    const sourceNameEl = document.getElementById('source-name');
    if (sourceNameEl) {
        sourceNameEl.textContent = filename;
        sourceNameEl.title = filename;
    }
}

/**
 * Check video status periodically when in video upload mode
 */
function checkVideoStatus() {
    if (!videoUploadMode) return;
    
    fetch('/api/video/status')
        .then(r => r.json())
        .then(status => {
            // Update source info
            if (status.current_video) {
                updateSourceInfo(status.current_video);
            }
            
            // Show error if any (but don't spam if we already showed it)
            if (status.video_error && status.video_error !== lastVideoError) {
                showUploadError(status.video_error);
                lastVideoError = status.video_error;
            }
        })
        .catch(() => {});
}

let lastVideoError = null;

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    // Wire the resolution readout once; the handlers persist across src
    // reassignments so every loaded frame reports its actual dimensions.
    // Each only reports while its own transport is the active preview.
    const mjpegImgEl = document.getElementById('preview-mjpeg');
    if (mjpegImgEl) {
        mjpegImgEl.onload = () => {
            if (currentTransport === 'mjpeg') {
                updateResolutionIndicator(mjpegImgEl.naturalWidth, mjpegImgEl.naturalHeight, 'MJPEG');
            }
        };
    }
    const nativeVideoEl = document.getElementById('preview-native-rtc');
    if (nativeVideoEl) {
        nativeVideoEl.addEventListener('loadedmetadata', () => {
            if (currentTransport === 'native_rtc') {
                updateResolutionIndicator(nativeVideoEl.videoWidth, nativeVideoEl.videoHeight, 'WebRTC');
            }
        });
    }

    // Check stream availability first (sets button availability + default transport)
    checkStreamAvailability();
    
    // Start stats update interval
    setInterval(updateStats, 1000);
    updateStats();
    
    // Load current config
    loadConfig();
    
    // Load PTZ status
    updatePtzStatus();
    
    // Initialize video upload functionality
    initVideoUpload();
    
    // Check video status periodically (for video upload mode)
    setInterval(checkVideoStatus, 3000);
});
