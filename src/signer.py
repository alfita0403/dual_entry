"""
Signer Module - EIP-712 Order Signing

Provides EIP-712 signature functionality for Polymarket orders
and authentication messages.

Uses the official py-order-utils SDK for order signing to ensure
correct EIP-712 domain, struct hashing, and signature format.

The sign_auth_message method handles L1 authentication (ClobAuthDomain).
The sign_order method delegates to py-order-utils for correct CTF Exchange signing.

Example:
    from src.signer import OrderSigner

    signer = OrderSigner(private_key)
    signed_order = signer.sign_order(
        token_id="123...",
        price=0.65,
        size=10,
        side="BUY",
        funder="0x...",
        signature_type=1,
        neg_risk=False,
        tick_size="0.01",
    )
"""

import time
from typing import Optional, Dict, Any
from dataclasses import dataclass
from eth_account import Account
from eth_account.messages import encode_typed_data

# Official Polymarket SDK imports
from py_clob_client.signer import Signer as ClobSigner
from py_clob_client.order_builder.builder import OrderBuilder as ClobOrderBuilder
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions


# USDC has 6 decimal places
USDC_DECIMALS = 6


@dataclass
class Order:
    """
    Represents a Polymarket order (user-facing parameters).

    Attributes:
        token_id: The ERC-1155 token ID for the market outcome
        price: Price per share (0-1, e.g., 0.65 = 65%)
        size: Number of shares
        side: Order side ('BUY' or 'SELL')
        funder: The funder's wallet address (Safe/Proxy)
        fee_rate_bps: Fee rate in basis points (usually 0)
        nonce: Unique order nonce (usually 0)
        expiration: Order expiration timestamp (0 = no expiration)
        signature_type: Signature type (0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE)
        neg_risk: Whether the market uses neg-risk exchange
        tick_size: Market tick size ("0.01", "0.001", etc.)
    """

    token_id: str
    price: float
    size: float
    side: str
    funder: str
    fee_rate_bps: int = 0
    nonce: int = 0
    expiration: int = 0
    signature_type: int = 0
    neg_risk: bool = False
    tick_size: str = "0.01"

    def __post_init__(self):
        """Validate and normalize order parameters."""
        self.side = self.side.upper()
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {self.side}")

        if not 0 < self.price <= 1:
            raise ValueError(f"Invalid price: {self.price}")

        if self.size <= 0:
            raise ValueError(f"Invalid size: {self.size}")


class SignerError(Exception):
    """Base exception for signer operations."""

    pass


class OrderSigner:
    """
    Signs Polymarket orders using EIP-712.

    Uses the official py-order-utils SDK for order signing (CTF Exchange domain).
    Uses custom EIP-712 for auth messages (ClobAuthDomain).

    Attributes:
        wallet: The Ethereum wallet instance
        address: The signer's address (EOA)
        private_key: The hex private key (with 0x prefix)
    """

    # Polymarket CLOB EIP-712 domain (for auth messages only)
    AUTH_DOMAIN = {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": 137,  # Polygon mainnet
    }

    def __init__(self, private_key: str, chain_id: int = 137):
        """
        Initialize signer with a private key.

        Args:
            private_key: Private key (with or without 0x prefix)
            chain_id: Chain ID (137 for Polygon mainnet)

        Raises:
            ValueError: If private key is invalid
        """
        if private_key.startswith("0x"):
            private_key = private_key[2:]

        try:
            self.wallet = Account.from_key(f"0x{private_key}")
        except Exception as e:
            raise ValueError(f"Invalid private key: {e}")

        self.address = self.wallet.address
        self.private_key = f"0x{private_key}"
        self.chain_id = chain_id

    @classmethod
    def from_encrypted(cls, encrypted_data: dict, password: str) -> "OrderSigner":
        """
        Create signer from encrypted private key.

        Args:
            encrypted_data: Encrypted key data
            password: Decryption password

        Returns:
            Configured OrderSigner instance

        Raises:
            InvalidPasswordError: If password is incorrect
        """
        from .crypto import KeyManager, InvalidPasswordError

        manager = KeyManager()
        private_key = manager.decrypt(encrypted_data, password)
        return cls(private_key)

    def sign_auth_message(self, timestamp: Optional[str] = None, nonce: int = 0) -> str:
        """
        Sign an authentication message for L1 authentication.

        This signature is used to create or derive API credentials.
        Uses ClobAuthDomain (separate from order signing domain).

        Args:
            timestamp: Message timestamp (defaults to current time)
            nonce: Message nonce (usually 0)

        Returns:
            Hex-encoded signature
        """
        if timestamp is None:
            timestamp = str(int(time.time()))

        # Auth message types
        auth_types = {
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ]
        }

        message_data = {
            "address": self.address,
            "timestamp": timestamp,
            "nonce": nonce,
            "message": "This message attests that I control the given wallet",
        }

        signable = encode_typed_data(
            domain_data=self.AUTH_DOMAIN,
            message_types=auth_types,
            message_data=message_data,
        )

        signed = self.wallet.sign_message(signable)
        return "0x" + signed.signature.hex()

    def sign_order(self, order: Order) -> Dict[str, Any]:
        """
        Sign a Polymarket order using the official py-order-utils SDK.

        Delegates to the py-clob-client OrderBuilder which handles:
        - Correct EIP-712 domain (CTF Exchange, with correct contract address)
        - Proper amount calculation with rounding
        - Correct struct hashing via poly_eip712_structs
        - Signing via Account._sign_hash (matching on-chain verification)

        Args:
            order: Order instance to sign

        Returns:
            SignedOrder dict ready for API submission (from SignedOrder.dict())

        Raises:
            SignerError: If signing fails
        """
        try:
            # Create the SDK's Signer wrapper (needs private_key + chain_id)
            clob_signer = ClobSigner(
                private_key=self.private_key,
                chain_id=self.chain_id,
            )

            # Create the ClobOrderBuilder with our credentials
            builder = ClobOrderBuilder(
                signer=clob_signer,
                sig_type=order.signature_type,
                funder=order.funder,
            )

            # Build OrderArgs for the SDK
            order_args = OrderArgs(
                token_id=order.token_id,
                price=order.price,
                size=order.size,
                side=order.side,
                fee_rate_bps=order.fee_rate_bps,
                nonce=order.nonce,
                expiration=order.expiration,
            )

            options = CreateOrderOptions(
                tick_size=order.tick_size,
                neg_risk=order.neg_risk,
            )

            # Use the SDK to create and sign the order
            signed_order = builder.create_order(order_args, options)

            # Return the dict format expected by the API
            return signed_order.dict()

        except Exception as e:
            raise SignerError(f"Failed to sign order: {e}")

    def sign_order_dict(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        funder: str,
        signature_type: int = 0,
        neg_risk: bool = False,
        tick_size: str = "0.01",
        fee_rate_bps: int = 0,
        nonce: int = 0,
        expiration: int = 0,
    ) -> Dict[str, Any]:
        """
        Sign an order from dictionary parameters.

        Args:
            token_id: Market token ID
            price: Price per share
            size: Number of shares
            side: 'BUY' or 'SELL'
            funder: Funder's wallet address (proxy/safe)
            signature_type: Signature type (0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE)
            neg_risk: Whether market uses neg-risk exchange
            tick_size: Market tick size
            fee_rate_bps: Fee rate in basis points
            nonce: Order nonce
            expiration: Order expiration

        Returns:
            SignedOrder dict ready for API submission
        """
        order = Order(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            funder=funder,
            fee_rate_bps=fee_rate_bps,
            nonce=nonce,
            expiration=expiration,
            signature_type=signature_type,
            neg_risk=neg_risk,
            tick_size=tick_size,
        )
        return self.sign_order(order)

    def sign_message(self, message: str) -> str:
        """
        Sign a plain text message (for API key derivation).

        Args:
            message: Plain text message to sign

        Returns:
            Hex-encoded signature
        """
        from eth_account.messages import encode_defunct

        signable = encode_defunct(text=message)
        signed = self.wallet.sign_message(signable)
        return "0x" + signed.signature.hex()


# Alias for backwards compatibility
WalletSigner = OrderSigner
