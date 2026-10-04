"""
Historique des offres deja envoyees.

DEUX NIVEAUX :

1. Historique PAR CLIENT  ->  seen_client_<slug>.json
   C'est lui qui decide ce que chaque destinataire recoit.
   Supprimer le fichier d'un client lui renvoie TOUT, sans toucher aux autres.

2. Historiques GLOBAUX hérités  ->  seen_bdc.json, seen_ao.json
   Conserves en LECTURE SEULE. Ils servent a amorcer le fichier d'un client
   la premiere fois, pour qu'une mise a jour ne declenche aucun renvoi.

Tout vit dans DATA_PATH (volume Railway).
"""
import os
import json
import re
import unicodedata

import config
from notifier import log

# Fichiers globaux hérités, lus une seule fois pour l'amorçage
LEGACY = ("bdc", "ao")

# Temoin de migration. Tant qu'il n'existe pas, le 1er fichier de CHAQUE
# client est amorce avec les historiques globaux : la mise a jour vers les
# historiques par client ne declenche donc aucun renvoi.
# Une fois ecrit, un fichier client absent signifie "ce client n'a rien recu"
# -> il recevra TOUT. C'est ce qui rend la suppression d'un fichier utile.
TEMOIN = "migration_clients.json"

# Cache du passage en cours : {slug: {"ids": [...], "set": {...}, "sale": bool}}
_cache = {}


# =========================================================
#                      CHEMINS
# =========================================================
def slug(nom):
    """'Zakariya' -> 'zakariya' ; 'Moi' -> 'moi'."""
    t = unicodedata.normalize("NFD", str(nom or "inconnu"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t or "inconnu"


def _path(name):
    return os.path.join(config.DATA_PATH, f"seen_{name}.json")


def chemin_client(nom):
    return os.path.join(config.DATA_PATH, f"seen_client_{slug(nom)}.json")


# =========================================================
#             HISTORIQUES GLOBAUX (hérités)
# =========================================================
def load_seen(name):
    os.makedirs(config.DATA_PATH, exist_ok=True)
    try:
        with open(_path(name), "r") as f:
            data = json.load(f)
        log(f"🧾 [{name}] historique global : {len(data)} offres")
        return list(data)
    except Exception:
        return []


def save_seen(name, seen_list):
    os.makedirs(config.DATA_PATH, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen_list[-config.MAX_SEEN:], f)
    os.replace(tmp, _path(name))


_amorce = None


def _migration_faite():
    return os.path.exists(os.path.join(config.DATA_PATH, TEMOIN))


def _marquer_migration():
    if _migration_faite():
        return
    try:
        os.makedirs(config.DATA_PATH, exist_ok=True)
        with open(os.path.join(config.DATA_PATH, TEMOIN), "w") as f:
            json.dump({"migre": True}, f)
        log("✅ Migration vers les historiques par client terminee. "
            "Desormais, supprimer seen_client_<nom>.json renvoie TOUT a ce client.")
    except Exception as e:
        log(f"⚠️ temoin de migration non ecrit : {e}")


def _amorcage():
    """Union des historiques globaux, UNIQUEMENT pendant la migration.
    Apres, un fichier client absent = ce client recoit tout."""
    global _amorce
    if _amorce is None:
        if _migration_faite():
            _amorce = []
            return _amorce
        ids = []
        for name in LEGACY:
            try:
                with open(_path(name), "r") as f:
                    ids.extend(json.load(f))
            except Exception:
                pass
        _amorce = ids
    return _amorce


# =========================================================
#              HISTORIQUE PAR CLIENT
# =========================================================
def _charger(nom):
    s = slug(nom)
    if s in _cache:
        return _cache[s]

    os.makedirs(config.DATA_PATH, exist_ok=True)
    chemin = chemin_client(nom)
    try:
        with open(chemin, "r") as f:
            ids = list(json.load(f))
        log(f"🧾 [{nom}] historique : {len(ids)} offres deja recues")
    except Exception:
        ids = list(_amorcage())
        if ids:
            log(f"🆕 [{nom}] 1er historique cree, amorce avec {len(ids)} offres "
                f"deja diffusees (aucun renvoi). Supprimer "
                f"seen_client_{s}.json pour tout lui renvoyer.")
        else:
            log(f"🆕 [{nom}] aucun historique : il recevra TOUT ce qui est en ligne.")
        _cache[s] = {"ids": ids, "set": set(ids), "sale": True}
        return _cache[s]

    _cache[s] = {"ids": ids, "set": set(ids), "sale": False}
    return _cache[s]


def client_a_vu(nom, offer_id):
    return offer_id in _charger(nom)["set"]


def client_marquer(nom, offer_id):
    e = _charger(nom)
    if offer_id in e["set"]:
        return
    e["set"].add(offer_id)
    e["ids"].append(offer_id)
    e["sale"] = True


def tous_ont_vu(abonnes, offer_id):
    """True si TOUS les destinataires ont deja recu cette offre.
    Permet aux scanners d'eviter un travail couteux (ouverture de fiche)
    tout en laissant passer l'offre des qu'UN client ne l'a pas encore eue."""
    if not abonnes:
        return True
    return all(client_a_vu(a.get("name"), offer_id) for a in abonnes)


def enregistrer_clients():
    """Ecriture atomique des historiques modifies. A appeler en fin de passage."""
    for s, e in _cache.items():
        if not e["sale"]:
            continue
        chemin = os.path.join(config.DATA_PATH, f"seen_client_{s}.json")
        tmp = chemin + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(e["ids"][-config.MAX_SEEN:], f)
            os.replace(tmp, chemin)
            e["sale"] = False
        except Exception as ex:
            log(f"❌ ecriture historique {s} : {ex}")
    _marquer_migration()


# =========================================================
def volume_absent():
    """True si DATA_PATH n'est pas un point de montage.
    Remonte l'arborescence : /app/data/v3 est persistant si /app/data l'est."""
    p = os.path.abspath(config.DATA_PATH)
    while True:
        if os.path.ismount(p):
            return False
        parent = os.path.dirname(p)
        if parent == p:
            return True
        p = parent
