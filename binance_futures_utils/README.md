# Binance Futures Client Utilities

This directory contains utility scripts for interacting with Binance Futures API.

## Set Up API Keys as Environment Variables

To use these scripts, you need to set up your Binance API keys as environment variables. This is more secure than hardcoding them in the scripts.

### For Mac/Linux:

Add the following lines to your `~/.bashrc`, `~/.zshrc`, `~/.bash_profile` or equivalent shell configuration file:

```bash
export BINANCE_API_KEY='your_api_key_here'
export BINANCE_API_SECRET='your_api_secret_here'
```

Then reload your configuration:

```bash
source ~/.bashrc  # or source ~/.zshrc
```

Alternatively, you can create a file in your home directory named `.binance_credentials` with the following content:

```bash
export BINANCE_API_KEY='your_api_key_here'
export BINANCE_API_SECRET='your_api_secret_here'
```

And then source it before running the scripts:

```bash
source ~/.binance_credentials
```

### Temporary Setup (for current session only):

```bash
export BINANCE_API_KEY='your_api_key_here'
export BINANCE_API_SECRET='your_api_secret_here'
```

## Available Scripts

### 1. search_orders_positions.py

Search for orders and their related positions on Binance Futures.

#### Requirements:
- Python 3.6+
- Required packages: `requests`, `tabulate`

#### Installation:
```bash
pip install requests tabulate
```

#### Usage:

Search by order ID:
```bash
python search_orders_positions.py --order-id 123456789
```

Search by symbol:
```bash
python search_orders_positions.py --symbol BTCUSDT
```

Show all positions and orders:
```bash
python search_orders_positions.py --all
```

### 2. check_positions_orders.py

Retrieves and displays current positions and open orders.

#### Usage:
```bash
python check_positions_orders.py
```

### 3. cancel_all_binance_positions.py

Closes all open positions on Binance Futures.

#### Requirements:
- Python 3.6+
- Required packages: `aiohttp`

#### Usage:
```bash
python cancel_all_binance_positions.py
```

## Obtaining Binance API Keys

1. Log in to your Binance account.
2. Navigate to "API Management" under your account settings.
3. Click "Create API" button.
4. Complete the security verification.
5. Set restrictions for your API key:
   - For these scripts, you need "Enable Reading" and "Enable Futures" permissions.
   - For `cancel_all_binance_positions.py`, you also need "Enable Spot & Margin Trading".
   - Consider restricting API access to specific IP addresses for security.
6. Save your API key and secret in a secure location.
7. Set them as environment variables as described above.

## Security Notes

- Never share your API keys with anyone.
- Set appropriate restrictions on your API keys.
- Consider using Binance's IP restriction feature to limit API access to your specific IP addresses.
- Regularly rotate your API keys for security.
- Set environment variables instead of hardcoding keys in scripts.

## Mac OSX Specific Notes

If you're using MacOS, make sure to check which shell you're using:

```bash
echo $SHELL
```

Then add the exports to the correct profile file:
- If you're using Bash: `~/.bash_profile` or `~/.bashrc`
- If you're using Zsh: `~/.zshrc`
- If you're using another shell: consult the documentation for that shell

Remember to source the file or restart your terminal session after adding the environment variables.