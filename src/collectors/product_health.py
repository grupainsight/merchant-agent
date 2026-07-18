"""
Merchant Agent - Product health collector.
Calls Merchant API to get product statuses and issues.

Implementation order:
1. aggregateProductStatuses.list (cheap, overview)
2. If changes detected -> products.list / reports.search for details
3. Normalize to merchant_product_status schema
"""

# TODO: Implement Monday
# from src.auth import get_credentials
# from googleapiclient.discovery import build
