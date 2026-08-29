# Telegram Subscription Code Bot

Relays a verification code from your Gmail inbox to paying Telegram subscribers,
with screenshot-based UPI payment approval.

## How it works

1. User sends `/start`, gets your UPI ID and instructions
2. User pays, sends a screenshot in the chat
3. Bot forwards the screenshot to you (admin) with Approve/Reject buttons
4. You approve → their subscription activates for `SUBSCRIPTION_DAYS`
5. User sends `/getcode` → bot pulls the newest matching email from your inbox and sends the code
6. `/status` shows a user their expiry

Trusted people other than you can also approve, using `/approve <id> <token>` with
the shared `ADMIN_APPROVAL_TOKEN` — no need to be logged into your admin chat.

## ⚠️ Before you deploy — things you MUST customize

### 1. The code-extraction regex (`email_fetcher.py`, `extract_code()`)
The current regex just grabs the first 4–8 digit number in the email body. **This
will grab the wrong number** if your email contains any other numbers (an order ID,
a phone number, a date, etc.) before the actual code. Paste me a real (redacted)
sample of your site's code email and I'll tighten this to match your exact format —
e.g. requiring it to appear right after the word "code" or "OTP".

### 2. `CODE_SENDER_FILTER` in `.env`
Set this to the exact sending address your site uses (e.g. `noreply@yoursite.com`).
This is what stops the bot from ever treating an unrelated email in your inbox as
a "code" to relay to a user — don't leave it blank.

### 3. Generate a real `ADMIN_APPROVAL_TOKEN`
Don't make one up by hand. Run:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```
Paste the output into `.env`. Treat it like a password — anyone with it can approve
payments and grant access.

## Setup

### 1. Create your bot
Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow the
prompts → copy the token it gives you.

### 2. Get your Telegram numeric ID
Message [@userinfobot](https://t.me/userinfobot) → it replies with your ID.

### 3. Create a Gmail App Password
Requires 2FA enabled on your Google account first.
Go to https://myaccount.google.com/apppasswords → generate one for "Mail" →
copy the 16-character password. **Do not use your real Gmail password** — the
app password is scoped and revocable without changing your main login.

### 4. Configure
```bash
cp .env.example .env
nano .env   # fill in BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_APPROVAL_TOKEN,
            # EMAIL_ADDRESS, EMAIL_APP_PASSWORD, CODE_SENDER_FILTER, UPI_ID
```

### 5. Install & run
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 bot.py
```

## Deploying on your VPS (keep it running 24/7)

Use `systemd` so it restarts automatically on crash or reboot:

```ini
# /etc/systemd/system/telegram-code-bot.service
[Unit]
Description=Telegram Code Subscription Bot
After=network.target

[Service]
Type=simple
User=YOUR_VPS_USER
WorkingDirectory=/path/to/telegram_bot
ExecStart=/path/to/telegram_bot/venv/bin/python3 bot.py
Restart=always
RestartSec=5
EnvironmentFile=/path/to/telegram_bot/.env

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-code-bot
sudo systemctl start telegram-code-bot
sudo systemctl status telegram-code-bot   # check it's running
journalctl -u telegram-code-bot -f        # live logs
```

## Security notes

- **`.env` contains real secrets.** Never commit it to git. Add it to `.gitignore`.
- **The Gmail App Password only grants mail access** — still, treat it as sensitive.
  If you ever suspect it's leaked, revoke it at https://myaccount.google.com/apppasswords
  and generate a new one.
- **`subscriptions.db`** contains your users' Telegram IDs and subscription status.
  Back it up periodically (`cp subscriptions.db subscriptions.db.bak`) so a VPS
  issue doesn't wipe your subscriber list.
- Since approval is manual (you looking at a screenshot), the main fraud risk is a
  **reused or doctored screenshot**. Get in the habit of checking the amount, UPI
  reference number (UTR), and timestamp against your actual bank/UPI app — not just
  that the image looks like a payment.

## Extending later

- **Payment gateway** (Razorpay/Cashfree): if you outgrow manual approval, the
  `_resolve_request()` function in `bot.py` is the single place that grants access —
  a gateway webhook would just call the same function instead of a human tapping a button.
- **Renewal reminders**: `database.get_expiring_soon()` is already built to support
  a daily cron job that DMs users before they expire — ask me to wire this in
  whenever you're ready.
