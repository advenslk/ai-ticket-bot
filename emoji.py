# ArveX Hosting AI Ticket Bot — custom emojis
EMOJIS = {
    "ticket": "<:ax_hosting:1542004286999765105>",
    "gift": "<:ax_hosting:1541995889231532214>",
    "sparkles": "<a:ax_hosting:1541995815961362512>",
    "trophy": "<:ax_hosting:1541995735661285508>",
    "winner": "<:ax_hosting:1541995615897129133>",
    "clock": "<:ax_hosting:1541995574117797960>",
    "users": "<:ax_hosting:1542004345095327764>",
    "arrow": "<:emoji_15:1542004100974125086>",
    "check": "<:ax_hosting:1542004047354134568>",
    "cross": "<a:ax_hosting:1542003949563805856>",
    "bell": "<:ax_hosting:1542003836439240794>",
    "star": "<:ax_hosting:1542003894970876025>",
    "host": "<:ax_hosting:1542003775902847068>",
    "fire": "<a:ax_hosting:1542003711910346902>",
    "loading": "<a:ax_hosting:1542003658324049950>",
    "lock": "<:ax_hosting:1542003576421744650>",
    "claim": "<:ax_hosting:1541995889231532214>",
    "close": "<:ax_hosting:1542003576421744650>",
    "rename": "✏️",
    "settings": "⚙️",
    "plus": "➕",
    "trash": "🗑️",
    "ai": "<a:ax_hosting:1541995815961362512>",
    "billing": "<:ax_hosting:1542004286999765105>",
    "game": "<:ax_hosting:1542003711910346902>",
    "vps": "<:ax_hosting:1542003775902847068>",
    "technical": "<:ax_hosting:1542004047354134568>",
    "sales": "<:ax_hosting:1542003894970876025>",
    "staff": "<:ax_hosting:1542004345095327764>",
    "general": "<:ax_hosting:1542004286999765105>",
}


def e(name: str) -> str:
    return EMOJIS.get(name, "")


def set_emoji(name: str, value: str):
    EMOJIS[name] = value
