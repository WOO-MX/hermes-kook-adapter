"""KOOK API constants — message types, signal types, and limits."""

# ---------------------------------------------------------------------------
# KOOK API base
# ---------------------------------------------------------------------------

API_BASE = "https://www.kookapp.cn/api/v3"
TOKEN_PREFIX = "Bot"

# KOOK message types (channel_type field in WebSocket events)
CHANNEL_TYPE_GROUP = "GROUP"
CHANNEL_TYPE_PERSON = "PERSON"

# KOOK message content types (type field in messages)
MSG_TYPE_TEXT = 1       # Plain text
MSG_TYPE_IMAGE = 2      # Image
MSG_TYPE_VIDEO = 3      # Video
MSG_TYPE_FILE = 4       # File
MSG_TYPE_AUDIO = 8      # Audio
MSG_TYPE_KMARKDOWN = 9  # KMarkdown (supports **bold**, *italic*, ```code```, > quote)
MSG_TYPE_CARD = 10      # Card message

# WebSocket signal types
SIGNAL_EVENT = 0         # Dispatch event
SIGNAL_HELLO = 1         # Server hello (connection established)
SIGNAL_PING = 2          # Client ping (we send every HEARTBEAT_INTERVAL)
SIGNAL_PONG = 3          # Server pong response (carries latest sn)
SIGNAL_RESUME = 4        # Resume session
SIGNAL_RECONNECT = 5     # Server requests reconnect

# Limits
MAX_MESSAGE_LENGTH = 20000   # KOOK KMarkdown content limit
HEARTBEAT_INTERVAL = 30      # Seconds between heartbeat pings
API_TIMEOUT = 30             # HTTP request timeout
RECONNECT_BACKOFF_BASE = 2   # Base seconds for exponential backoff
MAX_RECONNECT_BACKOFF = 300  # Max backoff seconds
DEDUP_WINDOW_SECONDS = 5     # Deduplicate identical messages within this window
