from pydantic import BaseModel


class BalanceFetchRequest(BaseModel):
    wallet_ids: list[int]  # empty list = fetch all wallets


class TransactionFetchRequest(BaseModel):
    wallet_id: int


class Transaction(BaseModel):
    tx_id: str
    type: str    # deposit / withdraw / trade / fill / transfer
    asset: str
    amount: str
    timestamp: str   # ISO 8601
    status: str      # completed / pending / failed
    address: str | None = None   # counterparty on-chain address (from/to)
    network: str | None = None   # blockchain network (ETH, BSC, TRC20, etc.)


class TransactionResponse(BaseModel):
    wallet_id: int
    wallet_name: str
    wallet_type: str
    type_value: str
    transactions: list[Transaction]
    error: str | None = None


class PnL(BaseModel):
    prev: str        # previous stable total as string
    abs: str         # absolute change, e.g. "+234.50" or "-12.30"
    pct: str         # percent change, e.g. "+2.30" or "-0.80"
    direction: str   # "up" | "down" | "flat"


class WalletBalanceResult(BaseModel):
    wallet_id: int
    wallet_name: str
    wallet_type: str
    type_value: str
    totals: dict[str, str]
    usd_values: dict[str, str]   # symbol → USD value string, only for priced tokens
    usd_total: str               # sum of all USD values for this wallet
    details: dict
    error: str | None = None
    pnl: PnL | None = None


class AggregatedBalance(BaseModel):
    stable: dict[str, str]
    other: dict[str, str]
    stable_total: str
    usd_total: str               # grand total across all wallets (stable + priced non-stable)
    pnl: PnL | None = None


class BalanceResponse(BaseModel):
    results: list[WalletBalanceResult]
    aggregated: AggregatedBalance
