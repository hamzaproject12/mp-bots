"""
Envoi des alertes : Telegram + WhatsApp Cloud API.
Partage par les deux bots.
"""
import re
import requests
from datetime import datetime

import config


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# =========================================================
#                      TELEGRAM
# =========================================================
def send_telegram(chat_id, message):
    if not config.TELEGRAM_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=20)
        if r.status_code >= 400:
            log(f"❌ Telegram {chat_id}: {r.text[:150]}")
            return False
        return True
    except Exception as e:
        log(f"❌ Erreur Telegram vers {chat_id}: {e}")
        return False


# =========================================================
#                      WHATSAPP
# =========================================================
WA_HINTS = {
    132001: "template introuvable (nom/langue incorrects ou pas encore approuve)",
    132000: "nombre de parametres different du template",
    131030: "numero absent de la liste des destinataires de test",
    131047: "hors fenetre 24h (il faut un template, pas du texte libre)",
    131026: "le numero destinataire n'a pas de compte WhatsApp",
    190: "token invalide ou expire",
    200: "permission manquante sur le token",
    368: "numero temporairement bloque par Meta (qualite)",
}


def wa_clean(text, max_len=280):
    """Les parametres de template WhatsApp interdisent sauts de ligne,
    tabulations et espaces multiples."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:max_len] if t else "-"


def send_whatsapp(to, params, template=None):
    """Envoie un template WhatsApp.
    template=None  -> config.WA_TEMPLATE (comportement par defaut)
    template="..." -> template dedie a un client"""
    if not (config.WA_TOKEN and config.WA_PHONE_ID and to):
        return False

    url = (f"https://graph.facebook.com/{config.WA_API_VERSION}"
           f"/{config.WA_PHONE_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "template",
        "template": {
            "name": template or config.WA_TEMPLATE,
            "language": {"code": config.WA_LANG},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": wa_clean(p)} for p in params],
            }],
        },
    }
    try:
        r = requests.post(url, json=payload, headers={
            "Authorization": f"Bearer {config.WA_TOKEN}",
            "Content-Type": "application/json",
        }, timeout=25)
        if r.status_code >= 400:
            code = ""
            try:
                code = r.json().get("error", {}).get("code", "")
            except Exception:
                pass
            log(f"❌ WhatsApp {to}: {code} {WA_HINTS.get(code, r.text[:200])}")
            return False
        return True
    except Exception as e:
        log(f"❌ Erreur WhatsApp vers {to}: {e}")
        return False


# =========================================================
#                      DISPATCH
# =========================================================
def notify(subscriber, telegram_msg, wa_params, wa_template=None):
    """Envoie la meme alerte sur tous les canaux configures pour l'abonne."""
    ok = False
    if subscriber.get("telegram"):
        ok = send_telegram(subscriber["telegram"], telegram_msg) or ok
    if subscriber.get("whatsapp"):
        ok = send_whatsapp(subscriber["whatsapp"], wa_params, wa_template) or ok
    return ok


def broadcast(alerts):
    """alerts = [{id, msg, wa_params, recipients, sort_key, wa_template?}]

    CHAQUE destinataire a son PROPRE historique : une offre n'est envoyee
    qu'aux clients qui ne l'ont pas deja recue. Ajouter un client n'envoie
    donc rien aux autres, et supprimer le fichier d'un client lui renvoie
    tout sans deranger personne.

    L'offre est marquee AVANT l'envoi : en cas de coupure on prefere rater
    une alerte plutot que d'en envoyer cent en double.
    """
    import time
    import store

    alerts.sort(key=lambda a: a.get("sort_key", 0))
    envois = 0
    touchees = 0

    for item in alerts:
        offer_id = item.get("id")
        destinataires = [s for s in item["recipients"]
                         if offer_id is None or not store.client_a_vu(s.get("name"), offer_id)]
        if not destinataires:
            continue
        touchees += 1
        for sub in destinataires:
            if offer_id is not None:
                store.client_marquer(sub.get("name"), offer_id)
            notify(sub, item["msg"], item["wa_params"], item.get("wa_template"))
            envois += 1
            time.sleep(0.4)

    store.enregistrer_clients()
    if envois:
        log(f"📨 {touchees} offres -> {envois} envois")
    return touchees
