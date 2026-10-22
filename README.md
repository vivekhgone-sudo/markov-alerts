# Markov signals on Telegram (free, no laptop needed)

GitHub runs this bot for free on its own computers every hour, all day. It reads 5-minute candles straight
from each instrument's own exchange and posts every BUY / SELL / FLIP / CONT, with the price and the stop,
to your Telegram group:

| Instrument | Prices from |
|---|---|
| ETHUSD.P | Delta Exchange India |
| BTCUSD.P | Delta Exchange India |
| XAUUSDT.P | Binance futures (if Binance refuses GitHub's servers, Shark's gold price is used and the message says so) |

## One-time setup (about 15 minutes, can be done from a phone browser)

### 1. Your Telegram bot and group
You need two things: the bot's **token** (from @BotFather, looks like `123456789:AAH...`) and the
group's **chat id** (a negative number, usually starting `-100`).
1. Add the bot to your group and make it an **admin** (so it is allowed to post).
2. If you don't have the group's chat id yet: send `/start@YourBotName` in the group, then open
   `https://api.telegram.org/bot<YOUR TOKEN>/getUpdates` in a browser and copy the number after
   `"chat":{"id":` (it starts with `-100`).

### 2. Put the bot on GitHub
1. Make a free account at **github.com**.
2. Top right **+** → **New repository** → name `markov-alerts` → choose **Public** → **Create**.
   (Public is needed for unlimited free running time. Your token stays secret — it goes in step 3, not in the code.)
3. Click **uploading an existing file** → drag in `mre_bot.py`, `mre_engine.py`, `README.md` → **Commit**.
4. Click **Add file → Create new file** → name it exactly `.github/workflows/mre.yml` →
   paste the contents of `mre.yml` → **Commit**.
5. Same again for `.github/workflows/test.yml` with the contents of `test.yml`.

### 3. Add your secrets
Repository → **Settings → Secrets and variables → Actions → New repository secret**:
- Name `TELEGRAM_TOKEN`, value = your bot token
- Name `TELEGRAM_CHAT_ID`, value = your group's chat id (with the minus sign)

### 4. Start it
**Actions** tab → enable workflows if asked → **Markov Telegram alerts** → **Run workflow**.
The group only gets a message when a signal prints. To check the connection right away, also run the workflow
**Markov test message**: it posts the latest price of all three instruments.
From then on it restarts by itself every hour.

## Good to know
- Alerts usually arrive within ~30 seconds of the candle closing. GitHub sometimes starts the hourly run
  a few minutes late; any signal missed in that gap is still sent, marked "sent X min late".
- GitHub pauses hourly jobs in a repo with no changes for 60 days. If alerts stop, open the Actions tab and
  press **Enable workflow** / **Run workflow** again.
- To change coins or timeframe, edit the `WATCH` list at the top of `mre_bot.py`.
- Signals use the same exchange prices as your TradingView charts, but TradingView loads years of history
  while the bot warms up on the last ~10 days, so very rarely the first signal after a quiet spell can differ.
