# domain/                 # чистая предметная область
#     __init__.py
#     models.py             # dataclasses: WalletRecord, BalanceResult, Portfolio
#     enums.py              # ProviderType, ChainType, ExchangeType
#     errors.py             # Domain errors

from .models import ChainWallet, ExchangeWallet, WalletBasic
from .enums import ChainType, ExchangeType