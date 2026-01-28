FROM python:3.11-slim

# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    ffmpeg \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Download go2rtc binary
RUN wget -q https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_amd64 -O /app/go2rtc \
    && chmod +x /app/go2rtc

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Install the package
RUN pip install --no-cache-dir -e .

# Create videos directory for uploads
RUN mkdir -p /app/videos

# Copy and setup entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Expose ports
# 8080 - ONVIF/Web UI
# 3702 - WS-Discovery (UDP)
# 1984 - go2rtc API
# 8554 - RTSP
# 1935 - RTMP
# 8555 - WebRTC
EXPOSE 8080
EXPOSE 3702/udp
EXPOSE 1984
EXPOSE 8554
EXPOSE 1935
EXPOSE 8555/udp

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Default command - start in video upload mode
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["--source", "video"]
CMD ["--source", "video"]
