from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
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