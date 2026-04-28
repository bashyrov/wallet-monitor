from backend.providers.exchanges.backpack_provider import BackpackProvider
from backend.providers.exchanges.binance_provider import BinanceProvider
from backend.providers.exchanges.bitget_provider import BitgetProvider
from backend.providers.exchanges.bingx_provider import BingXProvider
from backend.providers.exchanges.bybit_provider import BybitProvider
from backend.providers.exchanges.gate_provider import GateProvider
from backend.providers.exchanges.htx_provider import HTXProvider
from backend.providers.exchanges.kraken_provider import KrakenProvider
from backend.providers.exchanges.kucoin_provider import KucoinProvider
from backend.providers.exchanges.mexc_provider import MexcProvider
from backend.providers.exchanges.okx_provider import OKXProvider
from backend.providers.exchanges.whitebit_provider import WhiteBITProvider

EXCHANGE_PROVIDERS = {
    "binance": BinanceProvider,
    "okx": OKXProvider,
    "bybit": BybitProvider,
    "gate": GateProvider,
    "mexc": MexcProvider,
    "kucoin": KucoinProvider,
    "bitget": BitgetProvider,
    "backpack": BackpackProvider,
    "kraken": KrakenProvider,
    "whitebit": WhiteBITProvider,
    "bingx": BingXProvider,
    "htx": HTXProvider,
}