#!/bin/bash
# Whisper Covenant API server on port 18803
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "🌊 Whisper Covenant API starting on http://localhost:18803"
cd "$DIR" && python3 whisper_api.py
