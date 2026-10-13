#!/usr/bin/env python3
"""
Generuje nowy GOOGLE_REFRESH_TOKEN dla Merchant Agenta i sprawdza, do jakich kont Merchant Center ma dostep.

Uruchom LOKALNIE w swoim terminalu (token wypisze sie tylko tam, nie wklejaj go do czatu):

  cd ~/Sites/merchant-agent
  GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python3 scripts/get_refresh_token.py

Wartosci GOOGLE_CLIENT_ID i GOOGLE_CLIENT_SECRET sa w Railway (usluga merchant-agent -> Variables).

Wazne - kolejnosc:
1. NAJPIERW w Google Cloud Console (APIs & Services -> OAuth consent screen) zmien status z "Testing" na
   "In production". Tokeny wydane w trybie Testing wygasaja po 7 dniach, nawet jesli aplikacje opublikujesz pozniej.
2. Zaloguj sie kontem, ktore ma dostep do Merchant Center (np. Piccolo Tesoro, ID 107773243).
3. Wklej wypisany token do Railway jako GOOGLE_REFRESH_TOKEN (usluga merchant-agent).

Typ klienta OAuth: "Desktop app" dziala od razu. Dla "Web application" dodaj w kliencie redirect URI
http://localhost:8080/ (skrypt uzywa stalego portu 8080).
"""

import os
import sys

import requests
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/content"]
AUTHINFO_URL = "https://shoppingcontent.googleapis.com/content/v2.1/accounts/authinfo"


def main() -> int:
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Ustaw GOOGLE_CLIENT_ID i GOOGLE_CLIENT_SECRET (z Railway).", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:8080/"],
            }
        },
        scopes=SCOPES,
    )
    # access_type=offline + prompt=consent wymuszaja wydanie NOWEGO refresh tokenu
    creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("Google nie zwrocil refresh tokenu. Cofnij dostep aplikacji w "
              "https://myaccount.google.com/permissions i uruchom ponownie.", file=sys.stderr)
        return 1

    # Sprawdz, ze token dziala i do jakich kont Merchant Center daje dostep
    creds.refresh(Request())
    r = requests.get(AUTHINFO_URL, headers={"Authorization": f"Bearer {creds.token}"}, timeout=30)
    print("\n== Test dostepu do Merchant Center ==")
    if r.ok:
        ids = [a.get("accountId") for a in r.json().get("accountIdentifiers", [])]
        print("Konta Merchant Center dostepne dla tego tokenu:", ids or "brak")
    else:
        print(f"authinfo: HTTP {r.status_code} {r.text[:200]}")

    print("\n== GOOGLE_REFRESH_TOKEN (wklej do Railway, nie do czatu) ==")
    print(creds.refresh_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
