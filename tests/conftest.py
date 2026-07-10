import os

# Production defaults to a Windows DPAPI-protected automatic key. Tests opt out
# unless they explicitly set LIVINGPC_DB_KEY so fixtures remain inspectable.
os.environ.setdefault("LIVINGPC_AUTO_ENCRYPTION", "0")
