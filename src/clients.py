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
    is_mca: bool = False  # Multi-Client Account (advanced account)


# Add clients here as they onboard
CLIENTS: list[MerchantClient] = [
    # MerchantClient(
    #     client_id="client_piccolo_tesoro",
    #     merchant_id="123456789",
    #     name="Piccolo Tesoro",
    #     email="owner@piccolotesoro.pl",
    # ),
    # MerchantClient(
    #     client_id="client_grzebiennawszy",
    #     merchant_id="987654321",
    #     name="grzebiennawszy.pl",
    #     email="owner@grzebiennawszy.pl",
    # ),
]
