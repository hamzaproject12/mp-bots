"""
Bot BDC — bons de commande.
Filtre par MOTS-CLÉS dans le texte de l'annonce.
"""
import re
import time
import math
import hashlib
from functools import lru_cache
from datetime import datetime, timedelta

import config
import store
from notifier import log

NAME = "bdc"


# =========================================================
#              RECHERCHE DE MOTS-CLÉS
# =========================================================
@lru_cache(maxsize=4096)
def _pattern(mot):
    r"""\b au debut : le mot doit commencer une vraie coupure.
    \b a la fin : uniquement pour les acronymes de MOTS_EXACTS.
    Sans \b final, "agri" attrape bien "agricole" et "agriculture"."""
    m = mot.lower().strip()
    if m in config.MOTS_EXACTS:
        return re.compile(r"\b" + re.escape(m) + r"\b")
    return re.compile(r"\b" + re.escape(m))


def contient(mot, texte_lower):
    """Remplace `mot in texte` : evite les faux positifs en plein milieu d'un mot."""
    return bool(_pattern(mot).search(texte_lower))


# =========================================================
#                       SCORING
# =========================================================
def is_pepite(text_lower):
    """Zone prioritaire ou conseil agricole : l'offre passe quoi qu'il arrive."""
    return (any(contient(z, text_lower) for z in config.SPECIAL_ZONES)
            or contient("conseil agri", text_lower))


def scorer(text_lower):
    """Retourne (score, categorie, mots_trouves).
    Ne decide PAS du seuil : cherche seulement la MEILLEURE categorie."""
    for exc in config.EXCLUSIONS_BDC:
        if contient(exc, text_lower):
            return 0, f"Exclu ({exc})", []

    if contient("hébergement", text_lower):
        if not any(contient(x, text_lower) for x in
                   ["web", "site", "cloud", "serveur", "plateforme", "logiciel", "données"]):
            return 0, "Exclu (Hébergement non-IT)", []

    print_words = ["impression", "banderole", "flyer", "imprimerie"]
    training_words = ["formation", "session", "atelier", "renforcement", "sensibilisation"]
    if any(contient(p, text_lower) for p in print_words):
        if not any(contient(t, text_lower) for t in training_words):
            return 0, "Exclu (Impression seule)", []

    best_score, best_cat, best_mots = 0, "Pas de mots-clés", []
    for cat, mots in config.KEYWORDS.items():
        trouves = [m for m in mots if contient(m, text_lower)]
        if len(trouves) > best_score:
            best_score, best_cat, best_mots = len(trouves), cat, trouves

    return best_score, best_cat, best_mots


def passe_le_seuil(score, category):
    if category.startswith("Exclu") or category == "Pas de mots-clés":
        return False
    if category == "Event & Formation":
        return score >= config.SEUIL_EVENT
    return score >= config.SEUIL_DEFAUT


# =========================================================
#                        SCAN
# =========================================================
def _build_url(date_start, date_end, page_num):
    return (
        f"{config.URL_BDC}"
        f"?search_consultation_entreprise%5BdateLimiteStart%5D={date_start}"
        f"&search_consultation_entreprise%5BdateLimiteEnd%5D={date_end}"
        f"&search_consultation_entreprise%5Bcategorie%5D=3"
        f"&search_consultation_entreprise%5BpageSize%5D=50"
        f"&search_consultation_entreprise%5Bpage%5D={page_num}&page={page_num}"
    )


def run(context):
    """context = BrowserContext Playwright partage. Retourne (ok, alerts)."""
    seen_list = store.load_seen(NAME)
    seen_ids = set(seen_list)
    alerts = []

    today = datetime.now()
    date_start = today.strftime("%Y-%m-%d")
    date_end = (today + timedelta(days=60)).strftime("%Y-%m-%d")

    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,svg,css,woff,woff2,font,ico}",
               lambda route: route.abort())

    try:
        log(f"🌍 [BDC] Periode : {date_start} -> {date_end}")
        max_pages = 1
        current_page = 1

        while current_page <= max_pages:
            url = _build_url(date_start, date_end, current_page)

            loaded = False
            for attempt in range(1, 4):
                try:
                    page.goto(url, timeout=120000, wait_until="domcontentloaded")
                    loaded = True
                    break
                except Exception as e:
                    log(f"⏳ [BDC] tentative {attempt}/3 page {current_page} : {str(e)[:80]}")
                    time.sleep(10)

            if not loaded:
                log(f"❌ [BDC] page {current_page} inaccessible, scan interrompu.")
                return False, alerts

            if current_page == 1:
                try:
                    res_text = page.locator(".content__resultat").inner_text()
                    num = re.search(r"\d+", res_text)
                    if num:
                        max_pages = math.ceil(int(num.group()) / 50)
                        log(f"🧠 [BDC] {num.group()} offres ({max_pages} pages)")
                except Exception:
                    pass

            try:
                page.wait_for_selector(".entreprise__card", timeout=15000)
            except Exception:
                log(f"⚠️ [BDC] aucune carte sur la page {current_page}")
                current_page += 1
                continue

            cards = page.locator(".entreprise__card")
            for i in range(cards.count()):
                try:
                    card = cards.nth(i)
                    full_text = card.inner_text()

                    offer_id = hashlib.md5(full_text.encode("utf-8")).hexdigest()
                    if offer_id in seen_ids:
                        continue

                    t_lower = full_text.lower()

                    # La pepite est detectee AVANT le filtrage : une offre a
                    # Ouarzazate passe meme si son score est faible.
                    special = is_pepite(t_lower)
                    score, category, mots = scorer(t_lower)
                    retenue = passe_le_seuil(score, category)

                    if config.DEBUG_SCORING:
                        etat = "✅" if (retenue or special) else "❌"
                        log(f"   {etat} score={score} cat={category} pepite={special} mots={mots}")

                    if not (retenue or special):
                        continue

                    if special and not retenue:
                        category = "Pépite"

                    links = card.locator(".entreprise__middleSubCard a")
                    ref = links.nth(0).inner_text().strip()
                    objet = links.nth(1).inner_text().replace("Objet :", "").strip()

                    dates = card.locator(".entreprise__rightSubCard--top .font-bold")
                    date_limite = (f"{dates.nth(0).inner_text().strip()} à "
                                   f"{dates.nth(1).inner_text().strip()}")
                    lieu = dates.last.inner_text().strip()

                    href = links.first.get_attribute("href")
                    link = f"https://www.marchespublics.gov.ma{href}"

                    recipients = [
                        s for s in config.SUBSCRIBERS
                        if s.get("bdc")
                        and ("ALL" in s["subscriptions"] or category in s["subscriptions"])
                    ]
                    if not recipients:
                        log(f"↪️ [BDC] ignoree (aucun abonne pour '{category}') : {ref}")
                        continue

                    emoji = "🚜🌾" if contient("agri", t_lower) else "📍🏜️" if special else "🚨"
                    title = "PÉPITE DÉTECTÉE" if special else f"ALERTE {category}"

                    msg = (
                        f"{emoji} **{title}**\n━━━━━━━━━━━━\n"
                        f"🎯 Score: {score}\n"
                        f"📅 Limite: `{date_limite}`\n"
                        f"📍 Lieu: `{lieu}`\n━━━━━━━━━━━━\n"
                        f"{ref}\nObjet: {objet}\n\n"
                        f"🔗 [Voir l'offre]({link})"
                    )

                    alerts.append({
                        "sort_key": score + (100 if special else 0),
                        "msg": msg,
                        "wa_params": [f"{title} · Score {score}", ref, objet,
                                      date_limite, lieu, link],
                        "id": offer_id,
                        "recipients": recipients,
                    })
                except Exception:
                    continue

            current_page += 1

        # On memorise les offres AVANT l'envoi : si l'envoi echoue on prefere
        # rater une alerte plutot que de spammer au prochain passage.
        for a in alerts:
            seen_list.append(a["id"])
        if alerts:
            store.save_seen(NAME, seen_list)

        return True, alerts

    finally:
        page.close()
