import asyncio
import os
import sys
from pathlib import Path
from aiolimiter import AsyncLimiter
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Fix for Windows: Playwright requires ProactorEventLoop to launch Chromium subprocesses
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Load environment variables from .env
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BLUEPRINT_CACHE_DIR = BASE_DIR / "blueprints_cache"
BLUEPRINT_CACHE_DIR.mkdir(exist_ok=True)

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")

# Initialize NVIDIA NIM client (OpenAI SDK compatible)
nvidia_client = AsyncOpenAI(
    base_url=NVIDIA_BASE_URL,
    api_key=NVIDIA_API_KEY,
)

# Keep requests under NVIDIA's 40 RPM free limit (max 35 calls per 60 seconds)
nvidia_limiter = AsyncLimiter(max_rate=35, time_period=60)