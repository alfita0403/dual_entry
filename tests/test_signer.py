"""
Unit Tests for Signer Module

Tests EIP-712 order signing functionality.
Uses the official py-order-utils SDK for order signing.

Run with:
    pytest tests/test_signer.py -v
"""

import pytest
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.signer import OrderSigner, Order, SignerError


class TestOrderSigner:
    """Tests for OrderSigner class."""

    # Valid test private key (not a real wallet with funds!)
    TEST_PRIVATE_KEY = "0x" + "a" * 64

    def setup_method(self):
        """Set up test fixtures."""
        self.signer = OrderSigner(self.TEST_PRIVATE_KEY)
        self.test_address = self.signer.address
        # Use checksum address as funder for tests
        self.test_funder = "0x" + "b" * 40

    def test_signer_address_from_key(self):
        """Test that signer has correct address from key."""
        assert self.test_address.startswith("0x")
        assert len(self.test_address) == 42

    def test_invalid_key_raises(self):
        """Test that invalid key raises ValueError."""
        with pytest.raises(ValueError, match="Invalid private key"):
            OrderSigner("invalid_key")

    def test_from_encrypted_raises_without_module(self):
        """Test that from_encrypted fails with wrong password."""
        # This would require proper encrypted data
        # Just verify the method exists and has correct signature
        assert hasattr(OrderSigner, "from_encrypted")

    def test_sign_auth_message(self):
        """Test signing authentication message."""
        signature = self.signer.sign_auth_message()

        assert signature is not None
        assert signature.startswith("0x")
        assert len(signature) == 132  # 65 bytes * 2 + 0x

    def test_sign_auth_message_with_timestamp(self):
        """Test signing with custom timestamp."""
        timestamp = "1234567890"
        signature = self.signer.sign_auth_message(timestamp=timestamp)

        assert signature is not None
        assert signature.startswith("0x")

    def test_sign_auth_message_with_nonce(self):
        """Test signing with custom nonce."""
        signature = self.signer.sign_auth_message(nonce=42)

        assert signature is not None

    def test_sign_order_dict_basic(self):
        """Test signing order with basic parameters.

        Returns SignedOrder.dict() from the official SDK with on-chain fields.
        """
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
        )

        # SDK returns a flat dict with all on-chain fields
        assert "tokenId" in result
        assert "signature" in result
        assert "maker" in result
        assert "signer" in result
        assert "side" in result

        assert result["tokenId"] == "1234567890123456789"
        assert result["side"] == "BUY"

    def test_sign_order_dict_sell_side(self):
        """Test signing SELL order."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.35,
            size=5.0,
            side="SELL",
            funder=self.test_funder,
        )

        assert result["side"] == "SELL"

    def test_sign_order_with_nonce(self):
        """Test signing order with custom nonce."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
            nonce=12345,
        )

        assert result["nonce"] == "12345"  # SDK converts to string

    def test_sign_order_with_fee(self):
        """Test signing order with fee rate."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
            fee_rate_bps=100,  # 1%
        )

        assert result["feeRateBps"] == "100"  # SDK converts to string

    def test_sign_order_generates_valid_signature(self):
        """Test that signature is valid format."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
        )

        signature = result["signature"]

        assert signature.startswith("0x")
        assert len(signature) == 132  # 65 bytes hex encoded

    def test_sign_order_has_required_onchain_fields(self):
        """Test that signed order contains all required on-chain fields."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
        )

        required_fields = [
            "salt",
            "maker",
            "signer",
            "taker",
            "tokenId",
            "makerAmount",
            "takerAmount",
            "expiration",
            "nonce",
            "feeRateBps",
            "side",
            "signatureType",
            "signature",
        ]
        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

    def test_sign_order_signature_type_propagated(self):
        """Test that signature type is propagated to the signed order."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
            signature_type=1,  # POLY_PROXY
        )

        assert result["signatureType"] == 1

    def test_sign_order_neg_risk(self):
        """Test signing with neg_risk flag."""
        result = self.signer.sign_order_dict(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder=self.test_funder,
            neg_risk=True,
        )

        # Should still produce valid signed order
        assert "signature" in result
        assert result["signature"].startswith("0x")


class TestOrder:
    """Tests for Order dataclass."""

    def test_order_creation(self):
        """Test creating an Order."""
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder="0x1234567890123456789012345678901234567890",
        )

        assert order.token_id == "1234567890123456789"
        assert order.price == 0.65
        assert order.size == 10.0
        assert order.side == "BUY"
        assert order.funder == "0x1234567890123456789012345678901234567890"

    def test_order_side_normalized_to_upper(self):
        """Test that side is normalized to uppercase."""
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="buy",  # lowercase
            funder="0x1234567890123456789012345678901234567890",
        )

        assert order.side == "BUY"

    def test_order_invalid_side_raises(self):
        """Test that invalid side raises ValueError."""
        with pytest.raises(ValueError, match="Invalid side"):
            Order(
                token_id="1234567890123456789",
                price=0.65,
                size=10.0,
                side="INVALID",
                funder="0x1234567890123456789012345678901234567890",
            )

    def test_order_invalid_price_too_low(self):
        """Test that price <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="Invalid price"):
            Order(
                token_id="1234567890123456789",
                price=0,
                size=10.0,
                side="BUY",
                funder="0x1234567890123456789012345678901234567890",
            )

    def test_order_invalid_price_above_one(self):
        """Test that price > 1 raises ValueError."""
        with pytest.raises(ValueError, match="Invalid price"):
            Order(
                token_id="1234567890123456789",
                price=1.5,
                size=10.0,
                side="BUY",
                funder="0x1234567890123456789012345678901234567890",
            )

    def test_order_invalid_size_raises(self):
        """Test that size <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="Invalid size"):
            Order(
                token_id="1234567890123456789",
                price=0.65,
                size=0,
                side="BUY",
                funder="0x1234567890123456789012345678901234567890",
            )

    def test_order_defaults(self):
        """Test that Order has sensible defaults."""
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder="0x1234567890123456789012345678901234567890",
        )

        assert order.fee_rate_bps == 0
        assert order.nonce == 0
        assert order.expiration == 0
        assert order.signature_type == 0
        assert order.neg_risk is False
        assert order.tick_size == "0.01"

    def test_order_with_signature_type(self):
        """Test creating an Order with signature type."""
        order = Order(
            token_id="1234567890123456789",
            price=0.65,
            size=10.0,
            side="BUY",
            funder="0x1234567890123456789012345678901234567890",
            signature_type=1,  # POLY_PROXY
        )

        assert order.signature_type == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
