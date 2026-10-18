"""
Canal "site" : POST d'un appel d'offres vers le site externe FastAPI.

Ne leve JAMAIS d'exception : un site en panne doit se traduire par un canal
en echec (retente au passage suivant par store.noter_livraison), pas par un
scan interrompu.
"""
import requests

import config_site
from notifier import log


def envoyer(payload, clients):
    """POST {"appels_ouverts": [payload | {"clients": [...]}]}.
    Retourne True sur 2xx, False sinon."""
    if not config_site.actif():
        return False
    if not payload:
        log("❌ Site : aucun payload a envoyer")
        return False

    corps = {"appels_ouverts": [dict(payload, clients=list(clients or []))]}
    try:
        r = requests.post(
            config_site.SITE_URL,
            json=corps,
            headers={
                "Authorization": f"Bearer {config_site.SITE_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=config_site.SITE_TIMEOUT,
        )
        if r.status_code >= 300 or r.status_code < 200:
            log(f"❌ Site {r.status_code}: {str(r.text)[:200]}")
            return False
        return True
    except Exception as e:
        log(f"❌ Erreur site : {str(e)[:200]}")
        return False
