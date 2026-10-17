#!/usr/bin/env python3
"""Preload models through the real authenticated app, without logging secrets."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(url):
    from babybot.config import Settings
    settings = Settings.from_env()
    credentials = {"family_code": settings.family_code, "parent_pin": settings.parent_pin}
    async with httpx.AsyncClient(base_url=url, headers={"Origin": url.rstrip("/")}, trust_env=False, timeout=150) as client:
        for path, data in [("/api/auth/login", {"code": credentials["family_code"]}), ("/api/auth/parent", {"pin": credentials["parent_pin"]}), ("/api/admin/warmup", {})]:
            result = await client.post(path, json=data)
            result.raise_for_status()
        print(json.dumps({"warmup": result.json().get("ok", False)}, ensure_ascii=False))
        await client.post("/api/auth/logout")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    asyncio.run(run(parser.parse_args().url))
