"""
Compatibility shim for old routes.

Commercial MVP model lives in:
app.models.schwab
"""

from app.models.schwab import SchwabAccount, BrokerConnection

__all__ = ["SchwabAccount", "BrokerConnection"]
