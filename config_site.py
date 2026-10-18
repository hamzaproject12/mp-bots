"""
Sortie SITE : pousse les appels d'offres vers un site externe FastAPI.

Ce fichier est INDEPENDANT des autres clients. Le site est traite comme un
destinataire de plus, avec son PROPRE historique (seen_client_sitetravaux.json)
et sa propre memoire de livraison : il peut donc rattraper des offres deja
parties chez les clients WhatsApp sans que ceux-ci recoivent quoi que ce soit.

Tant que SITE_URL ou SITE_TOKEN est vide, actif() est faux et RIEN ne change
dans le comportement actuel.
"""
import os

SITE_URL = os.getenv("SITE_URL", "")          # ex https://travaux.xxx/api/v1/appels_ouverts
SITE_TOKEN = os.getenv("SITE_TOKEN", "")
SITE_TIMEOUT = int(os.getenv("SITE_TIMEOUT", "20"))

# Destinataire virtuel. Pas de telegram ni de whatsapp : seul le canal "site".
ABONNE = {"name": "SiteTravaux", "telegram": None, "whatsapp": None, "site": True}


def actif():
    """True seulement si l'URL ET le jeton sont renseignes."""
    return bool(SITE_URL and SITE_TOKEN)
