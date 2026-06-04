"""Local dev runner — sets env vars then starts uvicorn."""
import os, sys

os.environ.update({
    "DATABASE_URL": "postgresql://tezox:tezox@localhost:5432/avalant",
    "REDIS_URL": "redis://localhost:6379/0",
    "AVALANT_COOKIE_SECURE": "0",
    "AVALANT_RUN_MIGRATIONS": "false",
    "SECRET_KEY": "lezUBLzrNkRda0fLG/9VRxQEsZYGJR6B/Z9YWz0xPyD9JgYdzlFIQxe4XJtFWHAgvhNnxAenzhaS2gTehVxmiw==",
    "ENCRYPTION_KEY": "AxxgZ7lKRdtywln8gJjEyN03UtyoyXkgsJDqe3MmtKjkeoBXffXXj+eNLSJKsMVR9G/KuaAhUmS62R6Qen+r8g==",
    "LOG_LEVEL": "WARNING",
    "TG_BOT_TOKEN": "8321823801:AAGFuPbxi8UQgV7mRqzX-8TXDVlIApgCpRY",
    "TG_AUTH_BOT_TOKEN": "8628336287:AAFOcRTTDnSgweIjQEiCqjepaV5knARKdNc",
    "TG_AUTH_BOT_USERNAME": "avalant_authbot",
})

import uvicorn
uvicorn.run("app:app", host="0.0.0.0", port=8000)
