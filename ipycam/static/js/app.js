// IP Camera Web UI JavaScript

/**
 * Stream state management
 */
let currentStreamType = 'rtc';  // 'rtc', 'native_rtc', or 'mjpeg'
let currentStream = 'main';     // 'main', 'sub', or 'mjpeg'
let streamingMode = 'go2rtc';   // 'go2rtc', 'native_webrtc', or 'mjpeg' (server mode)
let mjpegUrl = '';
let nativeWebRTCAvailable = false;
let nativePeerConnection = null;

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
 * Switch between stream types and sources
 * @param {string} stream - 'main', 'sub', or 'mjpeg'
 * @param {string} type - 'rtc', 'native_rtc', or 'mjpeg'
 */
function switchStream(stream, type) {
    // Block go2rtc RTC streams if in MJPEG-only or native_webrtc mode
    if (type === 'rtc' && streamingMode !== 'go2rtc') {
        return;  // Don't switch - go2rtc not available
    }
    
    const iframe = document.getElementById('preview-iframe');
    const mjpegImg = document.getElementById('preview-mjpeg');
    const nativeVideo = document.getElementById('preview-native-rtc');
    const mainStream = iframe.dataset.mainStream;
    const subStream = iframe.dataset.subStream;
    const apiUrl = iframe.dataset.apiUrl;
    
    currentStream = stream;
    currentStreamType = type;
    
    // Stop any existing native WebRTC connection
    if (nativePeerConnection) {
        nativePeerConnection.close();
        nativePeerConnection = null;
    }
    
    if (type === 'mjpeg' || stream === 'mjpeg') {
        // Switch to MJPEG mode - always use native MJPEG endpoint
        iframe.style.display = 'none';
        nativeVideo.style.display = 'none';
        mjpegImg.style.display = 'block';
        // Use full URL from mjpegUrl, or construct from current location
        const mjpegSrc = mjpegUrl || (window.location.origin + '/stream.mjpeg');
        mjpegImg.src = mjpegSrc;
        currentStreamType = 'mjpeg';
        currentStream = 'mjpeg';
    } else if (type === 'native_rtc') {
        // Switch to native WebRTC mode
        iframe.style.display = 'none';
        mjpegImg.style.display = 'none';
        mjpegImg.src = '';  // Stop MJPEG loading
        nativeVideo.style.display = 'block';
        startNativeWebRTC(nativeVideo);
        currentStreamType = 'native_rtc';
    } else {
        // Switch to go2rtc WebRTC mode
        mjpegImg.style.display = 'none';
        mjpegImg.src = '';  // Stop MJPEG loading
        nativeVideo.style.display = 'none';
        iframe.style.display = 'block';
        const streamName = stream === 'sub' ? subStream : mainStream;
        iframe.src = `${apiUrl}/stream.html?src=${streamName}`;
    }
    
    // Update button states
    document.querySelectorAll('.btn-stream').forEach(btn => {
        const btnStream = btn.dataset.stream;
        const btnType = btn.dataset.type;
        let isActive = false;
        
        if (type === 'mjpeg' || stream === 'mjpeg') {
            isActive = btnStream === 'mjpeg';
        } else if (type === 'native_rtc') {
            isActive = btnType === 'native_rtc';
        } else {
            isActive = btnStream === stream && btnType === type;
        }
        
        btn.classList.toggle('active', isActive);
    });
    
    // Update stream mode indicator to show current view
    updateStreamModeIndicator();
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
            switchStream('mjpeg', 'mjpeg');
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
            
            // Configure UI based on streaming mode
            if (streamingMode === 'go2rtc') {
                // Full functionality available - go2rtc WebRTC is superior, disable native WebRTC
                enableStreamButtons(['rtc', 'mjpeg']);
                disableStreamButtons(['native_rtc'], 'go2rtc WebRTC is active (better performance)');
            } else if (streamingMode === 'native_webrtc') {
                // Native WebRTC mode - disable go2rtc buttons, default to MJPEG (faster start)
                disableStreamButtons(['rtc'], 'go2rtc WebRTC unavailable');
                enableStreamButtons(['native_rtc', 'mjpeg']);
                switchStream('mjpeg', 'mjpeg');
            } else {
                // MJPEG-only mode
                disableStreamButtons(['rtc'], 'WebRTC unavailable - go2rtc not running');
                if (nativeWebRTCAvailable) {
                    enableStreamButtons(['native_rtc', 'mjpeg']);
                } else {
                    disableStreamButtons(['native_rtc'], 'Install aiortc for native WebRTC');
                }
                switchStream('mjpeg', 'mjpeg');
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
    
    // Show what the user is currently viewing
    if (currentStreamType === 'mjpeg' || currentStream === 'mjpeg') {
        // Differentiate between go2rtc MJPEG and native Python MJPEG
        if (streamingMode === 'go2rtc') {
            indicator.textContent = 'go2rtc MJPEG';
            indicator.className = 'stream-mode-indicator mode-go2rtc';
            indicator.title = 'Viewing go2rtc MJPEG stream';
        } else {
            indicator.textContent = 'Python Native MJPEG';
            indicator.className = 'stream-mode-indicator mode-mjpeg';
            indicator.title = 'Viewing Python native MJPEG stream';
        }
    } else if (currentStreamType === 'native_rtc') {
        indicator.textContent = 'Python Native WebRTC';
        indicator.className = 'stream-mode-indicator mode-native-webrtc';
        indicator.title = 'Viewing Python native WebRTC stream (aiortc)';
    } else if (currentStreamType === 'rtc') {
        indicator.textContent = 'go2rtc WebRTC';
        indicator.className = 'stream-mode-indicator mode-go2rtc';
        indicator.title = 'Viewing go2rtc WebRTC stream';
    } else {
        // Default based on server mode
        if (streamingMode === 'go2rtc') {
            indicator.textContent = 'go2rtc';
            indicator.className = 'stream-mode-indicator mode-go2rtc';
            indicator.title = 'Full streaming available (RTSP/WebRTC/MJPEG)';
        } else if (streamingMode === 'native_webrtc') {
            indicator.textContent = 'Python Native WebRTC';
            indicator.className = 'stream-mode-indicator mode-native-webrtc';
            indicator.title = 'Python native WebRTC mode - RTSP unavailable';
        } else {
            indicator.textContent = 'Python Native MJPEG';
            indicator.className = 'stream-mode-indicator mode-mjpeg';
            indicator.title = 'Python native MJPEG only - go2rtc/aiortc not available';
        }
    }
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
            
            if (currentStreamType === 'mjpeg') {
                // Viewing MJPEG - show MJPEG stats
                fps = data.mjpeg_fps ?? data.actual_fps ?? '-';
                frames = data.mjpeg_frames_sent ?? data.frames_sent ?? '-';
                uptime = data.mjpeg_elapsed_time ?? data.elapsed_time;
            } else if (currentStreamType === 'native_rtc') {
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
        })
        .catch(err => {
            console.error('Failed to fetch stats:', err);
            document.getElementById('status').className = 'status offline';
        });
}

/**
 * Apply configuration changes
 */
function applyConfig() {
    const mainRes = document.getElementById('main_res').value.split('x');
    const subRes = document.getElementById('sub_res').value.split('x');
    
    const config = {
        main_width: parseInt(mainRes[0]),
        main_height: parseInt(mainRes[1]),
        main_fps: parseInt(document.getElementById('main_fps').value),
        main_bitrate: document.getElementById('main_bitrate').value,
        sub_width: parseInt(subRes[0]),
        sub_height: parseInt(subRes[1]),
        sub_bitrate: document.getElementById('sub_bitrate').value,
        hw_accel: document.getElementById('hw_accel').value,
        show_timestamp: document.getElementById('show_timestamp').checked,
        timestamp_position: document.getElementById('timestamp_position').value
    };
    
    const statusEl = document.getElementById('apply-status');
    statusEl.textContent = 'Saving...';
    
    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            if (data.restarted) {
                statusEl.textContent = 'Applied! Stream restarted.';
            } else if (data.restart_needed) {
                statusEl.textContent = 'Saved! Stream restart failed.';
                statusEl.style.color = '#ffaa00';
            } else {
                statusEl.textContent = 'Saved!';
            }
            if (!data.restart_needed || data.restarted) {
                statusEl.style.color = '#00ff88';
            }
        } else {
            statusEl.textContent = 'Error: ' + data.error;
            statusEl.style.color = '#e94560';
        }
        // Reset status after 3 seconds
        setTimeout(() => {
            statusEl.textContent = '';
            statusEl.style.color = '#888';
        }, 3000);
    })
    .catch(err => {
        statusEl.textContent = 'Error: ' + err.message;
        statusEl.style.color = '#e94560';
    });
}

/**
 * Load current configuration and update form
 */
function loadConfig() {
    fetch('/api/config')
        .then(r => r.json())
        .then(config => {
            // Update form values based on current config
            const mainResSelect = document.getElementById('main_res');
            const mainRes = `${config.main_width}x${config.main_height}`;
            for (let opt of mainResSelect.options) {
                opt.selected = opt.value === mainRes;
            }
            
            const mainFpsSelect = document.getElementById('main_fps');
            for (let opt of mainFpsSelect.options) {
                opt.selected = opt.value === String(config.main_fps);
            }
            
            const mainBitrateSelect = document.getElementById('main_bitrate');
            for (let opt of mainBitrateSelect.options) {
                opt.selected = opt.value === config.main_bitrate;
            }
            
            const subResSelect = document.getElementById('sub_res');
            const subRes = `${config.sub_width}x${config.sub_height}`;
            for (let opt of subResSelect.options) {
                opt.selected = opt.value === subRes;
            }
            
            const subBitrateSelect = document.getElementById('sub_bitrate');
            for (let opt of subBitrateSelect.options) {
                opt.selected = opt.value === config.sub_bitrate;
            }
            
            const hwAccelSelect = document.getElementById('hw_accel');
            for (let opt of hwAccelSelect.options) {
                opt.selected = opt.value === config.hw_accel;
            }
            
            // Overlay settings
            document.getElementById('show_timestamp').checked = config.show_timestamp !== false;
            
            const timestampPosSelect = document.getElementById('timestamp_position');
            for (let opt of timestampPosSelect.options) {
                opt.selected = opt.value === config.timestamp_position;
            }
            
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
    // Check stream availability first (may switch to MJPEG mode)
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
