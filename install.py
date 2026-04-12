"""Project installer and environment validator for LLM Council.

Usage:
  python install.py
  python install.py --provider duckduckgo --skip-model-setup
  python install.py --provider brave --brave-api-key <KEY>
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True, capture_output=True)


def print_step(msg: str) -> None:
    print(f"\n[setup] {msg}")


def check_python() -> None:
    print_step(f"Python: {sys.version.split()[0]}")


def install_requirements() -> None:
    print_step("Installing Python requirements")
    res = run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    if res.returncode != 0:
        print(res.stdout)
        print(res.stderr)
        raise RuntimeError("Failed to install requirements")
    print("[ok] requirements installed")


def check_ollama() -> bool:
    print_step("Checking Ollama")
    if shutil.which("ollama") is None:
        print("[warn] Ollama is not installed or not in PATH")
        return False
    res = run(["ollama", "list"])
    if res.returncode != 0:
        print("[warn] Ollama command exists but service may not be running")
        print(res.stderr.strip())
        return False
    print("[ok] Ollama is available")
    return True


def check_playwright() -> None:
    print_step("Checking Playwright")
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[warn] Playwright python package not available")
        return

    res = run([sys.executable, "-m", "playwright", "install", "chromium"])
    if res.returncode == 0:
        print("[ok] Playwright Chromium ready")
    else:
        print("[warn] Could not auto-install Playwright Chromium")
        print(res.stderr.strip())


def check_mem0() -> None:
    print_step("Checking Mem0 local setup")

    try:
        import mem0  # noqa: F401
        print("[ok] mem0 package import successful")
    except Exception:
        print("[warn] mem0 package not importable (ensure requirements are installed)")
        return

    cfg = {}
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    mem = cfg.get("memory", {})
    enabled = bool(mem.get("enabled", False))
    provider = str(mem.get("provider", ""))
    llm_model = str(mem.get("llm_model", "")).strip()
    embedder_model = str(mem.get("embedder_model", "")).strip()
    ollama_base = str(mem.get("ollama_base_url", "http://localhost:11434")).strip()

    print(f"[info] memory.enabled={enabled} provider={provider or 'n/a'}")
    if enabled and provider != "mem0-oss-local":
        print("[warn] memory.provider should be 'mem0-oss-local' for local setup")

    missing = []
    if enabled and not llm_model:
        missing.append("memory.llm_model")
    if enabled and not embedder_model:
        missing.append("memory.embedder_model")
    if missing:
        print(f"[warn] Missing Mem0 config fields: {', '.join(missing)}")

    try:
        resp = requests.get(f"{ollama_base.rstrip('/')}/api/tags", timeout=8)
        if resp.status_code == 200:
            print(f"[ok] Ollama reachable for Mem0 at {ollama_base}")
        else:
            print(f"[warn] Ollama check for Mem0 returned {resp.status_code}")
    except Exception as e:
        print(f"[warn] Ollama not reachable for Mem0 at {ollama_base}: {e}")


def check_search_provider(provider: str, brave_api_key: str) -> None:
    provider = provider.lower()
    print_step(f"Checking search provider: {provider}")

    if provider == "searxng":
        for url in ("http://localhost:8080/search", "http://localhost:8001/search"):
            try:
                resp = requests.get(url, params={"q": "test", "format": "json"}, timeout=8)
                if resp.status_code == 200:
                    print(f"[ok] SearXNG reachable at {url.rsplit('/search', 1)[0]}")
                    return
            except Exception:
                pass
        print("[warn] SearXNG not reachable on localhost:8080 or localhost:8001")
        return

    if provider == "brave":
        if not brave_api_key:
            print("[warn] Brave provider selected but API key is empty")
            return
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "test", "count": 1},
                headers={"Accept": "application/json", "X-Subscription-Token": brave_api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                print("[ok] Brave API key works")
            else:
                print(f"[warn] Brave API check failed ({resp.status_code})")
        except Exception as e:
            print(f"[warn] Brave API check failed: {e}")
        return

    if provider == "duckduckgo":
        try:
            resp = requests.get("https://duckduckgo.com/html/", params={"q": "test"}, timeout=10)
            if resp.status_code == 200:
                print("[ok] DuckDuckGo endpoint reachable")
            else:
                print(f"[warn] DuckDuckGo endpoint returned {resp.status_code}")
        except Exception as e:
            print(f"[warn] DuckDuckGo endpoint check failed: {e}")
        return


def model_setup() -> None:
    print_step("Creating Ollama model aliases from Modelfiles")
    model_map = [
        ("gemma4-26b", "Modelfile_gemma"),
        ("qwen3-14b", "Modelfile_qwen3"),
        ("ministral-14b", "Modelfile_ministral"),
        ("phi4-mini", "Modelfile_phi4"),
        ("qwen35-9b", "Modelfile_qwen35"),
    ]
    for alias, modelfile in model_map:
        mf = ROOT / modelfile
        if not mf.exists():
            print(f"[skip] {modelfile} not found")
            continue
        res = run(["ollama", "create", alias, "-f", str(mf)])
        if res.returncode == 0:
            print(f"[ok] created {alias}")
        else:
            print(f"[warn] failed to create {alias}: {res.stderr.strip()}")

    res = run(["ollama", "pull", "nomic-embed-text"])
    if res.returncode == 0:
        print("[ok] pulled nomic-embed-text")
    else:
        print(f"[warn] failed to pull nomic-embed-text: {res.stderr.strip()}")


def update_config(provider: str, brave_api_key: str) -> None:
    print_step("Updating config.yaml search settings")
    cfg = {}
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    cfg.setdefault("search", {})
    cfg["search"]["provider"] = provider
    cfg["search"]["brave_api_key"] = brave_api_key or ""

    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    print("[ok] config.yaml updated")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install and validate LLM Council environment")
    parser.add_argument("--provider", choices=["searxng", "brave", "duckduckgo"], default="searxng")
    parser.add_argument("--brave-api-key", default="")
    parser.add_argument("--skip-model-setup", action="store_true")
    parser.add_argument("--skip-requirements", action="store_true")
    args = parser.parse_args()

    check_python()
    if not args.skip_requirements:
        install_requirements()

    ollama_ok = check_ollama()
    check_playwright()
    check_mem0()
    check_search_provider(args.provider, args.brave_api_key)
    update_config(args.provider, args.brave_api_key)

    if not args.skip_model_setup and ollama_ok:
        model_setup()

    print_step("Setup complete")
    print("Run: python gui.py")


if __name__ == "__main__":
    main()
