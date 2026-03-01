# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python trading bot for Polymarket's 5-minute Up/Down binary crypto markets (BTC, ETH, SOL, XRP). Uses the Builder Program for gasless transactions via EIP-712 signing. Includes a full research pipeline for strategy development with statistical rigor.

## Project Structure

```
dual_entry/
├── src/                  # Core library (client, signer, config, websocket)
├── lib/                  # Shared strategy libraries (market_manager, console)
├── strategies/           # Live trading strategies (run in production/dry-run)
├── research/             # Research analysis scripts and findings
│   └── FINDINGS.md       # Comprehensive research results
├── scripts/              # Utility scripts (data collector, stress tests, setup)
├── tests/                # Unit tests (pytest)
├── apps/                 # Interactive tools (TUI, WS streams)
├── examples/             # Beginner examples
├── docs/                 # Polymarket API documentation
└── data/                 # Price data CSVs (gitignored, collected 24/7)
```

## Common Commands

```bash
# Setup (first time)
pip install -r requirements.txt
cp .env.example .env  # Edit with your credentials
source .env

# Run quickstart example
python examples/quickstart.py

# Run full integration test
python scripts/full_test.py

# Run the bot
python scripts/run_bot.py              # Quick demo
python scripts/run_bot.py --interactive # Interactive mode

# Run a live strategy
python strategies/stat_arb.py --spread 0.15 --target 0.15 --timeout 20 --size 10
python strategies/stat_arb.py --dry-run --name "test_config"

# Run research analysis
python research/autocorrelation.py     # Outcome sequence analysis
python research/research_v3.py         # Hypothesis-driven strategy tests

# Data collection (runs 24/7 on server)
python scripts/data_collector.py

# Testing
pytest tests/ -v                        # Run all tests
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         TradingBot                          │
│                        (bot.py)                             │
│  - High-level trading interface                             │
│  - Async order operations                                   │
└─────────────────────┬───────────────────────────────────────┘
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
┌─────────────┐ ┌───────────┐ ┌───────────────┐
│ OrderSigner │ │ ClobClient│ │ RelayerClient │
│ (signer.py) │ │(client.py)│ │ (client.py)   │
│             │ │           │ │               │
│ EIP-712     │ │ Order     │ │ Gasless       │
│ signatures  │ │ submission│ │ transactions  │
└──────┬──────┘ └─────┬─────┘ └───────────────┘
       │              │
       ▼              ▼
┌─────────────┐ ┌───────────┐
│ KeyManager  │ │  Config   │
│ (crypto.py) │ │(config.py)│
│             │ │           │
│ PBKDF2 +    │ │ YAML/ENV  │
│ Fernet      │ │ loading   │
└─────────────┘ └───────────┘
```

### Module Responsibilities

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `bot.py` | Main trading interface | `TradingBot`, `OrderResult` |
| `client.py` | API communication | `ClobClient`, `RelayerClient` |
| `signer.py` | EIP-712 signing | `OrderSigner`, `Order` |
| `crypto.py` | Key encryption | `KeyManager` |
| `config.py` | Configuration | `Config`, `BuilderConfig` |
| `utils.py` | Helper functions | `create_bot_from_env`, `validate_address` |

### Data Flow

1. `TradingBot.place_order()` creates an `Order` dataclass
2. `OrderSigner.sign_order()` produces EIP-712 signature
3. `ClobClient.post_order()` submits to CLOB with Builder HMAC auth headers
4. If gasless enabled, `RelayerClient` handles Safe deployment/approvals

## Key Patterns

- **Async methods**: All trading operations (`place_order`, `cancel_order`, `get_trades`) are async
- **Config precedence**: Environment vars > YAML file > defaults
- **Builder HMAC auth**: Timestamp + method + path + body signed with api_secret
- **Signature type 2**: Gnosis Safe signatures for Polymarket

## Configuration

Config loads from `config.yaml` or environment variables:

```python
# From environment
config = Config.from_env()

# From YAML
config = Config.load("config.yaml")

# With env overrides
config = Config.load_with_env("config.yaml")
```

Key fields:
- `safe_address`: Your Polymarket proxy wallet address
- `builder.api_key/api_secret/api_passphrase`: For gasless trading
- `clob.chain_id`: 137 (Polygon mainnet)

## Testing Notes

- Tests use `pytest` with `pytest-asyncio` for async
- Mock external API calls; never hit real Polymarket APIs in tests
- Test private key: `"0x" + "a" * 64`
- Test safe address: `"0x" + "b" * 40`
- YAML config values starting with `0x` must be quoted to avoid integer parsing

## Dependencies

- `eth-account>=0.13.0`: Uses new `encode_typed_data` API
- `web3>=6.0.0`: Polygon RPC interactions
- `cryptography`: Fernet encryption for private keys
- `pyyaml`: YAML config file support
- `python-dotenv`: .env file loading

## Polymarket API Context

- CLOB API: `https://clob.polymarket.com` - order submission/cancellation
- Relayer API: `https://relayer-v2.polymarket.com` - gasless transactions
- Token IDs are ERC-1155 identifiers for market outcomes
- Prices are 0-1 (probability percentages)
- USDC has 6 decimal places

**Important**: The `docs/` directory contains official Polymarket documentation. When implementing or debugging API features, always reference:
- `docs/developers/CLOB/` - CLOB API endpoints, authentication, orders
- `docs/developers/builders/` - Builder Program, Relayer, gasless transactions
- `docs/api-reference/` - REST API endpoint specifications

## For Beginners

Start with these files in order:
1. `examples/quickstart.py` - Simplest possible example
2. `examples/basic_trading.py` - Common operations
3. `src/bot.py` - Read the TradingBot class
4. `examples/strategy_example.py` - Custom strategy framework
