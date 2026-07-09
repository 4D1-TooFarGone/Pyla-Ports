#!/usr/bin/env bash
# Double-click in Finder to launch Pyla on macOS.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/cfgs_and_internal"

# --- compiled binary (distribution) ---
if [ -f "./pyla_main" ]; then
    chmod +x ./pyla_main
    exec ./pyla_main
fi

# --- run from source ---
VENV_DIR=".venv"

# scrcpy-client requires Python <3.13, so avoid newer default python3 installs.
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver="$("$candidate" -c 'import sys; print(sys.version_info[0], sys.version_info[1])')"
        major="${ver% *}"
        minor="${ver#* }"
        if [ "$major" -eq 3 ] && [ "$minor" -lt 13 ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Error: no compatible Python (<3.13) found. Install one with 'brew install python@3.12'." >&2
    exit 1
fi

if [ -d "$VENV_DIR" ]; then
    venv_ver="$("$VENV_DIR/bin/python3" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo "")"
    if [ -z "$venv_ver" ] || [ "$venv_ver" -ge 13 ]; then
        echo "Existing virtual environment uses an incompatible Python version, recreating..."
        rm -rf "$VENV_DIR"
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment (first run only)..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

if [ -f "requirements.txt" ]; then
    echo "Installing/updating dependencies..."
    pip install -q -r requirements.txt
    pip install -q --no-deps --ignore-requires-python --force-reinstall \
        "git+https://github.com/leng-yue/py-scrcpy-client.git@v0.5.0"
fi

# --- download models if missing ---
MODELS_DIR="./models"
MODELS_URL="https://github.com/4D1-TooFarGone/Pyla-Ports/releases/download/models-v1"
mkdir -p "$MODELS_DIR"
for model in mainInGameModel.onnx tileDetector.onnx closeTileDetector.onnx; do
    if [ ! -f "$MODELS_DIR/$model" ]; then
        echo "Downloading $model..."
        curl -L --progress-bar -o "$MODELS_DIR/$model" "$MODELS_URL/$model"
    fi
done

# --- create match_history.csv if missing ---
if [ ! -f "./cfg/match_history.csv" ]; then
    echo "date_time,brawler_name,result,current_trophies,trophy_delta,new_winstreak,playstyle_hash,playstyle_name,playstyle_gamemodes,playstyle_brawlers,pyla_version,power_level" > "./cfg/match_history.csv"
fi

# --- create login.toml from template if missing ---
if [ ! -f "./cfg/login.toml" ] && [ -f "./cfg/login.toml.template" ]; then
    cp "./cfg/login.toml.template" "./cfg/login.toml"
fi

exec python3 main.py
