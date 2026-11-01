"""
Bot AO — appels d'offres (recherche avancee).
Filtre par WHITELIST D'ACHETEURS (DRA, DPA, ORMVA, ONSSA, ONCA...).

Le portail AO est une application ASP.NET : chaque changement (taille de page,
page suivante) declenche un POSTBACK, c'est-a-dire un rechargement complet.
Il faut donc attendre la NAVIGATION, pas un simple delai, sinon Playwright
perd son contexte d'execution en plein comptage.
"""
import time
import math
from datetime import datetime, timedelta

import config
import store
import portail
import sentinelle
from notifier import log
from scanner_bdc import contient   # meme comparaison de mots que le bot BDC

NAME = "ao"

# Depuis octobre 2026 le portail affiche des cartes, plus un tableau.
ROWS_SELECTOR = portail.CARTES_AO


def scorer(objet, buyer):
    """Retourne (score, raison).

    La whitelist d'acheteurs reste le FILTRE PRINCIPAL : une offre d'un
    acheteur non cible n'est jamais retenue, quels que soient ses mots-cles.
    Les mots-cles (config.KEYWORDS_AO) mesurent si l'offre est dans le metier
    et permettent de passer outre une exclusion quand le score depasse
    SEUIL_FORCE_AO.

    Ils sont cherches dans l'OBJET seulement : le nom de l'acheteur contient
    presque toujours "agriculture", ce qui donnerait +1 a toutes les offres."""
    objet_lower = (objet or "").lower()
    buyer_lower = (buyer or "").lower()

    mots = [m for m in config.KEYWORDS_AO if contient(m, objet_lower)]
    score = len(mots)

    motif = None
    for exc in config.EXCLUSIONS_AO:
        if exc in objet_lower:
            motif = exc
            break
        if exc in buyer_lower:
            motif = f"acheteur {exc}"
            break

    if motif and score <= config.SEUIL_FORCE_AO:
        return 0, f"Exclu ({motif})"

    for target in config.TARGET_BUYERS:
        if target.lower() in buyer_lower:
            if motif:
                log(f"   ↗️ [AO] exclusion '{motif}' ignoree : score {score} "
                    f"({', '.join(mots)})")
            return 100 + score, f"Agri (score {score}: {', '.join(mots) or '-'})"

    return 0, "Acheteur Non-Cible"


# =========================================================
#            OUTILS ANTI-"CONTEXT DESTROYED"
# =========================================================
def _settle(page, timeout=45000):
    """Attend que la page ait fini de se recharger ET que le tableau soit la."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass   # networkidle n'est jamais atteint sur certaines pages : pas grave
    try:
        page.wait_for_selector(ROWS_SELECTOR, timeout=timeout)
        return True
    except Exception:
        return False


def _safe_count(page, retries=4):
    """rows.count() echoue si la page navigue encore. On retente apres settle."""
    for attempt in range(1, retries + 1):
        try:
            return page.locator(ROWS_SELECTOR).count()
        except Exception as e:
            if attempt == retries:
                log(f"⚠️ [AO] comptage impossible apres {retries} essais : {str(e)[:70]}")
                return 0
            log(f"⏳ [AO] page encore en navigation, nouvel essai {attempt}/{retries}...")
            time.sleep(3)
            _settle(page)
    return 0


def _postback(page, action, label, timeout=60000):
    """Execute une action qui declenche un postback, puis attend la navigation.
    Certains postbacks sont en AJAX (pas de navigation) : on retombe alors
    sur _settle, qui suffit."""
    try:
        with page.expect_navigation(timeout=timeout, wait_until="domcontentloaded"):
            action()
    except Exception:
        log(f"   ↳ [AO] {label} : pas de navigation detectee (AJAX ?), on attend le tableau")
    ok = _settle(page)
    time.sleep(1.5)   # petite marge, le tableau se re-rend apres le DOM
    return ok


# =========================================================
#                        SCAN
# =========================================================
def run(context):
    """context = BrowserContext Playwright partage. Retourne (ok, alerts)."""
    alerts = []

    today = datetime.now()
    date_pub_start = (today - timedelta(days=180)).strftime("%d/%m/%Y")
    date_pub_end = today.strftime("%d/%m/%Y")
    date_deadline_start = today.strftime("%d/%m/%Y")
    date_deadline_end = (today + timedelta(days=180)).strftime("%d/%m/%Y")

    page = context.new_page()
    # On NE bloque que les medias lourds : le formulaire ASP.NET depend du CSS/JS.
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ico}", lambda route: route.abort())

    try:
        log(f"🌍 [AO] Publication {date_pub_start} -> {date_pub_end}")
        page.goto(config.URL_AO, timeout=90000, wait_until="domcontentloaded")

        # --- REMPLISSAGE DU FORMULAIRE ---
        page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_procedureType", "1")  # AO ouvert
        page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_categorie", "3")      # Services

        # ⚠️ Champs volontairement identiques au code d'origine.
        # Le portail nomme ses champs de facon trompeuse : ne pas "corriger"
        # sans avoir verifie le resultat a la main sur le site.
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneStart", date_deadline_start)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneEnd", date_deadline_end)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeStart", date_pub_start)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeEnd", date_pub_end)

        log("📝 [AO] recherche...")
        _postback(
            page,
            lambda: page.click("#ctl0_CONTENU_PAGE_AdvancedSearch_lancerRecherche"),
            "recherche",
        )

        if not _settle(page, timeout=30000):
            # Aucune carte n'apparait. Legitime si le portail annonce 0 offre ;
            # une rupture s'il en annonce.
            sentinelle.verifier("ao", portail.lire_total(page), 0)
            log("⚠️ [AO] aucun resultat.")
            return True, alerts

        total_results = portail.lire_total(page)
        if total_results:
            log(f"📊 [AO] {total_results} offres.")
        extraites = 0     # cartes reellement lues (avant tout filtre)

        # --- PASSAGE A 500 PAR PAGE (postback) ---
        page_size = 10
        if total_results > 10:
            log("🔄 [AO] passage a 500 resultats par page...")
            _postback(
                page,
                lambda: page.select_option(
                    "#ctl0_CONTENU_PAGE_resultSearch_listePageSizeTop", "500"),
                "taille de page",
            )
            n = _safe_count(page)
            if n > 10:
                page_size = 500
                log(f"   ↳ [AO] OK, {n} lignes sur la page.")
            else:
                log(f"   ↳ [AO] le passage a 500 n'a pas pris ({n} lignes), "
                    "on continue en 10 par page.")

        total_pages = max(1, math.ceil(total_results / page_size)) if total_results else 1
        # Garde-fou : en 10 par page, 934 offres feraient 94 pages et 20 minutes.
        MAX_PAGES = 25
        if total_pages > MAX_PAGES:
            log(f"⚠️ [AO] {total_pages} pages : on s'arrete a {MAX_PAGES}.")
            total_pages = MAX_PAGES

        # --- BOUCLE SUR LES PAGES ---
        for current_page in range(1, total_pages + 1):
            count_on_page = _safe_count(page)
            log(f"📄 [AO] page {current_page}/{total_pages} ({count_on_page} lignes)...")

            if count_on_page == 0:
                log("⚠️ [AO] page vide, arret de la pagination.")
                break

            try:
                cartes = portail.lire_cartes_ao(page, "AO")
            except Exception as e:
                # La page a pu naviguer sous nos pieds : on tente la suivante.
                log(f"   ⚠️ [AO] lecture des cartes impossible : {str(e)[:70]}")
                cartes = []

            for i, c in enumerate(cartes):
                try:
                    if not (c["ref"] or c["objet"]):
                        continue          # carte vide : rien d'extrait
                    extraites += 1

                    buyer = c["acheteur"] or "N/A"
                    objet = c["objet"] or "N/A"
                    deadline = c["deadline"] or "-"
                    link = c["lien"] or config.URL_AO
                    offer_id = c["offer_id"]

                    # Historique par client : on ne saute que si TOUS l'ont recue
                    # Les clients en pause sont ecartes : ni envoi ni marquage.
                    _abos = config.filtrer_actifs(
                        [x for x in config.SUBSCRIBERS if x.get("ao")])
                    if store.tous_ont_vu(_abos, offer_id):
                        # Sans cette trace, une offre deja marquee disparait des
                        # logs : impossible de savoir pourquoi elle n'arrive pas.
                        if config.DEBUG_SCORING:
                            log(f"   ↩️ [AO] deja envoyee a tous | {offer_id} | {buyer[:45]}")
                        continue

                    score, reason = scorer(objet, buyer)

                    if config.DEBUG_SCORING:
                        etat = "✅" if score > 0 else "❌"
                        log(f"   {etat} {reason} | {buyer[:45]}")

                    if score <= 0:
                        continue

                    recipients = _abos
                    if not recipients:
                        continue

                    msg = (
                        f"🚜 **[AO] OFFRE AGRI CIBLÉE** 🚜\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏛️ *Acheteur :* {buyer}\n"
                        f"📅 *Limite :* `{deadline}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"{objet}\n\n"
                        f"🔗 [VOIR L'OFFRE]({link})"
                    )

                    alerts.append({
                        "sort_key": score,
                        "msg": msg,
                        "wa_params": ["🎯 [AO] OFFRE AGRI CIBLÉE", buyer, objet,
                                      deadline, "Maroc", link],
                        "id": offer_id,
                        "recipients": recipients,
                    })
                    log(f"   ✅ [AO] retenue : {buyer[:50]}")

                except Exception as e:
                    log(f"   ⚠️ [AO] carte {i} : {str(e)[:70]}")
                    continue

            # --- PAGE SUIVANTE (postback) ---
            if current_page < total_pages:
                log("➡️ [AO] page suivante...")
                ok = _postback(
                    page,
                    lambda: page.click("#ctl0_CONTENU_PAGE_resultSearch_PagerTop_ctl2"),
                    "page suivante",
                )
                if not ok:
                    log("⚠️ [AO] page suivante indisponible, arret.")
                    break

        # On memorise AVANT l'envoi : mieux vaut rater une alerte que spammer.
        # L'historique est tenu PAR CLIENT au moment de l'envoi (notifier.broadcast)

        # Sentinelle : le portail annonce des offres mais aucune n'a ete lue.
        # Une rupture n'est pas un echec de passage (voir sentinelle.py).
        sentinelle.verifier("ao", total_results, extraites)

        return True, alerts

    except Exception as e:
        log(f"❌ [AO] erreur : {str(e)[:200]}")
        return False, alerts

    finally:
        try:
            page.close()
        except Exception:
            pass
