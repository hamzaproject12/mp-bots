"""
Bot AO — appels d'offres (recherche avancee).
Filtre par WHITELIST D'ACHETEURS (DRA, DPA, ORMVA, ONSSA, ONCA...).
"""
import time
import math
import hashlib
from datetime import datetime, timedelta

import config
import store
from notifier import log

NAME = "ao"


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
    # On NE bloque PAS le CSS ici : le formulaire ASP.NET depend du rendu.
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ico}", lambda route: route.abort())

    try:
        log(f"🌍 [AO] Publication {date_pub_start} -> {date_pub_end}")
        page.goto(config.URL_AO, timeout=90000)

        # --- REMPLISSAGE DU FORMULAIRE ---
        page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_procedureType", "1")  # AO ouvert
        page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_categorie", "3")      # Services

        # ⚠️ Champs volontairement identiques au code d'origine.
        # Le site nomme ses champs de facon trompeuse : ne pas "corriger"
        # sans avoir verifie le resultat a la main sur le portail.
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneStart", date_deadline_start)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneEnd", date_deadline_end)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeStart", date_pub_start)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeEnd", date_pub_end)

        log("📝 [AO] recherche...")
        with page.expect_navigation(timeout=60000):
            page.click("#ctl0_CONTENU_PAGE_AdvancedSearch_lancerRecherche")

        try:
            page.wait_for_selector(".table-results", timeout=20000)
        except Exception:
            log("⚠️ [AO] aucun resultat.")
            return True, alerts

        try:
            count_text = page.locator("#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text()
            total_results = int(count_text.strip())
            log(f"📊 [AO] {total_results} offres.")
        except Exception:
            total_results = 0

        if total_results > 10:
            log("🔄 [AO] passage a 500 resultats par page...")
            try:
                with page.expect_response(lambda r: r.status == 200, timeout=60000):
                    page.select_option("#ctl0_CONTENU_PAGE_resultSearch_listePageSizeTop", "500")
                time.sleep(3)
            except Exception as e:
                log(f"⚠️ [AO] erreur affichage : {str(e)[:80]}")

        total_pages = max(1, math.ceil(total_results / 500))

        for current_page in range(1, total_pages + 1):
            log(f"📄 [AO] page {current_page}/{total_pages}...")
            rows = page.locator(".table-results tbody tr")

            for i in range(rows.count()):
                try:
                    row = rows.nth(i)
                    if not row.is_visible():
                        continue

                    full_row_text = row.inner_text()
                    offer_id = hashlib.md5(full_row_text.encode("utf-8")).hexdigest()
                    if offer_id in seen_ids:
                        continue

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

                    score, reason = scorer(objet, buyer)

                    if config.DEBUG_SCORING:
                        etat = "✅" if score > 0 else "❌"
                        log(f"   {etat} {reason} | {buyer[:45]}")

                    if score <= 0:
                        continue

                    deadline_cells = row.locator("td[headers='cons_dateEnd'] .cloture-line")
                    deadline = (deadline_cells.first.inner_text().replace("\n", " ").strip()
                                if deadline_cells.count() > 0 else "-")

                    href = row.locator("td.actions a").first.get_attribute("href")
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
                except Exception as e:
                    log(f"   ⚠️ [AO] erreur ligne {i}: {str(e)[:60]}")
                    continue

            if current_page < total_pages:
                try:
                    page.click("#ctl0_CONTENU_PAGE_resultSearch_PagerTop_ctl2")
                    page.wait_for_load_state("networkidle", timeout=30000)
                    time.sleep(2)
                except Exception:
                    break

        for a in alerts:
            seen_list.append(a["id"])
        if alerts:
            store.save_seen(NAME, seen_list)

        return True, alerts

    except Exception as e:
        log(f"❌ [AO] erreur : {str(e)[:150]}")
        return False, alerts

    finally:
        page.close()
