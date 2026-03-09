import asyncio
import time

from backend.domain import ExchangeWallet
from backend.domain.models import PerpDexWallet
from backend.schemas import ChainWalletSchema, ExchangeWalletSchema
from dotenv import load_dotenv
import os

from backend.schemas.wallets import PerpDexWalletSchema

load_dotenv()


async def create_exchange_wallet(wallet_data: dict):
    provider = None
    exchange_name = wallet_data.get("exchange", "unknown")

    try:
        validated = ExchangeWalletSchema.model_validate(wallet_data)
        wallet = ExchangeWallet(**validated.model_dump())

        provider_cls = wallet.provider
        provider = provider_cls()

        balances = await provider.fetch_balance(wallet=wallet)
        print(f"[{wallet.exchange}] OK:", balances)
        return balances

    except Exception as e:
        print(f"[{exchange_name}] ERROR:", repr(e))
        try:
            import httpx
            if isinstance(e, httpx.HTTPStatusError):
                print(f"[{exchange_name}] HTTP body:", e.response.text)
        except Exception:
            pass
        return e

    finally:
        if provider is not None:
            aclose = getattr(provider, "aclose", None)
            if callable(aclose):
                await aclose()


async def create_perpdex_wallet(wallet_data: dict):
    provider = None
    perp_dex_name = wallet_data.get("perp_dex", "unknown")

    try:
        validated = PerpDexWalletSchema.model_validate(wallet_data)
        wallet = PerpDexWallet(**validated.model_dump())

        provider_cls = wallet.provider
        provider = provider_cls()

        balances = await provider.fetch_balance(wallet=wallet)
        print(f"[{wallet.perp_dex}] OK:", balances)
        return balances

    except Exception as e:
        print(f"[{perp_dex_name}] ERROR:", repr(e))
        try:
            import httpx
            if isinstance(e, httpx.HTTPStatusError):
                print(f"[{perp_dex_name}] HTTP body:", e.response.text)
        except Exception:
            pass
        return e

    finally:
        if provider is not None:
            aclose = getattr(provider, "aclose", None)
            if callable(aclose):
                await aclose()


w_data = {
    "name": "My Chain Wallet",
    "address": "0x01234567890abcdef1234567890abcdef12345678",
    "user": "user1",
    "chain": "ethereum"
}

binance_data = {
    "name": "Binance Wallet",
    "exchange": "binance",
    "user": "user1",
    "api_secret": str(os.getenv("BINANCE_API_SECRET")),
    "api_key": str(os.getenv("BINANCE_API_KEY")),
}

okx_data = {
    "name": "My Exchange Wallet",
    "exchange": "okx",
    "user": "user1",
    "api_secret": str(os.getenv("OKX_API_SECRET")),
    "api_key": str(os.getenv("OKX_API_KEY")),
    "api_passphrase": str(os.getenv("OKX_API_PASSPHRASE")),
}

gate_data = {
    "name": "My Exchange Wallet",
    "exchange": "gate",
    "user": "user1",
    "api_secret": str(os.getenv("GATE_API_SECRET")),
    "api_key": str(os.getenv("GATE_API_KEY")),
}

mexc_data = {
    "name": "My Exchange Wallet",
    "exchange": "mexc",
    "user": "user1",
    "api_secret": str(os.getenv("MEXC_SECRET")),
    "api_key": str(os.getenv("MEXC_KEY")),
}

kucoin_data = {
    "name": "My Exchange Wallet",
    "exchange": "kucoin",
    "user": "user1",
    "api_secret": str(os.getenv("KUCOIN_API_SECRET")),
    "api_key": str(os.getenv("KUCOIN_API_KEY")),
    "api_passphrase": str(os.getenv("KUCOIN_API_PASSPHRASE")),

}

bybit_data = {
    "name": "My Exchange Wallet",
    "exchange": "bybit",
    "user": "user1",
    "api_secret": str(os.getenv("BYBIT_API_SECRET")),
    "api_key": str(os.getenv("BYBIT_API_KEY")),

}

bitget_data = {
    "name": "My Exchange Wallet",
    "exchange" : "bitget",
    "user": "user1",
    "api_secret": str(os.getenv("BITGET_API_SECRET")),
    "api_key": str(os.getenv("BITGET_API_KEY")),
    "api_passphrase": str(os.getenv("BITGET_API_PASSPHRASE")),
}

backpack_data = {
    "name": "My Exchange Wallet",
    "exchange" : "backpack",
    "user": "user1",
    "api_secret": str(os.getenv("BACKPACK_API_SECRET")),
    "api_key": str(os.getenv("BACKPACK_API_KEY")),
}


lighter_data = {
    "name": "My PerpDex Wallet",
    "perp_dex": "lighter",
    "user": "user1",
    "address": "0x8A3F0f3De937Cf08D6A4144e945A354954F878E9"
}

hyperliquid_data = {
    "name": "My PerpDex Wallet",
    "perp_dex": "hyperliquid",
    "user": "user1",
    "address": "0xF21884352df185669b63342740685abEdac1a6b7"
}

ethereal_data = {
    "name": "My PerpDex Wallet",
    "perp_dex": "ethereal",
    "user": "user1",
    "address": "0x8A3F0f3De937Cf08D6A4144e945A354954F878E9"
}

async def main():
    time_start = time.time()
    tasks =  [
        create_exchange_wallet(binance_data),
        create_exchange_wallet(okx_data),
        create_exchange_wallet(gate_data),
        create_exchange_wallet(mexc_data),
        create_exchange_wallet(kucoin_data),
        create_exchange_wallet(bybit_data),
        create_exchange_wallet(bitget_data),
        create_exchange_wallet(backpack_data),
        create_perpdex_wallet(lighter_data),
        create_perpdex_wallet(hyperliquid_data),
        create_perpdex_wallet(ethereal_data),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    time_end = time.time()
    print(f"Total time taken: {time_end - time_start:.2f} seconds")

asyncio.run(main())

#gate доделать