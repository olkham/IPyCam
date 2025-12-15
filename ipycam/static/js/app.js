// IP Camera Web UI JavaScript

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
 * Switch between main and sub stream preview
 */
function switchStream(stream) {
    const iframe = document.getElementById('preview-iframe');
    const mainStream = iframe.dataset.mainStream;
    const subStream = iframe.dataset.subStream;
    const apiUrl = iframe.dataset.apiUrl;
    
    const streamName = stream === 'sub' ? subStream : mainStream;
    iframe.src = `${apiUrl}/stream.html?src=${streamName}`;
    
    // Update button states
    document.querySelectorAll('.btn-stream').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.stream === stream);
    });
}

/**
 * Update stats display from server
 */
function updateStats() {
    fetch('/api/stats')
        .then(r => r.json())
        .then(data => {
            document.getElementById('fps').textContent = data.actual_fps || '-';
            document.getElementById('frames').textContent = data.frames_sent || '-';
            document.getElementById('uptime').textContent = data.elapsed_time ? Math.floor(data.elapsed_time) + 's' : '-';
            document.getElementById('dropped').textContent = data.dropped_frames || '0';
            document.getElementById('status').className = 'status ' + (data.is_streaming ? 'online' : 'offline');
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
        hw_accel: document.getElementById('hw_accel').value
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
        })
        .catch(err => console.error('Failed to load config:', err));
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    // Start stats update interval
    setInterval(updateStats, 1000);
    updateStats();
    
    // Load current config
    loadConfig();
    
    // Load PTZ status
    updatePtzStatus();
});
