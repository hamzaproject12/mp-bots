"""
Orchestrateur : lance les deux bots (BDC + AO) dans UN SEUL conteneur,
avec UN SEUL navigateur Chromium partage.

Modes (variable RUN_MODE) :
  once  -> un passage puis sortie. A utiliser avec le cron Railway.
           Le conteneur ne tourne que quelques minutes par jour.
  loop  -> boucle infinie avec sommeil (ancien comportement, tourne 24h/24).
"""
import sys
import time
import traceback

from playwright.sync_api import sync_playwright

import config
import store
import notifier
from notifier import log

import scanner_bdc
import scanner_ao

SCANNERS = {
    "bdc": scanner_bdc,
    "ao": scanner_ao,
}

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--no-zygote",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
    "--mute-audio",
]


def run_once():
    """Un passage complet sur tous les scanners actifs. Retourne True si tout est OK."""
    actifs = [n for n in config.SCANNERS if n in SCANNERS]
    if not actifs:
        log("⚠️ Aucun scanner actif (voir la variable SCANNERS).")
        return True

    all_ok = True
    total = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=config.USER_AGENT,
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
        )
        try:
            for name in actifs:
                log(f"━━━ Scanner {name.upper()} ━━━")
                try:
                    ok, alerts = SCANNERS[name].run(context)
                except Exception as e:
                    log(f"❌ [{name}] exception : {e}")
                    if config.DEBUG_SCORING:
                        traceback.print_exc()
                    ok, alerts = False, []

                all_ok = all_ok and ok

                if alerts:
                    sent = notifier.broadcast(alerts)
                    total += sent
                    log(f"🚀 [{name}] {sent} alertes envoyees.")
                else:
                    log(f"Ø [{name}] rien de nouveau.")
        finally:
            context.close()
            browser.close()

    log(f"✅ Passage termine : {total} alertes au total.")
    return all_ok


def main():
    log(f"🚀 MP Bots V6 | mode={config.RUN_MODE} | scanners={','.join(config.SCANNERS)}")

    if not config.TELEGRAM_TOKEN:
        log("⚠️ TELEGRAM_TOKEN absent.")
    if not config.WA_TOKEN:
        log("⚠️ WA_TOKEN absent : WhatsApp desactive.")
    if store.volume_absent():
        log(f"⚠️ '{config.DATA_PATH}' n'est pas un volume monte : "
            "les offres seront renvoyees en double apres chaque deploiement.")

    if config.STARTUP_PING:
        for s in config.SUBSCRIBERS:
            if s.get("telegram"):
                notifier.send_telegram(s["telegram"], "✅ Bot operationnel.")
                break

    if config.WA_TEST:
        cible = next((s["whatsapp"] for s in config.SUBSCRIBERS if s.get("whatsapp")), None)
        if cible:
            log("🧪 WhatsApp de controle...")
            ok = notifier.send_whatsapp(cible, [
                "TEST DEMARRAGE", "AO-TEST-001", "Verification du canal WhatsApp",
                "01/01/2027 a 10:00", "Rabat", "https://www.marchespublics.gov.ma",
            ])
            log("🧪 Resultat : " + ("OK ✅" if ok else "ECHEC ❌ (voir erreur ci-dessus)"))

    if config.RUN_MODE == "once":
        ok = run_once()
        log("👋 Sortie (mode once).")
        sys.exit(0 if ok else 1)

    # --- mode loop ---
    while True:
        ok = False
        try:
            ok = run_once()
        except Exception as e:
            log(f"⚠️ Erreur : {e}")
            traceback.print_exc()
        delay = config.SLEEP_OK if ok else config.SLEEP_FAIL
        log(f"💤 Sommeil ({delay // 60} min)...")
        time.sleep(delay)


if __name__ == "__main__":
    main()
