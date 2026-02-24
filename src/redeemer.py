"""
Redeemer Module - Automatic Position Redemption for Polymarket

Handles redemption of winning positions after market resolution.
Uses ProxyWalletFactory for Polymarket proxy wallets (signature_type=1).

Usage:
    from src.redeemer import Redeemer

    redeemer = Redeemer(
        private_key="0x...",
        proxy_address="0x...",
        rpc_url="https://polygon-rpc.com",
    )

    # Get redeemable positions
    positions = redeemer.get_redeemable_positions("0xUSER_ADDRESS")

    # Redeem all positions
    results = redeemer.redeem_all("0xUSER_ADDRESS")
"""

from __future__ import annotations

import os
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from eth_abi.abi import encode as eth_abi_encode
from eth_utils.address import to_checksum_address
from web3 import Web3


PROXY_WALLET_FACTORY_ADDRESS = to_checksum_address(
    "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
)
CTF_ADDRESS = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER_ADDRESS = to_checksum_address(
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
)
USDC_ADDRESS = to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

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

DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class RedeemablePosition:
    """A position that can be redeemed."""

    condition_id: str
    token_id: str
    size: float
    outcome: str
    neg_risk: bool
    title: str
    slug: str


class Redeemer:
    """
    Handles redemption of Polymarket positions.

    For proxy wallets (signature_type=1), uses ProxyWalletFactory directly.
    Requires a Polygon RPC for on-chain transactions.
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
        self.factory_contract = self.w3.eth.contract(
            address=PROXY_WALLET_FACTORY_ADDRESS,
            abi=PROXY_FACTORY_ABI,
        )

    def encode_redeem_standard(self, condition_id: str) -> bytes:
        """Encode redeemPositions for standard (non-neg-risk) markets."""
        parent_collection_id = bytes(32)
        index_sets = [1, 2]

        return eth_abi_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_ADDRESS, parent_collection_id, condition_id, index_sets],
        )

    def encode_redeem_neg_risk(
        self, condition_id: str, yes_amount: int, no_amount: int
    ) -> bytes:
        """Encode redeemPositions for neg-risk markets."""
        amounts = [yes_amount, no_amount]
        return eth_abi_encode(
            ["bytes32", "uint256[]"],
            [condition_id, amounts],
        )

    def send_transaction(
        self,
        to: str,
        data: bytes,
        value: int = 0,
    ) -> str:
        """Send a transaction through the proxy wallet."""
        proxy_txn = {
            "typeCode": 1,  # Call
            "to": to_checksum_address(to),
            "value": value,
            "data": data.hex() if isinstance(data, bytes) else data,
        }

        tx_data = self.factory_contract.functions.proxy([proxy_txn]).build_transaction(
            {
                "from": self.account.address,
                "value": value,
            }
        )

        tx_params = {
            "from": self.account.address,
            "to": PROXY_WALLET_FACTORY_ADDRESS,
            "data": tx_data["data"],
            "value": value,
            "gas": 500000,
            "gasPrice": self.w3.eth.gas_price,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": 137,
        }

        signed = self.account.sign_transaction(tx_params)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        return receipt["transactionHash"].hex()

    def get_positions(
        self, user_address: str, redeemable: bool = False
    ) -> List[Dict[str, Any]]:
        """Get positions from the Data API."""
        import requests

        params = {
            "user": to_checksum_address(user_address),
            "redeemable": str(redeemable).lower(),
            "sizeThreshold": "0.001",
        }

        response = requests.get(
            f"{DATA_API_BASE}/positions",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_redeemable_positions(self, user_address: str) -> List[RedeemablePosition]:
        """Get all positions that can be redeemed."""
        positions = self.get_positions(user_address, redeemable=True)

        redeemable = []
        for pos in positions:
            if float(pos.get("size", 0)) <= 0:
                continue

            redeemable.append(
                RedeemablePosition(
                    condition_id=pos.get("conditionId", ""),
                    token_id=pos.get("asset", ""),
                    size=float(pos.get("size", 0)),
                    outcome=pos.get("outcome", ""),
                    neg_risk=pos.get("negativeRisk", False),
                    title=pos.get("title", ""),
                    slug=pos.get("slug", ""),
                )
            )

        return redeemable

    def group_by_condition(
        self, positions: List[RedeemablePosition]
    ) -> Dict[str, List[RedeemablePosition]]:
        """Group positions by condition_id for redemption."""
        grouped = {}
        for pos in positions:
            if pos.condition_id not in grouped:
                grouped[pos.condition_id] = []
            grouped[pos.condition_id].append(pos)
        return grouped

    def redeem_position(self, condition_id: str, neg_risk: bool) -> str:
        """Redeem a position for a given condition."""
        if neg_risk:
            data = self.encode_redeem_neg_risk(condition_id, 1, 1)
            target = NEG_RISK_ADAPTER_ADDRESS
        else:
            data = self.encode_redeem_standard(condition_id)
            target = CTF_ADDRESS

        return self.send_transaction(target, data)

    def redeem_all(self, user_address: str) -> List[Dict[str, Any]]:
        """Redeem all redeemable positions for a user."""
        positions = self.get_redeemable_positions(user_address)

        if not positions:
            return []

        grouped = self.group_by_condition(positions)

        results = []
        for condition_id, cond_positions in grouped.items():
            pos = cond_positions[0]

            try:
                tx_hash = self.redeem_position(condition_id, pos.neg_risk)
                results.append(
                    {
                        "condition_id": condition_id,
                        "tx_hash": tx_hash,
                        "status": "success",
                        "neg_risk": pos.neg_risk,
                        "title": pos.title,
                    }
                )
                print(f"  Redeemed {pos.title}: {tx_hash[:20]}...")
            except Exception as e:
                results.append(
                    {
                        "condition_id": condition_id,
                        "status": "error",
                        "error": str(e),
                        "neg_risk": pos.neg_risk,
                        "title": pos.title,
                    }
                )
                print(f"  Failed to redeem {pos.title}: {e}")

        return results


def create_redeemer_from_env() -> Redeemer:
    """Create a Redeemer instance from environment variables."""
    from dotenv import load_dotenv

    load_dotenv()

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    proxy_address = os.environ.get("POLY_SAFE_ADDRESS", "")
    rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")

    if not private_key:
        raise ValueError("POLY_PRIVATE_KEY not set")
    if not proxy_address:
        raise ValueError("POLY_SAFE_ADDRESS not set")

    return Redeemer(
        private_key=private_key,
        proxy_address=proxy_address,
        rpc_url=rpc_url,
    )
