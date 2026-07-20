"""
Merchant Agent - Client registry.
Defines which Merchant Center accounts to process.
Add new clients here.
"""

from dataclasses import dataclass


@dataclass
class MerchantClient:
    client_id: str
    merchant_id: str
    name: str
    email: str
    is_mca: bool = False


CLIENTS: list[MerchantClient] = [
    MerchantClient(
        client_id="client_piccolo_tesoro",
        merchant_id="107773243",
        name="Piccolo Tesoro",
        email="rafal.grudowski@gmail.com",
    ),
]
