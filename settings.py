from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    BINANCE_BASE_URL: str = "https://api.binance.com"
    OKX_BASE_URL: str = "https://www.okx.com"
    GATE_BASE_URL: str = "https://api.gateio.ws"
    MEXC_BASE_URL: str = "https://api.mexc.com"
    KUCOIN_BASE_URL: str = "https://api.kucoin.com"
    BYBIT_BASE_URL: str = "https://api.bybit.com"
    BITGET_BASE_URL: str = "https://api.bitget.com"

    ETHEREUM_RPC: str
    BSC_RPC: str
    POLYGON_RPC: str
    ARBITRUM_RPC: str
    OPTIMISM_RPC: str
    BASE_RPC: str
    AVALANCHE_RPC: str
    FANTOM_RPC: str
    SOLANA_RPC: str
    ZKSYNC_RPC: str
    LINEA_RPC: str
    SCROLL_RPC: str
    MANTLE_RPC: str
    BLAST_RPC: str

    ANKR_KEY: str
    TATUM_KEY: str
    TRON_RPC: str
    TRON_KEY: str

settings = Settings()