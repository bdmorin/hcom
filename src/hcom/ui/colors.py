"""ANSI color codes and terminal control sequences."""

# ===== Core ANSI Codes =====
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
REVERSE = "\033[7m"

# Foreground colors
FG_GREEN = "\033[32m"
FG_CYAN = "\033[36m"
FG_WHITE = "\033[37m"
FG_BLACK = "\033[30m"
FG_GRAY = "\033[38;5;245m"  # Mid-gray (was 90, inconsistent across terminals)
FG_YELLOW = "\033[33m"
FG_RED = "\033[31m"
FG_BLUE = "\033[38;5;75m"  # Sky blue (256-color, consistent across terminals)

# TUI-specific foreground
FG_ORANGE = "\033[38;5;208m"
FG_GOLD = "\033[38;5;220m"
FG_LIGHTGRAY = "\033[38;5;250m"
FG_DELIVER = "\033[38;5;156m"  # Light green for message delivery state

# Stale instance color (brownish-grey, distinct from exited)
FG_STALE = "\033[38;5;137m"  # Tan/brownish-grey

# Background colors
BG_BLUE = "\033[48;5;69m"  # Light blue (256-color, consistent across terminals)
BG_GREEN = "\033[42m"
BG_CYAN = "\033[46m"
BG_YELLOW = "\033[43m"
BG_RED = "\033[41m"
BG_GRAY = "\033[100m"

# Stale background (brownish-grey to match foreground)
BG_STALE = "\033[48;5;137m"  # Tan/brownish-grey background

# TUI-specific background
BG_ORANGE = "\033[48;5;208m"
BG_CHARCOAL = "\033[48;5;236m"
BG_GOLD = "\033[48;5;220m"  # Gold background for warnings

# Terminal control
CLEAR_SCREEN = "\033[2J"
CURSOR_HOME = "\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

# Box drawing
BOX_H = "─"
