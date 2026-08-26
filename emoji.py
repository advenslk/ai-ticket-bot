# ArveX Hosting AI Ticket Bot — custom emojis
EMOJIS = {
    "ticket": "🎫",
    "gift": "🎁",
    "sparkles": "✨",
    "trophy": "🏆",
    "winner": "🏆",
    "clock": "⏰",
    "users": "👥",
    "arrow": "›",
    "check": "✅",
    "cross": "❌",
    "bell": "🔔",
    "star": "⭐",
    "host": "🛡️",
    "fire": "🔥",
    "loading": "⏳",
    "lock": "🔒",
    "claim": "🎟️"
}

def e(name: str) -> str:
    return EMOJIS.get(name, "")

def set_emoji(name: str, value: str):
    EMOJIS[name] = value
