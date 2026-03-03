from enum import Enum


class ExchangeType(str, Enum):

    BINANCE = "binance"
    OKX = "okx"
    BYBIT = "bybit"
    GATE = "gate"
    MEXC = "mexc"
    KUCOIN = "kucoin"
    BITGET = "bitget"

    LIGHTER = "lighter"
    HYPERLIQUID = "hyperliquid"
    ETHEREAL = "ethereal"

class ChainType(str, Enum):
    EVM = "evm"

    ETHEREUM = "ethereum"
    BSC = "bsc"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    BASE_RPC: str
    AVALANCHE = "avalanche"
    FANTOM = "fantom"
    SOLANA = "solana"
    ZKSYNC = "zksync"
    LINEA = "linea"
    SCROLL = "scroll"
    MANTLE = "mantle"
    BLAST = "blast"

