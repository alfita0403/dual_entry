"""
Merger Module - Merge Positions on Polymarket CTF

Merges a full set of outcome tokens (UP + DOWN) back into USDC collateral.
Instead of waiting for market resolution, you can merge immediately after
acquiring both sides to lock in profit.

mergePositions(address collateralToken, bytes32 parentCollectionId,
               bytes32 conditionId, uint256[] partition, uint256 amount)

For standard markets: call CTF contract directly.
For neg-risk markets: call NegRiskAdapter.

Usage:
    from src.merger import Merger

    merger = Merger(
        private_key="0x...",
        proxy_address="0x...",
    )

    tx_hash = merger.merge(condition_id="0x...", amount=5.0, neg_risk=False)
"""

from __future__ import annotations

import os
from typing import Optional

from eth_abi.abi import encode as eth_abi_encode
from eth_abi.packed import encode_packed
from eth_utils.address import to_checksum_address
from web3 import Web3


# Contract addresses (Polygon mainnet)
PROXY_WALLET_FACTORY_ADDRESS = to_checksum_address(
    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
)
CTF_ADDRESS = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER_ADDRESS = to_checksum_address(
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
)
USDC_ADDRESS = to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# USDC has 6 decimals
USDC_DECIMALS = 6

PROXY_FACTORY_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "typeCode", "type": "uint8"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "proxy",
        "outputs": [{"name": "returnValues", "type": "bytes[]"}],
        "payable": True,
        "stateMutability": "payable",
        "type": "function",
    }
]

# mergePositions function selector
# keccak256("mergePositions(address,bytes32,bytes32,uint256[],uint256)")[:4]
MERGE_SELECTOR = b"\x9e\x72\x12\xad"


class Merger:
    """
    Merges full sets of outcome tokens back into USDC.

    For proxy wallets (signature_type=1), routes through ProxyWalletFactory.
    """

    def __init__(
        self,
        private_key: str,
        proxy_address: str,
        rpc_url: str = "https://polygon-rpc.com",
    ):
        self.private_key = private_key
        self.proxy_address = to_checksum_address(proxy_address)
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

        if not self.w3.is_connected():
            raise ValueError(f"Cannot connect to RPC: {rpc_url}")

        self.account = self.w3.eth.account.from_key(private_key)

    def encode_merge(self, condition_id: str, amount_raw: int) -> bytes:
        """
        Encode mergePositions calldata.

        Args:
            condition_id: The market's condition ID (hex string)
            amount_raw: Amount in raw USDC units (6 decimals)

        Returns:
            Encoded calldata bytes
        """
        parent_collection_id = bytes(32)  # Always zero for Polymarket
        partition = [1, 2]  # Binary market: YES=1, NO=2

        if condition_id.startswith("0x"):
            condition_id = condition_id[2:]
        condition_id_bytes = bytes.fromhex(condition_id.zfill(64))

        inner_data = eth_abi_encode(
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [
                USDC_ADDRESS,
                parent_collection_id,
                condition_id_bytes,
                partition,
                amount_raw,
            ],
        )

        fn_selector = encode_packed(["bytes4"], [MERGE_SELECTOR])
        return fn_selector + inner_data

    def send_transaction(self, to: str, data: bytes, value: int = 0) -> str:
        """Send a transaction through the proxy wallet."""
        proxy_txn = [
            1,  # typeCode (CALL)
            to_checksum_address(to),
            value,
            data,
        ]

        calls_data = eth_abi_encode(["(uint8,address,uint256,bytes)[]"], [[proxy_txn]])

        proxy_fn_selector = encode_packed(
            ["bytes4"],
            [b"\x8d\x80\xff\x0a"],
        )

        full_data = proxy_fn_selector + calls_data

        tx_params = {
            "from": self.account.address,
            "to": PROXY_WALLET_FACTORY_ADDRESS,
            "data": full_data.hex(),
            "value": value,
            "gas": 300000,
            "gasPrice": self.w3.eth.gas_price,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": 137,
        }

        signed = self.account.sign_transaction(tx_params)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            raise RuntimeError(
                f"Merge transaction reverted: {receipt['transactionHash'].hex()}"
            )

        return receipt["transactionHash"].hex()

    def shares_to_raw(self, shares: float) -> int:
        """Convert share count to raw USDC amount (6 decimals).

        On Polymarket, 1 share = 1 USDC of collateral when merging a full set.
        So merging N shares = N * 10^6 raw units.
        """
        return int(shares * (10**USDC_DECIMALS))

    def merge(
        self,
        condition_id: str,
        shares: float,
        neg_risk: bool = False,
    ) -> str:
        """
        Merge a full set of outcome tokens into USDC.

        Args:
            condition_id: Market condition ID
            shares: Number of full sets to merge (e.g., 4.9 shares)
            neg_risk: Whether this is a neg-risk market

        Returns:
            Transaction hash
        """
        amount_raw = self.shares_to_raw(shares)
        if amount_raw <= 0:
            raise ValueError(f"Invalid merge amount: {shares} shares")

        data = self.encode_merge(condition_id, amount_raw)
        target = NEG_RISK_ADAPTER_ADDRESS if neg_risk else CTF_ADDRESS

        return self.send_transaction(target, data)


def create_merger_from_env() -> Merger:
    """Create a Merger from environment variables."""
    from dotenv import load_dotenv

    load_dotenv()

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    proxy_address = os.environ.get("POLY_SAFE_ADDRESS", "")
    rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")

    if not private_key:
        raise ValueError("POLY_PRIVATE_KEY not set")
    if not proxy_address:
        raise ValueError("POLY_SAFE_ADDRESS not set")

    return Merger(
        private_key=private_key,
        proxy_address=proxy_address,
        rpc_url=rpc_url,
    )
