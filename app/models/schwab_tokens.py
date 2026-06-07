"""
Compatibility shim for old Schwab routes.

Commercial MVP token/status model lives in:
app.models.schwab.BrokerConnection

Do not expose raw Schwab tokens in UI.
"""

from app.models.schwab import BrokerConnection as SchwabToken

__all__ = ["SchwabToken"]
