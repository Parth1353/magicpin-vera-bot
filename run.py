#!/usr/bin/env python3
"""Entry point: `python run.py` or `uvicorn vera_bot.app:app --host 0.0.0.0 --port 8080`."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "vera_bot.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info"),
        workers=1,          # state is in-process; the harness must hit one worker
    )
