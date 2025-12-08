# Fully Local Setup Guide

Run Asterisk AI Voice Agent completely on-premises with no cloud APIs.

## Overview

This guide covers setting up a fully local deployment using:
- **STT**: Vosk or Sherpa-ONNX (supports multiple languages)
- **LLM**: Phi-3 Mini (or other GGUF models)
- **TTS**: Piper or Kokoro

## Prerequisites

- Docker and Docker Compose
- 8GB+ RAM (16GB recommended for better LLM performance)
- Modern CPU (2020+) for reasonable LLM inference speed

## Configuration

### 1. Environment Variables (.env)

```bash
# Local AI Server Configuration
LOCAL_STT_BACKEND=vosk
LOCAL_STT_MODEL_PATH=/app/models/stt/vosk-model-en-us-0.22
LOCAL_TTS_BACKEND=kokoro
LOCAL_TTS_VOICE=af_heart
LOCAL_LLM_MODEL_PATH=/app/models/llm/phi-3-mini-4k-instruct.Q4_K_M.gguf

# Disable cloud providers (no API keys needed)
# OPENAI_API_KEY=        # Leave empty or remove
# DEEPGRAM_API_KEY=      # Leave empty or remove
```

### 2. AI Agent Configuration (config/ai-agent.yaml)

```yaml
# Default to local provider
default_provider: local
active_pipeline: local_only
audio_transport: externalmedia

# Provider Configuration
providers:
  # Full local provider (all-in-one)
  local:
    type: full
    enabled: true
    capabilities: [stt, llm, tts]
    base_url: ws://local_ai_server:8765
    
  # Modular local providers (for pipelines)
  local_stt:
    type: local
    enabled: true
    capabilities: [stt]
    ws_url: ws://local_ai_server:8765
    
  local_llm:
    type: local
    enabled: true
    capabilities: [llm]
    ws_url: ws://local_ai_server:8765
    
  local_tts:
    type: local
    enabled: true
    capabilities: [tts]
    ws_url: ws://local_ai_server:8765

# Pipelines - Pure Local
pipelines:
  local_only:
    stt: local_stt
    llm: local_llm
    tts: local_tts
    options:
      stt:
        streaming: true
        chunk_ms: 160
        stream_format: pcm16_16k
        mode: stt
      llm:
        # NOTE: Do NOT include OpenAI model names here!
        # The local LLM path is configured in .env
        temperature: 0.7
        max_tokens: 150
      tts:
        format:
          encoding: mulaw
          sample_rate: 8000

# Context for your agent
contexts:
  default:
    provider: local
    greeting: |
      Hello! I'm your AI assistant running completely locally.
      How can I help you today?
    prompt: |
      You are a helpful AI assistant. Be concise and friendly.
      Keep responses under 2 sentences when possible.
```

### 3. Important Notes

**Do NOT include cloud model names in local pipeline options:**

```yaml
# ❌ WRONG - This will cause validation errors
pipelines:
  local_only:
    options:
      llm:
        model: gpt-4o-mini          # BAD! This is a cloud model
        base_url: https://api.openai.com/v1  # BAD! This is a cloud URL

# ✅ CORRECT - Let local_ai_server handle model selection
pipelines:
  local_only:
    options:
      llm:
        temperature: 0.7
        max_tokens: 150
        # Model path is set via LOCAL_LLM_MODEL_PATH in .env
```

## Starting the Services

```bash
# Start only local services (no cloud dependencies)
docker compose up -d local-ai-server ai-engine admin-ui

# Verify local-ai-server is healthy
docker logs local_ai_server | grep -E "STT|LLM|TTS"
```

Expected output:
```
✅ STT model loaded: /app/models/stt/vosk-model-en-us-0.22
✅ LLM model loaded: /app/models/llm/phi-3-mini-4k-instruct.Q4_K_M.gguf
✅ TTS backend: Kokoro initialized
```

## Model Downloads

Models are downloaded automatically on first start, or you can pre-download:

```bash
cd scripts
python3 model_setup.py
```

### Supported Models

**STT - Vosk** (offline, good accuracy):
- `vosk-model-en-us-0.22` (English, recommended)
- `vosk-model-small-en-us-0.15` (English, smaller/faster)
- `vosk-model-nl-0.22` (Dutch)
- See [Vosk Models](https://alphacephei.com/vosk/models)

**STT - Sherpa-ONNX** (streaming, lower latency):
- `sherpa-onnx-streaming-zipformer-en-2023-06-26` (English, recommended)
- `sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` (Chinese/English)
- See [Sherpa-ONNX Models](https://github.com/k2-fsa/sherpa-onnx/releases)

To use Sherpa instead of Vosk, set in `.env`:
```bash
LOCAL_STT_BACKEND=sherpa
LOCAL_STT_MODEL_PATH=/app/models/stt/sherpa-onnx-streaming-zipformer-en-2023-06-26
```

**LLM (GGUF)**:
- `phi-3-mini-4k-instruct.Q4_K_M.gguf` (recommended)
- `phi-3-mini-128k-instruct.Q4_K_M.gguf` (larger context)
- Any llama.cpp compatible GGUF model

**TTS**:
- Kokoro (built-in, high quality)
- Piper (various voices/languages)

## Troubleshooting

### Validation Errors

If you see "Pipeline LLM validation FAILED" but calls still work:
- This is expected for local providers
- The validation checks `ws://127.0.0.1:8765` which may not be reachable from ai-engine container
- The actual runtime uses `ws://local_ai_server:8765` which works via Docker networking

### Slow LLM Responses

Local LLM inference speed depends on hardware:
- Reduce `max_tokens` in pipeline options
- Use a smaller quantized model (Q4_K_M vs Q8)
- Consider GPU acceleration if available

### Call Drops After Greeting

Check that:
1. Local AI server is running: `docker ps | grep local`
2. WebSocket is accessible: `docker exec ai_engine curl -v ws://local_ai_server:8765`
3. Models are loaded: `docker logs local_ai_server | tail -50`

## Hardware Recommendations

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 8GB | 16GB |
| CPU | 4 cores | 8+ cores |
| GPU | None | RTX 3060+ (for faster LLM) |

## Related Documentation

- [Configuration Reference](Configuration-Reference.md)
- [Hardware Requirements](HARDWARE_REQUIREMENTS.md)
- [Troubleshooting Guide](TROUBLESHOOTING_GUIDE.md)
