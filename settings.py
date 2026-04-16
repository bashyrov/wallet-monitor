from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    # ── Database ──────────────────────────────────────────────────────────
    # PostgreSQL in production: postgresql://user:pass@host:5432/dbname
    # SQLite for local dev (default):
    DATABASE_URL: str = "sqlite:///./wallet_monitor.db"

    # ── Auth ──────────────────────────────────────────────────────────────
    # MUST be overridden in production via environment variables!
    SECRET_KEY: str = "change-me-in-production-use-a-long-random-string"
    ENCRYPTION_KEY: str = "change-me-in-production-use-a-long-random-string"
    ACCESS_TOKEN_EXPIRE_DAYS: int = 30

    # ── CORS ──────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins, or "*" for dev.
    # Example: https://yourdomain.com,https://www.yourdomain.com
    ALLOWED_ORIGINS: str = ""

    # ── Logging ───────────────────────────────────────────────────────────
    # DEBUG | INFO | WARNING | ERROR | CRITICAL
    LOG_LEVEL: str = "INFO"

    BINANCE_BASE_URL: str = "https://api.binance.com"
    OKX_BASE_URL: str = "https://www.okx.com"
    GATE_BASE_URL: str = "https://api.gateio.ws"
    MEXC_BASE_URL: str = "https://api.mexc.com"
    KUCOIN_BASE_URL: str = "https://api.kucoin.com"
    BYBIT_BASE_URL: str = "https://api.bybit.com"
    BITGET_BASE_URL: str = "https://api.bitget.com"
    KRAKEN_BASE_URL: str = "https://api.kraken.com"
    WHITEBIT_BASE_URL: str = "https://whitebit.com"
    BINGX_BASE_URL: str = "https://open-api.bingx.com"

    ETHEREUM_RPC: str | None = None
    BSC_RPC: str | None = None
    POLYGON_RPC: str | None = None
    ARBITRUM_RPC: str | None = None
    OPTIMISM_RPC: str | None = None
    BASE_RPC: str | None = None
    AVALANCHE_RPC: str | None = None
    FANTOM_RPC: str | None = None
    SOLANA_RPC: str | None = None
    ZKSYNC_RPC: str | None = None
    LINEA_RPC: str | None = None
    SCROLL_RPC: str | None = None
    MANTLE_RPC: str | None = None
    BLAST_RPC: str | None = None

    ANKR_KEY: str | None = None
    TATUM_KEY: str | None = None
    TRON_RPC: str | None = None
    TRON_KEY: str | None = None

    # CoinMarketCap — for top-100 symbol list (hourly price cache)
    CMC_API_KEY: str | None = None

    # Telegram alerts bot token (BotFather)
    TG_BOT_TOKEN: str | None = None
    TG_BOT_USERNAME: str = "avalant_bot"    # used for deep links + login widget

settings = Settings()