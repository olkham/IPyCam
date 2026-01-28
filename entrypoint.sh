#!/bin/bash
set -e

# Start go2rtc in background
echo "Starting go2rtc..."
/app/go2rtc -config /app/ipycam/go2rtc.yaml &
GO2RTC_PID=$!

# Wait for go2rtc to be ready
echo "Waiting for go2rtc to be ready..."
for i in {1..30}; do
    if wget -q --spider http://127.0.0.1:1984/api 2>/dev/null; then
        echo "go2rtc is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "Warning: go2rtc did not become ready in time, continuing anyway..."
    fi
    sleep 0.5
done

# Start ipycam
echo "Starting ipycam..."
exec python -m ipycam "$@"
