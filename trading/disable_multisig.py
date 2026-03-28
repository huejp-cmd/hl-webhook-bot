"""
Désactive le multi-sig sur le compte Hyperliquid de JP.
Appelle convertToMultiSigUser avec liste vide + threshold=0.
"""
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account
import json

PRIVATE_KEY = "0x9fcf4d1bae9622fe7aba5b4218842d1b022a29dd4488c3118e0ba412ad98d7b4"
ADDRESS = "0x01fE7894a5A41BA669Cf541f556832c8E1F164B7"

wallet = Account.from_key(PRIVATE_KEY)
exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ADDRESS)

print(f"Wallet: {wallet.address}")
print("Tentative de désactivation du multi-sig...")

try:
    result = exchange.convert_to_multi_sig_user(
        authorized_users=[],
        threshold=0
    )
    print("Résultat:", json.dumps(result, indent=2))
except Exception as e:
    print(f"Erreur: {e}")
