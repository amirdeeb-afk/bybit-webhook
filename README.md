# Bybit Webhook Server — Wyckoff ETH Strategy

שרת Python שמקבל Webhooks מ-TradingView ושולח פקודות ל-Bybit USDT Perpetual Futures.

## הגדרת Environment Variables ב-Render.com

| Variable | תיאור |
|---|---|
| `BYBIT_API_KEY` | ה-API Key שלך מ-Bybit |
| `BYBIT_API_SECRET` | ה-API Secret שלך מ-Bybit |
| `WEBHOOK_SECRET` | סיסמה סודית (לדוגמה: `wyckoff2024`) |

## Webhook URL

```
https://YOUR-APP.onrender.com/webhook?secret=wyckoff2024
```

## JSON לסיגנלים מ-TradingView

### כניסת LONG (עם SL ו-TP)
```json
{
  "ticker": "ETHUSDT",
  "action": "buy",
  "sentiment": "long",
  "quantity": 1,
  "stopLoss": "{{plot_0}}",
  "takeProfit": "{{plot_1}}"
}
```

### כניסת SHORT
```json
{
  "ticker": "ETHUSDT",
  "action": "sell",
  "sentiment": "short",
  "quantity": 1,
  "stopLoss": "{{plot_0}}",
  "takeProfit": "{{plot_1}}"
}
```

### יציאה (סגירת פוזיציה)
```json
{
  "ticker": "ETHUSDT",
  "action": "exit",
  "sentiment": "flat",
  "quantity": 1
}
```

### Limit TP (Strategy B — delay 5 שניות)
```json
{
  "ticker": "ETHUSDT",
  "action": "limit_tp",
  "sentiment": "long",
  "quantity": 1,
  "takeProfit": "{{plot_1}}"
}
```

### ביטול כל הפקודות
```json
{
  "ticker": "ETHUSDT",
  "action": "cancel"
}
```
