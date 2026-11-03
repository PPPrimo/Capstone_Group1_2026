#!/usr/bin/env bash
set -e

echo "Starting Redis..."

if ! redis-cli ping >/dev/null 2>&1; then
    sudo service redis-server start
fi

until redis-cli ping 2>/dev/null | grep -q "PONG"; do
    echo "Waiting for Redis..."
    sleep 1
done

echo "Redis is ready."

if ! command -v turnserver >/dev/null; then
    echo "Install coturn once: wsl.exe -d Ubuntu -u root -- apt-get install -y coturn"
    exit 1
fi
