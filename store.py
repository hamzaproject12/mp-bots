"""
Historique des offres deja envoyees.
Un fichier JSON par bot, dans DATA_PATH (monter un volume Railway dessus).
"""
import os
import json

import config
from notifier import log


def _path(name):
    return os.path.join(config.DATA_PATH, f"seen_{name}.json")


def load_seen(name):
    os.makedirs(config.DATA_PATH, exist_ok=True)
    try:
        with open(_path(name), "r") as f:
            data = json.load(f)
        log(f"🧾 [{name}] historique : {len(data)} offres deja vues")
        return list(data)
    except Exception:
        log(f"🧾 [{name}] aucun historique (1er lancement ou volume absent)")
        return []


def save_seen(name, seen_list):
    """On garde une LISTE pour conserver l'ordre : la troncature supprime
    les plus ANCIENNES, pas des entrees au hasard."""
    os.makedirs(config.DATA_PATH, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen_list[-config.MAX_SEEN:], f)
    os.replace(tmp, _path(name))   # ecriture atomique : pas de fichier corrompu


def volume_absent():
    """True si DATA_PATH n'est probablement pas un volume persistant."""
    return not os.path.ismount(config.DATA_PATH)
