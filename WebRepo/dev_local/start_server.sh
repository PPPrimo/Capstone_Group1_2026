#!/usr/bin/env bash
set -e

PROJECT_DIR="/mnt/c/Users/tiant/Documents/1_My_folder/Projects/Capstone"

cd "$PROJECT_DIR"

echo "Starting Redis..."

if ! redis-cli ping >/dev/null 2>&1; then
    sudo service redis-server start
fi

until redis-cli ping 2>/dev/null | grep -q "PONG"; do
    echo "Waiting for Redis..."
    sleep 1
done

echo "Redis is ready."