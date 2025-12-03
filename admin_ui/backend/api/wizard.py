import docker
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
import os
import yaml
import subprocess
import stat
from typing import Dict, Any, Optional
from settings import ENV_PATH, CONFIG_PATH, ensure_env_file, PROJECT_ROOT

router = APIRouter()


def setup_host_symlink() -> dict:
    """Create /app/project symlink on host for Docker path resolution.
    
    The admin_ui container uses PROJECT_ROOT=/app/project internally.
    When docker-compose runs from inside the container, the docker daemon
    (on the host) resolves paths like /app/project/models on the HOST.
    This symlink ensures the host's /app/project points to the actual project.
    """
    results = {"success": True, "messages": [], "errors": []}
    
    try:
        client = docker.from_env()
        
        # Create symlink on host: /app/project -> actual project path
        # We detect the actual host path from the admin_ui container's mount
        admin_container = client.containers.get("admin_ui")
        mounts = admin_container.attrs.get("Mounts", [])
        
        # Find the mount for /app/project
        host_project_path = None
        for mount in mounts:
            if mount.get("Destination") == "/app/project":
                host_project_path = mount.get("Source")
                break
        
        if host_project_path:
            # Run alpine container to create symlink on host
            symlink_script = f'''
                mkdir -p /app 2>/dev/null || true
                if [ -L /app/project ]; then
                    # Symlink exists, check if pointing to correct path
                    CURRENT=$(readlink /app/project)
                    if [ "$CURRENT" = "{host_project_path}" ]; then
                        echo "Symlink already correct"
                        exit 0
                    fi
                fi
                rm -rf /app/project 2>/dev/null || true
                ln -sfn {host_project_path} /app/project
                echo "Created symlink /app/project -> {host_project_path}"
            '''
            output = client.containers.run(
                "alpine:latest",
                command=["sh", "-c", symlink_script],
                volumes={"/app": {"bind": "/app", "mode": "rw"}},
                remove=True,
            )
            results["messages"].append(output.decode().strip() if output else "Symlink setup complete")
        else:
            results["messages"].append("Could not detect host project path from mounts")
            
    except Exception as e:
        results["errors"].append(f"Symlink setup error: {e}")
    
    return results


def setup_media_paths() -> dict:
    """Setup media directories and symlink for Asterisk playback.
    
    Mirrors the setup_media_paths() function from install.sh to ensure
    the wizard provides the same out-of-box experience.
    """
    results = {
        "success": True,
        "messages": [],
        "errors": []
    }
    
    # First, ensure host symlink exists for Docker path resolution
    symlink_result = setup_host_symlink()
    results["messages"].extend(symlink_result.get("messages", []))
    results["errors"].extend(symlink_result.get("errors", []))
    
    # Path inside container (mounted from host)
    container_media_dir = "/mnt/asterisk_media/ai-generated"
    # Path on host (PROJECT_ROOT is mounted from host)
    host_media_dir = os.path.join(PROJECT_ROOT, "asterisk_media", "ai-generated")
    
    # 1. Create directories with proper permissions (775 = rwxrwxr-x)
    try:
        os.makedirs(host_media_dir, mode=0o775, exist_ok=True)
        # Ensure parent also has correct permissions
        os.chmod(os.path.dirname(host_media_dir), 0o775)
        os.chmod(host_media_dir, 0o775)
        results["messages"].append(f"Created media directory: {host_media_dir}")
    except Exception as e:
        results["errors"].append(f"Failed to create media directory: {e}")
        results["success"] = False
    
    # 2. Try to create symlink on host via docker exec on host system
    # This runs a privileged command to create the symlink
    try:
        # Check if we can access docker socket
        client = docker.from_env()
        
        # Get the actual host path for PROJECT_ROOT
        # The symlink should be: /var/lib/asterisk/sounds/ai-generated -> {PROJECT_ROOT}/asterisk_media/ai-generated
        # We need to detect the actual host path
        
        # Run a command on host to create the symlink
        # Using alpine image with host volume mounts
        symlink_script = f'''
            mkdir -p /mnt/asterisk_media/ai-generated 2>/dev/null || true
            chmod 775 /mnt/asterisk_media/ai-generated 2>/dev/null || true
            chmod 775 /mnt/asterisk_media 2>/dev/null || true
            if [ -L /var/lib/asterisk/sounds/ai-generated ] || [ -e /var/lib/asterisk/sounds/ai-generated ]; then
                rm -rf /var/lib/asterisk/sounds/ai-generated 2>/dev/null || true
            fi
            ln -sfn /mnt/asterisk_media/ai-generated /var/lib/asterisk/sounds/ai-generated 2>/dev/null || true
            if [ -d /var/lib/asterisk/sounds/ai-generated ]; then
                echo "SUCCESS: Symlink created"
            else
                echo "FALLBACK: Creating alternative symlink"
                # Try alternative path if /mnt/asterisk_media doesn't exist
                PROJ_MEDIA="{PROJECT_ROOT}/asterisk_media/ai-generated"
                if [ -d "$PROJ_MEDIA" ]; then
                    ln -sfn "$PROJ_MEDIA" /var/lib/asterisk/sounds/ai-generated 2>/dev/null || true
                fi
            fi
        '''
        
        # Run on host via privileged container
        container = client.containers.run(
            "alpine:latest",
            command=["sh", "-c", symlink_script],
            volumes={
                "/var/lib/asterisk/sounds": {"bind": "/var/lib/asterisk/sounds", "mode": "rw"},
                "/mnt/asterisk_media": {"bind": "/mnt/asterisk_media", "mode": "rw"},
                PROJECT_ROOT: {"bind": PROJECT_ROOT, "mode": "rw"},
            },
            remove=True,
            detach=False,
        )
        output = container.decode() if isinstance(container, bytes) else str(container)
        results["messages"].append(f"Symlink setup: {output.strip()}")
        
    except docker.errors.ImageNotFound:
        results["messages"].append("Alpine image not found, will pull on next attempt")
        try:
            client.images.pull("alpine:latest")
            results["messages"].append("Pulled alpine image")
        except:
            results["errors"].append("Could not pull alpine image for symlink setup")
    except Exception as e:
        # Symlink creation failed, provide manual instructions
        results["messages"].append(f"Auto symlink setup skipped: {e}")
        results["messages"].append(
            "Manual setup required: Run on host:\n"
            f"  sudo ln -sfn {PROJECT_ROOT}/asterisk_media/ai-generated /var/lib/asterisk/sounds/ai-generated"
        )
    
    return results


@router.post("/init-env")
async def init_env():
    """Initialize .env from .env.example on first wizard step.
    
    Called when user clicks Next from step 1 (provider selection).
    This ensures .env exists with default values before proceeding.
    """
    created = ensure_env_file()
    return {"created": created, "env_path": ENV_PATH}


@router.get("/load-config")
async def load_existing_config():
    """Load existing configuration from .env file.
    
    Used to pre-populate wizard fields if config already exists.
    """
    from dotenv import dotenv_values
    
    config = {}
    
    # Load from .env if it exists
    if os.path.exists(ENV_PATH):
        env_values = dotenv_values(ENV_PATH)
        config = {
            "asterisk_host": env_values.get("ASTERISK_HOST", "127.0.0.1"),
            "asterisk_username": env_values.get("ASTERISK_ARI_USERNAME", ""),
            "asterisk_password": env_values.get("ASTERISK_ARI_PASSWORD", ""),
            "asterisk_port": int(env_values.get("ASTERISK_ARI_PORT", "8088")),
            "asterisk_scheme": env_values.get("ASTERISK_ARI_SCHEME", "http"),
            "asterisk_app": env_values.get("ASTERISK_ARI_APP", "asterisk-ai-voice-agent"),
            "openai_key": env_values.get("OPENAI_API_KEY", ""),
            "deepgram_key": env_values.get("DEEPGRAM_API_KEY", ""),
            "google_key": env_values.get("GOOGLE_API_KEY", ""),
            "elevenlabs_key": env_values.get("ELEVENLABS_API_KEY", ""),
        }
    
    # Load AI config from YAML if it exists
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                yaml_config = yaml.safe_load(f)
            
            # Get default context settings
            default_ctx = yaml_config.get("contexts", {}).get("default", {})
            config["ai_name"] = default_ctx.get("ai_name", "Asterisk Agent")
            config["ai_role"] = default_ctx.get("ai_role", "")
            config["greeting"] = default_ctx.get("greeting", "")
            
            # Try to detect provider from config
            if default_ctx.get("provider"):
                config["provider"] = default_ctx.get("provider")
        except:
            pass
    
    return config


@router.get("/engine-status")
async def get_engine_status():
    """Check if ai-engine container is running.
    
    Used in wizard completion step to determine if user needs
    to start the engine (first time) or if it's already running.
    """
    try:
        client = docker.from_env()
        try:
            container = client.containers.get("ai_engine")
            return {
                "running": container.status == "running",
                "status": container.status,
                "exists": True
            }
        except docker.errors.NotFound:
            return {
                "running": False,
                "status": "not_found",
                "exists": False
            }
    except Exception as e:
        return {
            "running": False,
            "status": "error",
            "exists": False,
            "error": str(e)
        }


@router.post("/setup-media-paths")
async def setup_media_paths_endpoint():
    """Setup media directories and symlinks for Asterisk audio playback.
    
    This endpoint ensures the AI Engine can write audio files that Asterisk
    can read for playback. Creates directories and symlinks as needed.
    """
    result = setup_media_paths()
    return result


@router.post("/start-engine")
async def start_engine():
    """Start the ai-engine container.
    
    Called from wizard completion step when user clicks 'Start AI Engine'.
    Uses docker-compose to create/start the container.
    Uses --force-recreate if container is already running.
    
    Automatically sets up media paths before starting to ensure audio playback works.
    """
    import subprocess
    from settings import PROJECT_ROOT
    
    print(f"DEBUG: Starting AI Engine from PROJECT_ROOT={PROJECT_ROOT}")
    
    # First, setup media paths for audio playback
    print("DEBUG: Setting up media paths...")
    media_setup = setup_media_paths()
    print(f"DEBUG: Media setup result: {media_setup}")
    
    # Check if container is already running
    already_running = False
    try:
        client = docker.from_env()
        try:
            container = client.containers.get("ai_engine")
            already_running = container.status == "running"
            print(f"DEBUG: ai_engine container status: {container.status}")
        except docker.errors.NotFound:
            print("DEBUG: ai_engine container not found, will create")
    except Exception as e:
        print(f"DEBUG: Could not check container status: {e}")
    
    try:
        # Use --force-recreate if already running to ensure fresh start with latest config
        cmd = ["docker", "compose", "up", "-d"]
        
        # Explicitly remove container if it exists to avoid "Conflict" errors
        # This handles cases where the container exists but isn't managed by compose correctly
        try:
            client = docker.from_env()
            try:
                old_container = client.containers.get("ai_engine")
                print(f"DEBUG: Removing existing ai_engine container ({old_container.status})")
                old_container.remove(force=True)
            except docker.errors.NotFound:
                pass
        except Exception as e:
            print(f"DEBUG: Error removing container: {e}")

        if already_running:
            cmd.append("--force-recreate")
            print("DEBUG: Container already running, using --force-recreate")
        cmd.append("ai-engine")
        
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120  # Give more time for potential build
        )
        
        print(f"DEBUG: docker-compose returncode={result.returncode}")
        print(f"DEBUG: docker-compose stdout={result.stdout}")
        print(f"DEBUG: docker-compose stderr={result.stderr}")
        
        if result.returncode == 0:
            return {
                "success": True,
                "action": "started",
                "message": "AI Engine started successfully" + (" (recreated)" if already_running else ""),
                "output": result.stdout,
                "media_setup": media_setup,
                "recreated": already_running
            }
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            return {
                "success": False,
                "action": "error",
                "message": f"Failed to start AI Engine: {error_msg}",
                "media_setup": media_setup
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "action": "timeout",
            "message": "Timeout waiting for AI Engine to start (120s). Check docker-compose logs.",
            "media_setup": media_setup
        }
    except FileNotFoundError as e:
        print(f"DEBUG: FileNotFoundError: {e}")
        return {
            "success": False,
            "action": "not_found",
            "message": "docker-compose not found. Please install Docker Compose.",
            "media_setup": media_setup
        }
    except Exception as e:
        print(f"DEBUG: Exception: {type(e).__name__}: {e}")
        return {"success": False, "action": "error", "message": str(e), "media_setup": media_setup}

# ============== Local AI Server Setup ==============

@router.get("/local/detect-tier")
async def detect_local_tier():
    """Detect system tier for local AI models based on CPU/RAM/GPU."""
    import subprocess
    from settings import PROJECT_ROOT
    
    try:
        # Get system info
        import psutil
        cpu_count = psutil.cpu_count()
        ram_gb = psutil.virtual_memory().total // (1024**3)
        
        # Check for GPU
        gpu_detected = False
        try:
            result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
            if result.returncode == 0:
                gpu_detected = True
        except:
            pass
        
        # Determine tier
        if gpu_detected:
            if ram_gb >= 32 and cpu_count >= 8:
                tier = "HEAVY_GPU"
            elif ram_gb >= 16 and cpu_count >= 4:
                tier = "MEDIUM_GPU"
            else:
                tier = "LIGHT_CPU"
        else:
            if ram_gb >= 32 and cpu_count >= 16:
                tier = "HEAVY_CPU"
            elif ram_gb >= 16 and cpu_count >= 8:
                tier = "MEDIUM_CPU"
            elif ram_gb >= 8 and cpu_count >= 4:
                tier = "LIGHT_CPU"
            else:
                tier = "LIGHT_CPU"
        
        # Tier descriptions
        tier_info = {
            "LIGHT_CPU": {
                "models": "TinyLlama 1.1B + Vosk Small + Piper Medium",
                "performance": "25-40 seconds per turn",
                "download_size": "~1.5 GB"
            },
            "MEDIUM_CPU": {
                "models": "Phi-3-mini 3.8B + Vosk 0.22 + Piper Medium",
                "performance": "20-30 seconds per turn",
                "download_size": "~3.5 GB"
            },
            "HEAVY_CPU": {
                "models": "Phi-3-mini 3.8B + Vosk 0.22 + Piper Medium",
                "performance": "25-35 seconds per turn",
                "download_size": "~3.5 GB"
            },
            "MEDIUM_GPU": {
                "models": "Phi-3-mini 3.8B + Vosk 0.22 + Piper Medium (GPU)",
                "performance": "8-12 seconds per turn",
                "download_size": "~3.5 GB"
            },
            "HEAVY_GPU": {
                "models": "Llama-2 13B + Vosk 0.22 + Piper High (GPU)",
                "performance": "10-15 seconds per turn",
                "download_size": "~10 GB"
            }
        }
        
        return {
            "cpu_cores": cpu_count,
            "ram_gb": ram_gb,
            "gpu_detected": gpu_detected,
            "tier": tier,
            "tier_info": tier_info.get(tier, {})
        }
    except Exception as e:
        return {"error": str(e)}


_download_process = None
_download_output = []
_download_status = {"running": False, "completed": False, "error": None}

@router.post("/local/download-models")
async def download_local_models(tier: str = "auto"):
    """Start model download in background. Returns immediately."""
    import subprocess
    import threading
    from settings import PROJECT_ROOT
    
    global _download_process, _download_output, _download_status
    
    # Reset state
    _download_output = []
    _download_status = {"running": True, "completed": False, "error": None}
    
    try:
        # Run model_setup.sh with --assume-yes
        cmd = ["bash", "scripts/model_setup.sh", "--assume-yes"]
        if tier != "auto":
            cmd.extend(["--tier", tier])
        
        def run_download():
            global _download_process, _download_output, _download_status
            try:
                process = subprocess.Popen(
                    cmd,
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                _download_process = process
                
                for line in iter(process.stdout.readline, ''):
                    if line:
                        _download_output.append(line.strip())
                        # Keep last 50 lines
                        if len(_download_output) > 50:
                            _download_output.pop(0)
                
                process.wait()
                _download_status["running"] = False
                _download_status["completed"] = process.returncode == 0
                if process.returncode != 0:
                    _download_status["error"] = f"Download failed with code {process.returncode}"
            except Exception as e:
                _download_status["running"] = False
                _download_status["error"] = str(e)
        
        # Start download thread
        thread = threading.Thread(target=run_download, daemon=True)
        thread.start()
        
        return {
            "status": "started",
            "message": "Model download started. This may take several minutes."
        }
    except Exception as e:
        _download_status = {"running": False, "completed": False, "error": str(e)}
        return {"status": "error", "message": str(e)}


@router.get("/local/download-progress")
async def get_download_progress():
    """Get current download progress and output."""
    global _download_output, _download_status
    
    return {
        "running": _download_status.get("running", False),
        "completed": _download_status.get("completed", False),
        "error": _download_status.get("error"),
        "output": _download_output[-20:] if _download_output else []  # Last 20 lines
    }


@router.get("/local/models-status")
async def check_models_status():
    """Check if required models are downloaded."""
    from settings import PROJECT_ROOT
    import os
    
    models_dir = os.path.join(PROJECT_ROOT, "models")
    
    # Check for model files
    stt_models = []
    llm_models = []
    tts_models = []
    
    stt_dir = os.path.join(models_dir, "stt")
    llm_dir = os.path.join(models_dir, "llm")
    tts_dir = os.path.join(models_dir, "tts")
    
    if os.path.exists(stt_dir):
        for item in os.listdir(stt_dir):
            if item.startswith("vosk-model"):
                stt_models.append(item)
    
    if os.path.exists(llm_dir):
        for item in os.listdir(llm_dir):
            if item.endswith(".gguf"):
                llm_models.append(item)
    
    if os.path.exists(tts_dir):
        for item in os.listdir(tts_dir):
            if item.endswith(".onnx"):
                tts_models.append(item)
    
    ready = len(stt_models) > 0 and len(llm_models) > 0 and len(tts_models) > 0
    
    return {
        "ready": ready,
        "stt_models": stt_models,
        "llm_models": llm_models,
        "tts_models": tts_models
    }


@router.post("/local/start-server")
async def start_local_ai_server():
    """Start the local-ai-server container.
    
    Also sets up media paths for audio playback to work correctly.
    Uses --force-recreate to handle cases where container is already running.
    """
    import subprocess
    from settings import PROJECT_ROOT
    
    # Setup media paths first (same as start_engine)
    print("DEBUG: Setting up media paths for local AI server...")
    media_setup = setup_media_paths()
    print(f"DEBUG: Media setup result: {media_setup}")
    
    # Check if container is already running
    already_running = False
    try:
        client = docker.from_env()
        try:
            container = client.containers.get("local_ai_server")
            already_running = container.status == "running"
            print(f"DEBUG: local_ai_server container status: {container.status}")
        except docker.errors.NotFound:
            print("DEBUG: local_ai_server container not found, will create")
    except Exception as e:
        print(f"DEBUG: Could not check container status: {e}")
    
    try:
        # Use --force-recreate if already running to ensure fresh start
        cmd = ["docker", "compose", "up", "-d"]
        
        # Explicitly remove container if it exists to avoid "Conflict" errors
        try:
            client = docker.from_env()
            try:
                old_container = client.containers.get("local_ai_server")
                print(f"DEBUG: Removing existing local_ai_server container ({old_container.status})")
                old_container.remove(force=True)
            except docker.errors.NotFound:
                pass
        except Exception as e:
            print(f"DEBUG: Error removing container: {e}")

        if already_running:
            cmd.append("--force-recreate")
            print("DEBUG: Container already running, using --force-recreate")
        cmd.append("local-ai-server")
        
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode == 0:
            return {
                "success": True,
                "message": "Local AI Server started successfully" + (" (recreated)" if already_running else ""),
                "media_setup": media_setup,
                "recreated": already_running
            }
        else:
            return {
                "success": False,
                "message": f"Failed to start: {result.stderr or result.stdout}",
                "media_setup": media_setup
            }
    except Exception as e:
        return {"success": False, "message": str(e), "media_setup": media_setup}


@router.get("/local/server-logs")
async def get_local_server_logs():
    """Get local-ai-server container logs."""
    import subprocess
    
    try:
        # Get recent logs for display
        result = subprocess.run(
            ["docker", "logs", "--tail", "30", "local_ai_server"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        logs = result.stdout or result.stderr
        lines = logs.strip().split('\n') if logs else []
        
        # Check if server is ready by looking at ALL logs (not just tail)
        # The startup message might be pushed out by connection logs
        ready_result = subprocess.run(
            ["docker", "logs", "local_ai_server"],
            capture_output=True,
            text=True,
            timeout=10
        )
        all_logs = (ready_result.stdout or "") + (ready_result.stderr or "")
        
        # Check for ready indicators in full log history
        ready = "Enhanced Local AI Server started" in all_logs or \
                "All models loaded successfully" in all_logs or \
                "models loaded" in all_logs.lower()
        
        return {
            "logs": lines[-20:],
            "ready": ready
        }
    except subprocess.TimeoutExpired:
        return {"logs": [], "ready": False, "error": "Timeout getting logs"}
    except Exception as e:
        return {"logs": [], "ready": False, "error": str(e)}


@router.get("/local/server-status")
async def get_local_server_status():
    """Check if local-ai-server is running and healthy."""
    import docker
    import httpx
    
    try:
        client = docker.from_env()
        try:
            container = client.containers.get("local_ai_server")
            running = container.status == "running"
        except:
            running = False
        
        # Try health check
        healthy = False
        if running:
            try:
                async with httpx.AsyncClient() as http_client:
                    response = await http_client.get("http://127.0.0.1:8000/health", timeout=5.0)
                    healthy = response.status_code == 200
            except:
                pass
        
        return {
            "running": running,
            "healthy": healthy
        }
    except Exception as e:
        return {"running": False, "healthy": False, "error": str(e)}


class ApiKeyValidation(BaseModel):
    provider: str
    api_key: str

class AsteriskConnection(BaseModel):
    host: str
    username: str
    password: str
    port: int = 8088
    scheme: str = "http"
    app: str = "asterisk-ai-voice-agent"

@router.post("/validate-key")
async def validate_api_key(validation: ApiKeyValidation):
    """Validate an API key by testing it against the provider's API"""
    try:
        import httpx
        
        provider = validation.provider.lower()
        api_key = validation.api_key
        
        if not api_key:
            return {"valid": False, "error": "API key is empty"}
        
        async with httpx.AsyncClient() as client:
            if provider == "openai":
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10.0
                )
                if response.status_code == 200:
                    return {"valid": True, "message": "OpenAI API key is valid"}
                elif response.status_code == 401:
                    return {"valid": False, "error": "Invalid API key"}
                else:
                    return {"valid": False, "error": f"API error: HTTP {response.status_code}"}
                    
            elif provider == "deepgram":
                response = await client.get(
                    "https://api.deepgram.com/v1/projects",
                    headers={"Authorization": f"Token {api_key}"},
                    timeout=10.0
                )
                if response.status_code == 200:
                    return {"valid": True, "message": "Deepgram API key is valid"}
                elif response.status_code == 401:
                    return {"valid": False, "error": "Invalid API key"}
                else:
                    return {"valid": False, "error": f"API error: HTTP {response.status_code}"}
                    
            elif provider == "google":
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                    timeout=10.0
                )
                if response.status_code == 200:
                    return {"valid": True, "message": "Google API key is valid"}
                elif response.status_code in [400, 403]:
                    return {"valid": False, "error": "Invalid API key"}
                else:
                    return {"valid": False, "error": f"API error: HTTP {response.status_code}"}
            
            elif provider == "elevenlabs":
                # Validate ElevenLabs API key by fetching user info
                response = await client.get(
                    "https://api.elevenlabs.io/v1/user",
                    headers={"xi-api-key": api_key},
                    timeout=10.0
                )
                if response.status_code == 200:
                    return {"valid": True, "message": "ElevenLabs API key is valid"}
                elif response.status_code == 401:
                    return {"valid": False, "error": "Invalid API key"}
                else:
                    return {"valid": False, "error": f"API error: HTTP {response.status_code}"}
            
            else:
                return {"valid": False, "error": f"Unknown provider: {provider}"}
                
    except httpx.TimeoutException:
        return {"valid": False, "error": "Connection timeout"}
    except Exception as e:
        return {"valid": False, "error": str(e)}

@router.post("/validate-connection")
async def validate_asterisk_connection(conn: AsteriskConnection):
    """Test Asterisk ARI connection"""
    try:
        import httpx
        
        # Try to connect to ARI interface
        base_url = f"{conn.scheme}://{conn.host}:{conn.port}/ari"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url}/asterisk/info",
                auth=(conn.username, conn.password),
                timeout=5.0
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "valid": True,
                    "message": f"Connected to Asterisk {data.get('system', {}).get('version', 'Unknown')}"
                }
            elif response.status_code == 401:
                return {"valid": False, "error": "Invalid username or password"}
            else:
                return {"valid": False, "error": f"Connection failed: HTTP {response.status_code}"}
                
    except httpx.ConnectError:
        return {"valid": False, "error": f"Cannot connect to {conn.host}:{conn.port} - Is Asterisk running?"}
    except httpx.TimeoutException:
        return {"valid": False, "error": "Connection timeout"}
    except Exception as e:
        return {"valid": False, "error": str(e)}

@router.get("/status")
async def get_setup_status():
    """
    Check if initial setup has been completed
    Returns configured: true if .env exists with required keys
    """
    try:
        if not os.path.exists(ENV_PATH):
            return {"configured": False, "message": "Environment file not found"}
        
        # Read .env and check for minimal required config
        with open(ENV_PATH, 'r') as f:
            content = f.read()
            has_asterisk_host = "ASTERISK_HOST=" in content
            has_username = "ASTERISK_ARI_USERNAME=" in content
            
            if has_asterisk_host and has_username:
                return {"configured": True, "message": "Setup complete"}
            else:
                return {"configured": False, "message": "Incomplete configuration"}
                
    except Exception as e:
        return {"configured": False, "message": str(e)}

class SetupConfig(BaseModel):
    provider: str = "openai_realtime"
    asterisk_host: str
    asterisk_username: str
    asterisk_password: str
    asterisk_port: int = 8088
    asterisk_scheme: str = "http"
    asterisk_app: str = "asterisk-ai-voice-agent"
    openai_key: Optional[str] = None
    deepgram_key: Optional[str] = None
    google_key: Optional[str] = None
    elevenlabs_key: Optional[str] = None
    elevenlabs_agent_id: Optional[str] = None
    cartesia_key: Optional[str] = None
    greeting: str
    ai_name: str
    ai_role: str

# ... (keep existing endpoints) ...

@router.post("/save")
async def save_setup_config(config: SetupConfig):
    # Validation: Check for required keys based on provider
    if config.provider == "openai_realtime" and not config.openai_key:
            raise HTTPException(status_code=400, detail="OpenAI API Key is required for OpenAI Realtime provider")
    if config.provider == "deepgram" and not config.deepgram_key:
            raise HTTPException(status_code=400, detail="Deepgram API Key is required for Deepgram provider")
    if config.provider == "google_live" and not config.google_key:
            raise HTTPException(status_code=400, detail="Google API Key is required for Google Live provider")
    # Local hybrid uses OpenAI for LLM, so check that too
    if config.provider == "local_hybrid" and not config.openai_key:
            raise HTTPException(status_code=400, detail="OpenAI API Key is required for Local Hybrid pipeline (LLM)")
    if config.provider == "elevenlabs_agent":
        if not config.elevenlabs_key:
            raise HTTPException(status_code=400, detail="ElevenLabs API Key is required for ElevenLabs Conversational provider")
        if not config.elevenlabs_agent_id:
            raise HTTPException(status_code=400, detail="ElevenLabs Agent ID is required for ElevenLabs Conversational provider")

    try:
        import shutil
        import datetime
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Backup existing files
        if os.path.exists(ENV_PATH):
            shutil.copy2(ENV_PATH, f"{ENV_PATH}.bak.{timestamp}")
            
        if os.path.exists(CONFIG_PATH):
            shutil.copy2(CONFIG_PATH, f"{CONFIG_PATH}.bak.{timestamp}")

        # 1. Update .env
        env_updates = {
            "ASTERISK_HOST": config.asterisk_host,
            "ASTERISK_ARI_USERNAME": config.asterisk_username,
            "ASTERISK_ARI_PASSWORD": config.asterisk_password,
            "ASTERISK_ARI_PORT": str(config.asterisk_port),
            "ASTERISK_ARI_SCHEME": config.asterisk_scheme,
            "ASTERISK_APP_NAME": config.asterisk_app,
            "AI_NAME": config.ai_name,
            "AI_ROLE": config.ai_role,
            "AI_GREETING": config.greeting
        }
        
        if config.openai_key:
            env_updates["OPENAI_API_KEY"] = config.openai_key
        if config.deepgram_key:
            env_updates["DEEPGRAM_API_KEY"] = config.deepgram_key
        if config.google_key:
            env_updates["GOOGLE_API_KEY"] = config.google_key
        if config.elevenlabs_key:
            env_updates["ELEVENLABS_API_KEY"] = config.elevenlabs_key
        if config.elevenlabs_agent_id:
            env_updates["ELEVENLABS_AGENT_ID"] = config.elevenlabs_agent_id
        if config.cartesia_key:
            env_updates["CARTESIA_API_KEY"] = config.cartesia_key

        current_env = {}
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        current_env[key] = val
        
        current_env.update(env_updates)
        
        with open(ENV_PATH, "w") as f:
            for key, val in current_env.items():
                f.write(f"{key}={val}\n")

        # 2. Update ai-agent.yaml
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                yaml_config = yaml.safe_load(f)
            
            # Update default provider based on provider selection
            # Update default provider based on provider selection
            if config.provider == "openai_realtime":
                yaml_config["default_provider"] = "openai_realtime"
                yaml_config.setdefault("providers", {})
                yaml_config["providers"].setdefault("openai_realtime", {})["enabled"] = True
                yaml_config["providers"]["openai_realtime"]["greeting"] = config.greeting
                yaml_config["providers"].setdefault("deepgram", {})["enabled"] = False
                yaml_config["providers"].setdefault("local", {})["enabled"] = False
                yaml_config["providers"].setdefault("google_live", {})["enabled"] = False
                
            elif config.provider == "deepgram":
                yaml_config["default_provider"] = "deepgram"
                yaml_config.setdefault("providers", {})
                yaml_config["providers"].setdefault("deepgram", {})["enabled"] = True
                yaml_config["providers"]["deepgram"]["greeting"] = config.greeting
                yaml_config["providers"].setdefault("openai_realtime", {})["enabled"] = False
                yaml_config["providers"].setdefault("local", {})["enabled"] = False
                yaml_config["providers"].setdefault("google_live", {})["enabled"] = False
                
            elif config.provider == "local_hybrid":
                # C4 Fix: local_hybrid is a pipeline, not a provider
                yaml_config["active_pipeline"] = "local_hybrid"
                yaml_config["default_provider"] = "local"
                yaml_config.setdefault("providers", {})
                yaml_config["providers"].setdefault("local", {})["enabled"] = True
                # Ensure OpenAI is enabled for hybrid pipeline (LLM)
                yaml_config["providers"].setdefault("openai", {})["enabled"] = True
                yaml_config["providers"].setdefault("openai_realtime", {})["enabled"] = False
                yaml_config["providers"].setdefault("deepgram", {})["enabled"] = False
                yaml_config["providers"].setdefault("google_live", {})["enabled"] = False
                
                # Start local-ai-server container
                try:
                    client = docker.from_env()
                    # Check if container exists
                    try:
                        container = client.containers.get("local_ai_server")
                        if container.status != "running":
                            container.start()
                    except docker.errors.NotFound:
                        print("Warning: local_ai_server container not found. Please run 'docker compose up -d local-ai-server'")
                except Exception as e:
                    print(f"Error starting local_ai_server: {e}")
                    # Don't fail the wizard if docker fails, just log it


            elif config.provider == "google_live":
                yaml_config["default_provider"] = "google_live"
                yaml_config.setdefault("providers", {})
                yaml_config["providers"].setdefault("google_live", {})["enabled"] = True
                yaml_config["providers"]["google_live"]["greeting"] = config.greeting
                yaml_config["providers"].setdefault("openai_realtime", {})["enabled"] = False
                yaml_config["providers"].setdefault("deepgram", {})["enabled"] = False
                yaml_config["providers"].setdefault("local", {})["enabled"] = False

            elif config.provider == "elevenlabs_agent":
                yaml_config["default_provider"] = "elevenlabs_agent"
                yaml_config.setdefault("providers", {})
                yaml_config["providers"].setdefault("elevenlabs_agent", {})["enabled"] = True
                yaml_config["providers"]["elevenlabs_agent"]["api_key"] = "${ELEVENLABS_API_KEY}"
                yaml_config["providers"]["elevenlabs_agent"]["agent_id"] = "${ELEVENLABS_AGENT_ID}"
                # ElevenLabs greeting is configured in the agent dashboard, not here
                yaml_config["providers"].setdefault("openai_realtime", {})["enabled"] = False
                yaml_config["providers"].setdefault("deepgram", {})["enabled"] = False
                yaml_config["providers"].setdefault("google_live", {})["enabled"] = False
                yaml_config["providers"].setdefault("local", {})["enabled"] = False

            elif config.provider == "local":
                # Local Full: 100% on-premises using Local AI Server as full agent
                yaml_config["default_provider"] = "local"
                yaml_config.setdefault("providers", {})
                yaml_config["providers"].setdefault("local", {})["enabled"] = True
                yaml_config["providers"]["local"]["greeting"] = config.greeting
                yaml_config["providers"]["local"]["type"] = "full"
                yaml_config["providers"]["local"]["capabilities"] = ["stt", "llm", "tts"]
                yaml_config["providers"].setdefault("openai_realtime", {})["enabled"] = False
                yaml_config["providers"].setdefault("deepgram", {})["enabled"] = False
                yaml_config["providers"].setdefault("google_live", {})["enabled"] = False
                
                # Start local-ai-server container
                try:
                    client = docker.from_env()
                    try:
                        container = client.containers.get("local_ai_server")
                        if container.status != "running":
                            container.start()
                    except docker.errors.NotFound:
                        print("Warning: local_ai_server container not found. Please run 'docker compose up -d local-ai-server'")
                except Exception as e:
                    print(f"Error starting local_ai_server: {e}")

            # C6 Fix: Create default context
            yaml_config.setdefault("contexts", {})["default"] = {
                "greeting": config.greeting,
                "prompt": f"You are {config.ai_name}, a {config.ai_role}. Be helpful and concise.",
                "provider": config.provider if config.provider != "local_hybrid" else "local",
                "profile": "telephony_ulaw_8k"
            }

            with open(CONFIG_PATH, "w") as f:
                yaml.dump(yaml_config, f, default_flow_style=False)
        
        # Config saved - engine start will be handled by completion step UI
        return {"status": "success", "provider": config.provider}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/skip")
async def skip_setup():
    """
    Skip the setup wizard by creating a minimal .env file
    This allows advanced users to configure manually
    """
    try:
        # Create minimal .env with a marker that setup was acknowledged
        if not os.path.exists(ENV_PATH):
            with open(ENV_PATH, 'w') as f:
                f.write("# Setup wizard skipped - configure manually\n")
                f.write("ASTERISK_HOST=127.0.0.1\n")
                f.write("ASTERISK_ARI_USERNAME=asterisk\n")
                f.write("ASTERISK_ARI_PASSWORD=\n")
        
        return {"status": "success", "message": "Setup skipped successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
