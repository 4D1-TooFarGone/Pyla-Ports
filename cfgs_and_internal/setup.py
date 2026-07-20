import sys
import platform
import subprocess
import os
import shutil
import glob
import importlib

# --- GIT CHECK ---
if not any(arg in sys.argv for arg in ["--help", "-h", "--version", "clean"]):
    if not shutil.which("git"):
        print("\n" + "!" * 80)
        print(" ERROR: 'git' command not found in your PATH.")
        print(" PylaAI requires Git to install dependencies (e.g. scrcpy-client).")
        print(" Please install Git from https://git-scm.com/ and add it to your PATH.")
        print("!" * 80 + "\n")
        sys.exit(1)

# --- LOOP-PROOF BOOTSTRAP ---

def bootstrap():
    if os.environ.get("PYLAAI_BOOTSTRAP") == "1":
        return
    try:
        import jaraco.functools
        import wheel
    except ImportError:
        print("\nDetected missing core tools. Stabilizing environment...")
        os.environ["PYLAAI_BOOTSTRAP"] = "1"
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        print("Environment stabilized. Restarting setup...\n")
        subprocess.run([sys.executable] + sys.argv)
        sys.exit(0)

if any(cmd in sys.argv for cmd in ["install", "develop"]):
    bootstrap()

from setuptools import setup, find_packages

def check_pytorch_status():
    """Returns 'cuda', 'mps', 'cpu', or 'missing'."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", torch.__version__
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            return "mps", torch.__version__
        return "cpu", torch.__version__
    except ImportError:
        return "missing", None

def get_requirement_name(req):
    req = req.strip()

    if " @ " in req:
        name = req.split(" @ ", 1)[0].strip()
    elif "@git+" in req:
        name = req.split("@git+", 1)[0].strip()
    else:
        name = req
        for sep in ["~=", ">=", "<=", "==", "!=", ">", "<"]:
            name = name.split(sep, 1)[0]

    name = name.split("[", 1)[0].strip()
    return name.replace("-", "_").lower()

def check_base_requirements(req_list):
    print("\nVerifying base requirements...")
    # Check scrcpy-client manually to avoid build-time dependency resolution conflicts
    for req in req_list + ["scrcpy-client"]:
        pkg_name = get_requirement_name(req)
        mapping = {
            "opencv_python": "cv2",
            "discord.py": "discord",
            "pillow": "PIL",
            "scrcpy_client": "scrcpy",
            "onnxruntime": "onnxruntime",
            "pycryptodome": "Crypto",
            "flask": "flask",
            "pandas": "pandas",
        }
        import_name = mapping.get(pkg_name, pkg_name)

        try:
            importlib.import_module(import_name)
            print(f"  [OK] {req}")
        except ImportError:
            print(f"  [INSTALLING] {req}")

def get_gpu_info():
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader,nounits"],
            encoding='utf-8'
        )
        cc = float(output.strip().split('\n')[0])
        return "nvidia", cc
    except:
        return "other", 0.0

def ask_user(prompt_text):
    print(f"{prompt_text} (Y/N): ", end='', flush=True)
    response = sys.stdin.readline().strip().lower()
    return response in ['y', 'yes']

# --- MAIN SETUP ---
install_requires = [
    "aiohttp~=3.13",
    "opencv-python~=4.11",
    "numpy~=2.3",
    "onnxruntime>=1.24",
    "requests~=2.32",
    "toml~=0.10",
    "torch~=2.8",
    "pillow>=11.2.1",
    "discord.py~=2.7",
    "packaging>=25.0",
    "easyocr~=1.7",
    "adbutils~=2.12",
    "av~=12.3",
    "Flask~=3.1",
    "pycryptodome~=3.21",
    "pandas~=3.0",
]

setup(
    name="PylaAI",
    version="1.0.0",
    packages=find_packages(exclude=["api", "cfg", "images", "models"]),
    install_requires=install_requires,
)

if any(cmd in sys.argv for cmd in ["install", "develop"]):
    try:
        check_base_requirements(install_requires)

        # --- SMART PYTORCH CHECK ---
        status, version = check_pytorch_status()
        gpu_type, cc = get_gpu_info()

        installed_pytorch = f"{status.upper()} Edition ({version})" if version else "Installing..."
        installed_cuda = "N/A"

        # Logic for NVIDIA users (Windows / Linux)
        if gpu_type == "nvidia":
            if status == "cuda":
                print(f"\n[SKIP] PyTorch with CUDA is already installed ({version}).")
                installed_cuda = "Already Present"
            else:
                print(f"\nNVIDIA GPU detected (CC: {cc})")
                if ask_user("Do you want to install/upgrade to PyTorch FULL (CUDA Support)?"):
                    if cc >= 12.0 or cc == 10.0:
                        subprocess.check_call([
                            sys.executable, "-m", "pip", "install", "--pre",
                            "torch", "torchvision", "torchaudio",
                            "--index-url", "https://download.pytorch.org/whl/nightly/cu128"
                        ])
                        installed_pytorch = "PyTorch (Nightly Full)"
                        installed_cuda = "12.8"
                    elif cc >= 8.9:
                        subprocess.check_call([
                            sys.executable, "-m", "pip", "install",
                            "torch", "torchvision", "torchaudio",
                            "--index-url", "https://download.pytorch.org/whl/cu124"
                        ])
                        installed_pytorch = "PyTorch (Stable Full)"
                        installed_cuda = "12.4"
                    else:
                        subprocess.check_call([
                            sys.executable, "-m", "pip", "install",
                            "torch", "torchvision", "torchaudio",
                            "--index-url", "https://download.pytorch.org/whl/cu118"
                        ])
                        installed_pytorch = "PyTorch (Stable Full)"
                        installed_cuda = "11.8"
                elif status == "missing":
                    print("Installing mandatory PyTorch CPU Edition...")
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "torch", "torchvision"])
                    installed_pytorch = "PyTorch (Standard CPU)"

            subprocess.check_call([sys.executable, "-m", "pip", "install", "onnxruntime-gpu"])
            installed_onnx = "ONNX Runtime (GPU)"

        # Logic for Mac / Apple Silicon / CPU-only users
        else:
            if status == "mps":
                print(f"\n[SKIP] PyTorch with MPS (Apple Silicon) is already installed ({version}).")
                installed_pytorch = f"PyTorch (MPS / Apple Silicon, {version})"
            elif status != "missing":
                print(f"\n[SKIP] PyTorch is already installed ({version}).")
            else:
                if sys.platform == 'darwin':
                    print("\nInstalling PyTorch (with MPS support for Apple Silicon)...")
                else:
                    print("\nInstalling mandatory PyTorch CPU...")
                subprocess.check_call([sys.executable, "-m", "pip", "install", "torch", "torchvision"])
                if sys.platform == 'darwin':
                    installed_pytorch = "PyTorch (MPS/CPU, Apple Silicon compatible)"
                else:
                    installed_pytorch = "PyTorch (Standard CPU)"

            subprocess.check_call([sys.executable, "-m", "pip", "install", "onnxruntime"])
            if sys.platform == 'darwin':
                installed_onnx = "ONNX Runtime (CoreML/CPU)"
            else:
                installed_onnx = "ONNX Runtime (CPU)"

        # Conflict Resolution
        subprocess.check_call([sys.executable, "-m", "pip", "install", "adbutils==2.12.0", "av==12.3.0"])
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--force-reinstall", "--no-deps",
            "git+https://github.com/leng-yue/py-scrcpy-client.git@v0.5.0"
        ])

        os.system('cls' if os.name == 'nt' else 'clear')
        print("\n" + "="*50 + "\n              SETUP COMPLETED!                \n" + "="*50)
        print(
            f"  - PyTorch:          {installed_pytorch}\n"
            f"  - CUDA Status:      {installed_cuda}\n"
            f"  - ONNX Engine:      {installed_onnx}\n"
            + "="*50 + "\n"
        )

    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
