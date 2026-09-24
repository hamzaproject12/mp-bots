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


def send_whatsapp(to, params):
    """Envoie le template WhatsApp. params = liste dans l'ordre {{1}}..{{6}}"""
    if not (config.WA_TOKEN and config.WA_PHONE_ID and to):
        return False

    url = (f"https://graph.facebook.com/{config.WA_API_VERSION}"
           f"/{config.WA_PHONE_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "to": str(to),
        "type": "template",
        "template": {
            "name": config.WA_TEMPLATE,
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
def notify(subscriber, telegram_msg, wa_params):
    """Envoie la meme alerte sur tous les canaux configures pour l'abonne."""
    if subscriber.get("telegram"):
        send_telegram(subscriber["telegram"], telegram_msg)
    if subscriber.get("whatsapp"):
        send_whatsapp(subscriber["whatsapp"], wa_params)


def broadcast(alerts):
    """alerts = liste de dicts {msg, wa_params, recipients, sort_key}.
    Les meilleures offres sont envoyees en DERNIER pour rester en haut
    de la conversation."""
    import time
    alerts.sort(key=lambda a: a.get("sort_key", 0))
    for item in alerts:
        for sub in item["recipients"]:
            notify(sub, item["msg"], item["wa_params"])
            time.sleep(0.4)
    return len(alerts)
