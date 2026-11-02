"""
Sentinelle : detecte qu'un scanner est devenu aveugle.

Le portail annonce "N offres" mais le scanner n'en extrait aucune : le HTML a
change et le bot tourne sans erreur en ne trouvant rien. On le journalise et on
previent l'ADMINISTRATEUR seulement.

Une rupture n'est PAS un echec de passage : reessayer dans 15 minutes ne
servirait a rien, le portail ne redevient pas l'ancien. Les scanners rendent
donc ok=True et le rythme normal du bot est conserve.

Pour ne pas inonder l'administrateur a chaque passage, la sentinelle garde une
memoire SUR DISQUE (DATA_PATH/sentinelle.json) : pas deux messages pour le meme
scanner a moins de SENTINELLE_SILENCE_H heures d'intervalle (defaut 6).

Ce module n'utilise JAMAIS notifier.broadcast ni la liste des abonnes : un
client ne peut donc pas recevoir ce message. Le seul destinataire est
ADMIN_TELEGRAM (variable d'environnement, vide = aucun envoi).
"""
import os
import json
from datetime import datetime, timedelta

import config
import notifier
from notifier import log

FICHIER = "sentinelle.json"
# Capture de la page aveugle, une par scanner, ecrasee a chaque rupture.
CAPTURE = "capture_{}.html"


def _maintenant():
    """Isole l'horloge pour les tests."""
    return datetime.now()


def _chemin():
    return os.path.join(config.DATA_PATH, FICHIER)


def _lire_memoire():
    """{scanner: horodatage ISO du dernier message}. Vide si absent/illisible."""
    try:
        with open(_chemin(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ecrire_memoire(data):
    try:
        os.makedirs(config.DATA_PATH, exist_ok=True)
        tmp = _chemin() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _chemin())
    except Exception as e:
        log(f"   ⚠️ memoire de la sentinelle non ecrite : {e}")


def _en_silence(scanner, memoire, maintenant):
    """True si un message a deja ete envoye pour ce scanner trop recemment."""
    silence = timedelta(hours=float(getattr(config, "SENTINELLE_SILENCE_H", 6)))
    try:
        dernier = datetime.fromisoformat(memoire[scanner])
    except Exception:
        return False
    return maintenant - dernier < silence


def capturer(page, scanner):
    """Ecrit le HTML de la page aveugle dans DATA_PATH, pour diagnostic.

    Sans cette capture, comprendre un changement du portail oblige a aller
    recopier les pages a la main : c'est ce qui a coute plusieurs jours en
    octobre 2026. Ne leve JAMAIS : une capture ratee ne doit pas faire
    echouer un passage."""
    try:
        html = page.content()
    except Exception as e:
        log(f"   ⚠️ [{scanner}] capture impossible : {str(e)[:70]}")
        return None
    chemin = os.path.join(config.DATA_PATH, CAPTURE.format(scanner))
    try:
        os.makedirs(config.DATA_PATH, exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"   📸 [{scanner}] page capturee : {chemin} "
            f"({len(html)} caracteres)")
        return chemin
    except Exception as e:
        log(f"   ⚠️ [{scanner}] capture non ecrite : {str(e)[:70]}")
        return None


def verifier(scanner, annoncees, extraites, page=None):
    """True s'il y a RUPTURE : le portail annonce des offres, zero extraite.

    `annoncees` : nombre lu sur le compteur du portail (0 si illisible).
    `extraites` : cartes/lignes reellement lues, AVANT tout filtre metier.
    `page`      : si fournie, son HTML est capture pour le diagnostic."""
    if not annoncees or annoncees <= 0 or extraites > 0:
        return False

    log(f"🚨 [{scanner}] RUPTURE : {annoncees} annoncees, {extraites} extraites "
        f"— le portail a change")
    if page is not None:
        capturer(page, scanner)
    _prevenir_admin(scanner, annoncees)
    return True


def tester_alarme():
    """Envoie un message de controle a l'administrateur, hors memoire de
    silence. Sert a RUN_MODE=selftest : une alarme qui n'a jamais sonne
    n'est pas une alarme. Aucun client ne recoit ce message."""
    chat = (getattr(config, "ADMIN_TELEGRAM", "") or "").strip()
    if not chat:
        log("🧪 sentinelle : ADMIN_TELEGRAM vide, rien a tester.")
        return False
    texte = ("🧪 Test de l'alarme de rupture de scraping.\n"
             "Si tu lis ce message, l'alerte fonctionne. "
             "Aucun client ne le recoit.\n"
             f"{_maintenant().strftime('%d/%m/%Y %H:%M')}")
    ok = notifier.send_telegram(chat, texte)
    log("🧪 sentinelle : " + ("alarme OK ✅" if ok else "alarme MUETTE ❌"))
    return ok


def _prevenir_admin(scanner, annoncees):
    chat = (getattr(config, "ADMIN_TELEGRAM", "") or "").strip()
    if not chat:
        log(f"   ℹ️ [{scanner}] ADMIN_TELEGRAM vide : aucun message d'alerte envoye")
        return

    maintenant = _maintenant()
    memoire = _lire_memoire()
    if _en_silence(scanner, memoire, maintenant):
        log(f"   🤫 [{scanner}] administrateur deja prevenu a {memoire[scanner]} "
            f"(silence {config.SENTINELLE_SILENCE_H} h)")
        return

    # Texte brut, sans _ * [ ] : le mode Markdown de Telegram les interprete.
    texte = (f"🚨 Rupture de scraping : scanner {scanner}\n"
             f"{annoncees} offres annoncees par le portail, 0 extraites.\n"
             f"Le portail a probablement change, les selecteurs sont a revoir.\n"
             f"{maintenant.strftime('%d/%m/%Y %H:%M')}")
    if notifier.send_telegram(chat, texte):
        # On ne memorise que si le message est parti : un echec sera retente.
        memoire[scanner] = maintenant.isoformat(timespec="seconds")
        _ecrire_memoire(memoire)
        log(f"   📨 [{scanner}] administrateur prevenu")
    else:
        log(f"   ❌ [{scanner}] message a l'administrateur non envoye")
