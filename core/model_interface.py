"""
Model Interface — Handles sequential loading/unloading via Ollama.
Each call loads the model, runs inference, then immediately frees VRAM.
"""

import json
import re
import time
import logging
from typing import Optional
from dataclasses import dataclass

import requests

logger = logging.getLogger("council.model")

# ─── Model Roles ─────────────────────────────────────────────────────────────

ROLE_RESEARCHER  = "researcher"    # lightweight, fast — queries + summarizes
ROLE_COUNCIL     = "council"       # brainstorm, critique, vote
ROLE_SYNTHESIZER = "synthesizer"   # large model — unifies proposals
ROLE_COMPRESSOR  = "compressor"    # lightweight — summarizes discussion logs


@dataclass
class ModelConfig:
    model_id: str          # internal name  e.g. "llama3"
    ollama_name: str       # ollama pull name  e.g. "llama3:8b-instruct-q5_K_M"
    display_name: str      # human label  e.g. "Llama-3 8B"
    role: str
    context_size: int = 8192
    temperature: float = 0.7
    personality: str = ""  # injected into system prompt for council diversity


# ─── Ollama Client ────────────────────────────────────────────────────────────

class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base = base_url.rstrip("/")

    # ── Low-level call ────────────────────────────────────────────────────────

    def generate(
        self,
        model: ModelConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        timeout: int = 300,
    ) -> str:
        """
        Calls Ollama /api/chat.
        Sets keep_alive=0 so the model is unloaded from VRAM after the response.
        """
        payload = {
            "model": model.ollama_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "options": {
                "temperature":  model.temperature,
                "num_predict":  max_tokens,
                "num_ctx":      model.context_size,
            },
            "keep_alive": 0,    # ← KEY: unload immediately after response
            "stream": False,
        }

        logger.info(f"  ↳ Calling [{model.display_name}] ({model.ollama_name})")
        t0 = time.time()

        try:
            resp = requests.post(
                f"{self.base}/api/chat",
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["message"]["content"].strip()
            elapsed = time.time() - t0
            logger.info(f"  ↳ [{model.display_name}] finished in {elapsed:.1f}s")
            return text

        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                "Cannot reach Ollama. Is it running? → `ollama serve`"
            )
        except requests.exceptions.Timeout:
            raise RuntimeError(f"[{model.display_name}] timed out after {timeout}s")
        except Exception as e:
            raise RuntimeError(f"[{model.display_name}] error: {e}")

    def list_models(self) -> list[str]:
        resp = requests.get(f"{self.base}/api/tags", timeout=10)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    def is_available(self, model: ModelConfig) -> bool:
        try:
            return model.ollama_name in self.list_models()
        except Exception:
            return False


# ─── JSON extraction helper ───────────────────────────────────────────────────

def extract_json(text: str) -> dict | list:
    """
    Robustly extracts the first JSON object or array from model output.
    Handles markdown fences and stray text.
    """
    # Try to strip markdown fences
    cleaned = re.sub(r"```(?:json)?", "", text).strip()

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find the first { ... } or [ ... ] block
    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        match = re.search(pattern, cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Could not extract JSON from model output:\n{text[:500]}")
