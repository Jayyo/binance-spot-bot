# API keys

Backtests and paper trading need no key. `--testnet` and `--live` do, and they read
different variables, so a testnet run can never pick up a live key by mistake:

| Mode | API key | Signing (pick one) |
|---|---|---|
| `--testnet` | `BINANCE_TESTNET_API_KEY` | `BINANCE_TESTNET_PRIVATE_KEY_PATH` (Ed25519/RSA PEM) or `BINANCE_TESTNET_API_SECRET` (HMAC) |
| `--live` | `BINANCE_API_KEY` | `BINANCE_PRIVATE_KEY_PATH` (Ed25519/RSA PEM) or `BINANCE_API_SECRET` (HMAC) |

Values come from the environment first, then from `./.env` in the directory you run
`spotbot` from. Setting both a secret and a key path is refused, as is a missing one, and
so is a key with characters Binance keys never contain (a stray quote or space from
copy-paste); the value is not echoed back.
An encrypted PEM file can be unlocked with `BINANCE_PRIVATE_KEY_PASSWORD` (or the
`BINANCE_TESTNET_` variant).

## What happens to the key

- Requests are signed locally. The HMAC secret or the private key is used only for the
  signature and never leaves the process; it is not written to the state directory,
  logs or reports.
- The API key (an identifier) goes in the `X-MBX-APIKEY` header to exactly one origin,
  `https://testnet.binance.vision` or `https://api.binance.com`. Public market-data
  requests never carry it, redirects are refused instead of followed, and proxy
  variables and `~/.netrc` are ignored.
- Output shows a 12-character SHA-256 fingerprint of the key, never the key. A logging
  filter blanks the key and secret if they ever end up in a log line anyway.
- `spotbot` warns when `.env` or the PEM file is readable by other users.

## Spot Testnet (fake funds)

1. Open <https://testnet.binance.vision> and log in with GitHub.
2. Generate an HMAC key, or register an Ed25519 public key made as shown below.
3. Put it in `.env` and lock the file down:

   ```
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```

   ```bash
   chmod 600 .env
   spotbot check-key --testnet
   ```

The testnet is wiped periodically (about once a month). After a reset the bot's ledger
no longer matches the account: it notices when trade ids come back for other orders and
pauses entries. Start again with a fresh `--state-dir`.

## Live (real funds)

Binance recommends Ed25519 keys and marks HMAC keys as deprecated. With an Ed25519 key
the private half is created on your machine and Binance only ever sees the public half.

1. Create the key pair on the machine that will run the bot:

   ```bash
   openssl genpkey -algorithm ed25519 -out spotbot-ed25519.pem
   openssl pkey -in spotbot-ed25519.pem -pubout -out spotbot-ed25519.pub.pem
   chmod 600 spotbot-ed25519.pem
   ```

2. In Binance, API Management, create a **self-generated** key and paste the contents of
   `spotbot-ed25519.pub.pem`.
3. Edit its restrictions:
   - allow reading and **Spot & Margin Trading**;
   - leave **withdrawals off**, and every other permission off as well (margin loans and
     transfers, futures, options, universal and internal transfers);
   - choose **Restrict access to trusted IPs only** and enter the public IP address of the
     machine running the bot.
4. Add it to `.env` (`chmod 600 .env`):

   ```
   BINANCE_API_KEY=...
   BINANCE_PRIVATE_KEY_PATH=./spotbot-ed25519.pem
   ```

5. Check it without trading:

   ```bash
   spotbot check-key --live
   ```

`check-key --live` and `run --live` read the key's permissions from Binance
(`GET /sapi/v1/account/apiRestrictions`) and the account (`GET /api/v3/account`).
`run --live` refuses to start when any of these is true:

- withdrawals are enabled;
- the key is not restricted to trusted IPs;
- reading or spot trading is disabled;
- internal transfer, universal transfer, margin, futures, options or portfolio margin
  is enabled, or Binance does not report one of these permissions at all;
- the account cannot trade, or the symbol is not trading;
- `--max-position` is too close to the symbol's minimum order value.

Then it shows the account uid, symbol, budget and limits and waits for you to type
`LIVE <SYMBOL>`. There is no flag to skip that prompt, so live mode cannot start
unattended.

### A separate sub-account helps

Sell orders are capped at the quantity the bot's own recorded fills bought, and the bot
never cancels or adopts orders it did not place. Coins on one balance are interchangeable, though: if you
trade the same symbol by hand on the same account, the bot's books and the account drift
apart. When the account holds less than the ledger says, the bot pauses entries and
sells at most what is there. A sub-account used only by the bot avoids all of that.
Each ledger file (`state/<mode>-<symbol>.db`) also records the account (Binance uid) it
was first used with and refuses to run with a key from another account.

### If a key leaks

Delete it in API Management (it stops working immediately) and create a new one.
