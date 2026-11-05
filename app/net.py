import re

USER_AGENT = "BurnScarPrecipMonitor/0.1 (local dashboard)"


def redact(text: str) -> str:
    """Remove Synoptic tokens from errors and log lines."""
    return re.sub(r"(token=)[^&\s\"']+", r"\1REDACTED", text or "", flags=re.IGNORECASE)
