"""
Bot BDC — bons de commande.
Filtre par MOTS-CLÉS dans le texte de l'annonce.
"""
import re
import time
import math
from functools import lru_cache
from datetime import datetime, timedelta

import config
import store
import portail
import sentinelle
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
def zone_prioritaire(text_lower):
    """Zone geographique prioritaire.
    ⚠️ C'est un BONUS, pas un laissez-passer : l'offre doit d'abord passer
    le filtre mots-cles. Sinon on recevait des marches de moquette ou
    d'hotellerie juste parce que 'Drâa-Tafilalet' figurait dans le lieu."""
    return any(contient(z, text_lower) for z in config.SPECIAL_ZONES)


def _motif_exclusion(text_lower):
    """Retourne le mot d'exclusion present dans l'annonce, ou None."""
    for exc in config.EXCLUSIONS_BDC:
        if contient(exc, text_lower):
            return exc
    return None


def _meilleure_categorie(text_lower):
    best_score, best_cat, best_mots = 0, "Pas de mots-clés", []
    for cat, mots in config.KEYWORDS.items():
        trouves = [m for m in mots if contient(m, text_lower)]
        if len(trouves) > best_score:
            best_score, best_cat, best_mots = len(trouves), cat, trouves
    return best_score, best_cat, best_mots


def scorer(text_lower):
    """Retourne (score, categorie, mots_trouves).
    Ne decide PAS du seuil : cherche seulement la MEILLEURE categorie.

    Une exclusion bloque l'offre, SAUF si le score depasse SEUIL_FORCE_BDC.
    Exemple reel : "Organisation des sessions de formation ... et de nettoyage
    des touffes" marque 3 (formation, organisation, agri) ; le mot "nettoyage"
    ne doit pas faire perdre une offre de formation agricole."""
    # Ces deux regles bloquent TOUJOURS : elles ne se declenchent justement
    # qu'en l'ABSENCE des mots du metier, un score eleve est donc impossible.
    if contient("hébergement", text_lower):
        if not any(contient(x, text_lower) for x in
                   ["web", "site", "cloud", "serveur", "plateforme", "logiciel", "données"]):
            return 0, "Exclu (Hébergement non-IT)", []

    print_words = ["impression", "banderole", "flyer", "imprimerie"]
    training_words = ["formation", "session", "atelier", "renforcement", "sensibilisation"]
    if any(contient(p, text_lower) for p in print_words):
        if not any(contient(t, text_lower) for t in training_words):
            return 0, "Exclu (Impression seule)", []

    best_score, best_cat, best_mots = _meilleure_categorie(text_lower)

    motif = _motif_exclusion(text_lower)
    if motif:
        if best_score <= config.SEUIL_FORCE_BDC:
            return 0, f"Exclu ({motif})", []
        log(f"   ↗️ [BDC] exclusion '{motif}' ignoree : score {best_score} "
            f"({', '.join(best_mots)})")

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


CARTES = "article.bdc-card"
ATTENTE_CARTES_MS = 15000   # delai d'apparition des cartes (reduit par les tests)

# Une evaluation par page (50 cartes) plutot qu'une dizaine d'appels par carte.
_CARTES_JS = """
() => {
  const T = e => e ? (e.textContent || '').replace(/\\s+/g, ' ').trim() : '';
  return [...document.querySelectorAll('article.bdc-card')].map(card => {
    const q = s => card.querySelector(s);
    const titre = q('a.bdc-card__title');
    const meta = q('.bdc-card__meta');
    // L'acheteur est le 1er <span> du bloc meta ; le lieu en est un autre.
    // Si le 1er span EST le lieu, il n'y a pas d'acheteur.
    const premier = meta ? meta.querySelector(':scope > span') : null;
    const acheteur = premier && !premier.classList.contains('bdc-card__lieu')
                     ? premier : null;
    return {
      ref: T(q('.bdc-card__ref')),
      objet: T(titre),
      href: titre ? (titre.getAttribute('href') || '') : '',
      jour: T(q('.bdc-card__v')),
      heure: T(q('.bdc-card__s')),
      lieu: T(q('.bdc-card__lieu')),
      acheteur: T(acheteur),
      badges: [...card.querySelectorAll('.bdc-card__tags .badge')].map(T),
      texte: (card.innerText || '').trim(),
    };
  });
}
"""


def normaliser_carte(brut):
    """Brut (retour du JS) -> dict propre. Pure : testable sans navigateur."""
    href = brut.get("href") or ""
    # Le site ne prefixe plus par "Reference :" ; on garde ce nettoyage au cas ou.
    ref = re.sub(r"^\s*r[ée]f[ée]rence\s*:\s*", "", brut.get("ref") or "",
                 flags=re.I).strip()
    statut = " ".join(b for b in (brut.get("badges") or []) if b)
    return {
        "ref": ref,
        "objet": (brut.get("objet") or "").strip(),
        "href": href,
        "lien": portail.urljoin(portail.PORTAIL, href) if href else "",
        "date_limite": f"{brut.get('jour') or ''} {brut.get('heure') or ''}".strip(),
        "lieu": (brut.get("lieu") or "").strip(),
        "acheteur": (brut.get("acheteur") or "").strip(),
        "statut": statut,
        # "Annule", "Annulé", "annulée"... : on compare sans accent
        "annule": "annul" in portail._sans_accents(statut).lower(),
        "texte": brut.get("texte") or "",
    }


def texte_score(carte):
    """Texte analyse par scorer() : reference + objet + lieu.

    L'ACHETEUR n'en fait volontairement PAS partie : son nom contient tres
    souvent "agri" (DIRECTION PROVINCIALE DE L'AGRICULTURE), ce qui donnerait un
    point a toutes ses offres et viderait les exclusions de leur sens. Il ne
    sert qu'a l'affichage dans le message."""
    return " ".join(x for x in (carte["ref"], carte["objet"], carte["lieu"]) if x)


def lire_cartes(page):
    return [normaliser_carte(b) for b in page.evaluate(_CARTES_JS)]


def run(context):
    """context = BrowserContext Playwright partage. Retourne (ok, alerts)."""
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
        annoncees = 0     # compteur du portail
        extraites = 0     # cartes reellement lues (avant tout filtre)

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
                        annoncees = int(num.group())
                        max_pages = math.ceil(annoncees / 50)
                        log(f"🧠 [BDC] {num.group()} offres ({max_pages} pages)")
                except Exception:
                    pass

            try:
                page.wait_for_selector(CARTES, timeout=ATTENTE_CARTES_MS)
            except Exception:
                log(f"⚠️ [BDC] aucune carte sur la page {current_page}")
                current_page += 1
                continue

            try:
                cartes = lire_cartes(page)
            except Exception as e:
                log(f"⚠️ [BDC] lecture des cartes impossible (page {current_page}) : "
                    f"{str(e)[:80]}")
                current_page += 1
                continue

            for c in cartes:
                try:
                    ref, objet, acheteur = c["ref"], c["objet"], c["acheteur"]
                    lieu, date_limite, link = c["lieu"], c["date_limite"], c["lien"]
                    if not (ref or objet):
                        continue          # carte vide : rien d'extrait
                    extraites += 1

                    # Un marche annule ne vaut aucune alerte.
                    if c["annule"]:
                        if config.DEBUG_SCORING:
                            log(f"   🚫 [BDC] annulee, ignoree : {ref}")
                        continue

                    offer_id = portail.id_bdc(c["href"], c["texte"], "BDC")

                    # L'acheteur est affiche mais JAMAIS score (voir texte_score).
                    t_lower = texte_score(c).lower()

                    # SEUL le filtre mots-cles decide. La zone n'est qu'un bonus.
                    score, category, mots = scorer(t_lower)
                    retenue = passe_le_seuil(score, category)

                    if config.DEBUG_SCORING:
                        etat = "✅" if retenue else "❌"
                        log(f"   {etat} score={score} cat={category} mots={mots}")

                    if not retenue:
                        continue

                    zone = zone_prioritaire(t_lower)

                    # filtrer_actifs : un client en pause est ecarte ICI, donc
                    # ni envoi ni marquage de son historique (il recevra tout
                    # a sa reactivation).
                    recipients = config.filtrer_actifs([
                        s for s in config.SUBSCRIBERS
                        if s.get("bdc")
                        and ("ALL" in s["subscriptions"] or category in s["subscriptions"])
                    ])
                    if not recipients:
                        log(f"↪️ [BDC] ignoree (aucun abonne actif pour '{category}') : {ref}")
                        continue

                    # Chaque client a son propre historique : on ne saute
                    # que si TOUS l'ont deja recue.
                    if store.tous_ont_vu(recipients, offer_id):
                        continue

                    emoji = "🚜🌾" if contient("agri", t_lower) else "📍🏜️" if zone else "🚨"
                    #title = f"ALERTE {category}" + (" — ZONE PRIORITAIRE" if zone else "")
                    title = f"📦 [BDC] ALERTE {category}" + (" — ZONE PRIORITAIRE" if zone else "")

                    ligne_acheteur = f"🏛️ Acheteur: {acheteur}\n" if acheteur else ""
                    msg = (
                        f"{emoji} **{title}**\n━━━━━━━━━━━━\n"
                        f"🎯 Score: {score}\n"
                        f"📅 Limite: `{date_limite}`\n"
                        f"📍 Lieu: `{lieu}`\n"
                        f"{ligne_acheteur}━━━━━━━━━━━━\n"
                        f"{ref}\nObjet: {objet}\n\n"
                        f"🔗 [Voir l'offre]({link})"
                    )

                    alerts.append({
                        # Les zones prioritaires remontent en tete de conversation
                        "sort_key": score + (100 if zone else 0),
                        "msg": msg,
                        "wa_params": [f"{title} · Score {score}", ref, objet,
                                      date_limite, lieu, link],
                        "id": offer_id,
                        "recipients": recipients,
                    })
                except Exception:
                    continue

            current_page += 1

        # L'historique est tenu PAR CLIENT au moment de l'envoi (notifier.broadcast)

        # Sentinelle : le portail annonce des offres mais aucune n'a ete lue.
        # Une rupture n'est pas un echec de passage (voir sentinelle.py) :
        # on garde le rythme normal, reessayer dans 15 min ne servirait a rien.
        sentinelle.verifier("bdc", annoncees, extraites)

        return True, alerts

    finally:
        page.close()
