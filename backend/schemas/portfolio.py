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


class WalletBalanceResult(BaseModel):
    wallet_id: int
    wallet_name: str
    wallet_type: str
    type_value: str
    totals: dict[str, str]
    details: dict
    error: str | None = None


class AggregatedBalance(BaseModel):
    stable: dict[str, str]
    other: dict[str, str]
    stable_total: str


class BalanceResponse(BaseModel):
    results: list[WalletBalanceResult]
    aggregated: AggregatedBalance
