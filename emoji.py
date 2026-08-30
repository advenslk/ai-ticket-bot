# ArveX Hosting AI Ticket Bot — custom emoji registry
# Use Discord emoji syntax here, e.g. <:name:id> or <a:name:id>.
# Keep all custom emojis in this file so the bot code never needs hard-coded IDs.

EMOJIS = {
    "ticket": "🎫",
    "ai": "🤖",
    "billing": "💳",
    "game": "🎮",
    "vps": "🖥️",
    "technical": "🛠️",
    "sales": "💎",
    "staff": "👤",
    "general": "🎫",
    "gift": "🎁",
    "sparkles": "✨",
    "trophy": "🏆",
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
    "claim": "🎟️",
    "close": "🔒",
    "rename": "✏️",
    "settings": "⚙️",
    "plus": "➕",
    "trash": "🗑️",
}


def e(name: str) -> str:
    """Return a configured emoji. Unknown names return an empty string."""
    return EMOJIS.get(name, "")


def set_emoji(name: str, value: str):
    EMOJIS[name] = value
