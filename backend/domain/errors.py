class DomainError(Exception):
    """Base class for all domain errors."""


class WalletNotFound(DomainError):
    def __init__(self, wallet_id: int):
        super().__init__(f"Wallet {wallet_id} not found")
        self.wallet_id = wallet_id


class TagNotFound(DomainError):
    def __init__(self, tag_id: int):
        super().__init__(f"Tag {tag_id} not found")
        self.tag_id = tag_id


class TagAlreadyExists(DomainError):
    def __init__(self, name: str):
        super().__init__(f"Tag '{name}' already exists")
        self.name = name


class InvalidProviderType(DomainError):
    def __init__(self, wallet_type: str):
        super().__init__(f"Invalid wallet type: {wallet_type!r}")
        self.wallet_type = wallet_type


class InvalidCredentials(DomainError):
    """Exchange API credentials are wrong or missing."""


class InvalidAddress(DomainError):
    """Blockchain address is invalid for the selected network."""
    def __init__(self, address: str, chain: str):
        super().__init__(f"Address {address!r} is not valid for chain {chain!r}")
        self.address = address
        self.chain = chain


class WalletLimitReached(DomainError):
    def __init__(self, limit: int):
        super().__init__(f"Free plan limit of {limit} wallets reached")
        self.limit = limit


class ProviderUnavailable(DomainError):
    """Provider is temporarily unavailable (rate-limit, network error, etc.)."""
    def __init__(self, provider: str, reason: str):
        super().__init__(f"Provider {provider!r} unavailable: {reason}")
        self.provider = provider
        self.reason = reason
