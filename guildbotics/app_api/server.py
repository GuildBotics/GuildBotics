from __future__ import annotations

import argparse
import os
import secrets

import uvicorn

from guildbotics.app_api.api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GuildBotics local app API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.getenv("GUILDBOTICS_APP_API_TOKEN"))
    args = parser.parse_args()

    token = args.token or secrets.token_urlsafe(32)
    print(f"GUILDBOTICS_APP_API_TOKEN={token}", flush=True)
    uvicorn.run(create_app(session_token=token), host=args.host, port=args.port)
