# Polymarket Dual Entry Trading Bot

A Python trading bot for Polymarket that implements the **Dual Entry** strategy on BTC 5-minute markets.

## Features

- **WebSocket tick-by-tick** - Real-time price monitoring via WebSocket (no REST polling)
- **Dual Entry Strategy** - Buy both UP and DOWN legs to hedge exposure
- **TUI Display** - Full-screen terminal UI with real-time stats
- **Gasless Trading** - Builder Program integration for gas-free orders

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure .env
cp .env.example .env
# Edit .env with your credentials

# Run the bot
python strategies/dual_entry.py --entry-price 0.47 --total-tickets 0.94 --size 5 --entry-window 30
```

## Configuration

Edit `.env` with your credentials:

```
POLY_PRIVATE_KEY=your_private_key
POLY_SAFE_ADDRESS=your_proxy_wallet_address
POLY_BUILDER_API_KEY=your_api_key
POLY_BUILDER_API_SECRET=your_api_secret
POLY_BUILDER_API_PASSPHRASE=your_passphrase
POLY_SIGNATURE_TYPE=1
```

## Strategy Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--entry-price` | Max price for LEG1 | 0.48 |
| `--total-tickets` | Max total for LEG1 + LEG2 | 0.90 |
| `--size` | Shares per order | 5 |
| `--entry-window` | LEG1 window (seconds) | 30 |

## Claiming Winnings

After markets resolve, claim your winnings at **polymarket.com**:
1. Go to your profile
2. Find positions with "You Won"
3. Click "Claim"

## Project Structure

```
.
├── strategies/
│   └── dual_entry.py     # Main trading strategy
├── src/
│   ├── bot.py            # Trading bot core
│   ├── client.py         # CLOB API client
│   ├── signer.py         # EIP-712 signing
│   ├── gamma_client.py   # Market discovery
│   └── redeemer.py       # Position redemption
├── scripts/
│   └── redeem_all.py     # Manual redeem script
└── lib/
    ├── market_manager.py # Market management
    └── console.py        # TUI utilities
```

## Requirements

- Python 3.10+
- Polygon RPC (for on-chain operations)
- Polymarket account with Builder Program (optional, for gasless trading)
