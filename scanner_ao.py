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
import hashlib
from datetime import datetime, timedelta

import config
import store
from notifier import log

NAME = "ao"

ROWS_SELECTOR = ".table-results tbody tr"


def scorer(objet, buyer):
    objet_lower = (objet or "").lower()
    buyer_lower = (buyer or "").lower()

    for exc in config.EXCLUSIONS_AO:
        if exc in objet_lower:
            return 0, f"Exclu ({exc})"
        if exc in buyer_lower:
            return 0, f"Exclu Acheteur ({exc})"

    for target in config.TARGET_BUYERS:
        if target.lower() in buyer_lower:
            return 100, "Agri"

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
#                    EXTRACTION
# =========================================================
def _extract_row(row):
    """Retourne (full_text, buyer, objet, deadline, href) ou None."""
    full_text = row.inner_text()

    buyer_el = row.locator("div[id*='_panelBlocDenomination']")
    buyer = (buyer_el.inner_text()
             .replace("Acheteur public\n:", "")
             .replace("Acheteur public :", "").strip()
             if buyer_el.count() > 0 else "N/A")

    objet_el = row.locator("div[id*='_panelBlocObjet']")
    objet = (objet_el.inner_text()
             .replace("Objet\n:", "")
             .replace("Objet :", "").strip()
             if objet_el.count() > 0 else "N/A")

    deadline_cells = row.locator("td[headers='cons_dateEnd'] .cloture-line")
    deadline = (deadline_cells.first.inner_text().replace("\n", " ").strip()
                if deadline_cells.count() > 0 else "-")

    link_el = row.locator("td.actions a")
    href = link_el.first.get_attribute("href") if link_el.count() > 0 else None

    return full_text, buyer, objet, deadline, href


# =========================================================
#                        SCAN
# =========================================================
def run(context):
    """context = BrowserContext Playwright partage. Retourne (ok, alerts)."""
    seen_list = store.load_seen(NAME)
    seen_ids = set(seen_list)
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
            log("⚠️ [AO] aucun resultat.")
            return True, alerts

        try:
            count_text = page.locator("#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text()
            total_results = int(count_text.strip())
            log(f"📊 [AO] {total_results} offres.")
        except Exception:
            total_results = 0

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

            rows = page.locator(ROWS_SELECTOR)

            for i in range(count_on_page):
                try:
                    row = rows.nth(i)
                    if not row.is_visible():
                        continue

                    full_text, buyer, objet, deadline, href = _extract_row(row)

                    offer_id = hashlib.md5(full_text.encode("utf-8")).hexdigest()
                    if offer_id in seen_ids:
                        continue

                    score, reason = scorer(objet, buyer)

                    if config.DEBUG_SCORING:
                        etat = "✅" if score > 0 else "❌"
                        log(f"   {etat} {reason} | {buyer[:45]}")

                    if score <= 0:
                        continue

                    link = (f"https://www.marchespublics.gov.ma/index.php{href}"
                            if href else config.URL_AO)

                    recipients = [s for s in config.SUBSCRIBERS if s.get("ao")]
                    if not recipients:
                        continue

                    msg = (
                        f"🚜 **OFFRE AGRI CIBLÉE** 🚜\n"
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
                        "wa_params": ["OFFRE AGRI CIBLÉE", buyer, objet,
                                      deadline, "Maroc", link],
                        "id": offer_id,
                        "recipients": recipients,
                    })
                    log(f"   ✅ [AO] retenue : {buyer[:50]}")

                except Exception as e:
                    msg_err = str(e)[:70]
                    if "context was destroyed" in msg_err or "Target closed" in msg_err:
                        # La page a navigue sous nos pieds : inutile de continuer
                        # cette page, on tente la suivante.
                        log(f"   ⚠️ [AO] navigation pendant la lecture, on passe a la suite")
                        break
                    log(f"   ⚠️ [AO] ligne {i} : {msg_err}")
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
        for a in alerts:
            seen_list.append(a["id"])
        if alerts:
            store.save_seen(NAME, seen_list)

        return True, alerts

    except Exception as e:
        log(f"❌ [AO] erreur : {str(e)[:200]}")
        return False, alerts

    finally:
        try:
            page.close()
        except Exception:
            pass