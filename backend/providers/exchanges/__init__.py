from backend.providers.exchanges.binance_provider import BinanceProvider
from backend.providers.exchanges.bybit_provider import BybitProvider
from backend.providers.exchanges.gate_provider import GateProvider
from backend.providers.exchanges.kucoin_provider import KucoinProvider
from backend.providers.exchanges.mexc_provider import MexcProvider
from backend.providers.exchanges.okx_provider import OKXProvider

EXCHANGE_PROVIDERS = {
    "binance": BinanceProvider,
    "okx": OKXProvider,
    "bybit": BybitProvider,
    "gate": GateProvider,
    "mexc": MexcProvider,
    "kucoin":KucoinProvider,
}