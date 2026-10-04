"""
Client HICHAM — appels d'offres TRAVAUX du secteur agricole.

Chaine de traitement :
  1. Recherche avancee : AO ouvert + categorie Travaux + date limite >= aujourd'hui
  2. Passage a 500 resultats par page
  3. Filtre ACHETEUR sur les 40 noms cibles (elimine ~97 % des lignes)
  4. Ecarte les references deja envoyees (historique seen_hicham.json)
  5. Ouvre la fiche de detail, deplie le (+), lit estimation / caution /
     qualifications / classe
  6. Filtre CONCEPT sur l'objet
  7. Envoi

Le portail est une application ASP.NET : chaque changement de page est un
POSTBACK. Il faut attendre la NAVIGATION, pas un delai fixe.
"""
import os
import re
import time
import math
import hashlib
import unicodedata
from functools import lru_cache
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import config
import store
import config_hicham as CH
from notifier import log

NAME = CH.CLIENT
ROWS_SELECTOR = ".table-results tbody tr"


# =========================================================
#              NORMALISATION ET COMPARAISON
# =========================================================
def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def norm(s):
    """Majuscules, sans accents, apostrophes unifiees, espaces compactes."""
    t = _strip_accents(s).upper().replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", t).strip()


# --- Acheteurs : comparaison exacte apres normalisation ---
@lru_cache(maxsize=1)
def _cibles():
    return {norm(a) for a in CH.ACHETEURS}


def acheteur_cible(buyer):
    return norm(buyer) in _cibles()


# --- Filet de securite : nom proche mais pas identique ---
_MOTS_VIDES = {"LE", "LA", "LES", "DE", "DU", "DES", "D", "L", "ET", "A"}


def _mots(s):
    return [w for w in re.split(r"[^A-Z0-9]+", norm(s)) if w and w not in _MOTS_VIDES]


def acheteur_proche(buyer):
    """Retourne la cible la plus ressemblante si le nom n'est pas identique
    mais tres proche. Sert UNIQUEMENT a alerter dans les logs : on ne declenche
    jamais d'alerte sur cette base, pour ne pas inventer de correspondance."""
    bw = set(_mots(buyer))
    if not bw:
        return None
    best, best_score = None, 0.0
    for cible in CH.ACHETEURS:
        cw = set(_mots(cible))
        if not cw:
            continue
        inter = len(bw & cw) / max(len(cw), 1)
        ratio = SequenceMatcher(None, norm(buyer), norm(cible)).ratio()
        score = max(inter, ratio)
        if score > best_score:
            best, best_score = cible, score
    return best if best_score >= 0.80 else None


# --- Concepts : racines presentes dans l'objet ---
def _tokens(texte):
    return [w for w in re.split(r"[^A-Z0-9]+", norm(texte)) if w]


def concepts_trouves(objet):
    tk = _tokens(objet)
    trouves = []
    for nom, alternatives in CH.CONCEPTS.items():
        for alt in alternatives:
            if all(any(t.startswith(racine) for t in tk) for racine in alt):
                trouves.append(nom)
                break
    return trouves


def exclu(objet):
    o = norm(objet)
    for mot in CH.EXCLUSIONS:
        if norm(mot) in o:
            return mot
    return None


# =========================================================
#            POSTBACKS ASP.NET (repris de scanner_ao)
# =========================================================
def _settle(page, timeout=45000):
    for etat in ("domcontentloaded", "networkidle"):
        try:
            page.wait_for_load_state(etat, timeout=timeout)
        except Exception:
            pass
    try:
        page.wait_for_selector(ROWS_SELECTOR, timeout=timeout)
        return True
    except Exception:
        return False


def _safe_count(page, retries=4):
    for essai in range(1, retries + 1):
        try:
            return page.locator(ROWS_SELECTOR).count()
        except Exception as e:
            if essai == retries:
                log(f"⚠️ [{NAME}] comptage impossible : {str(e)[:70]}")
                return 0
            time.sleep(3)
            _settle(page)
    return 0


def _postback(page, action, label, timeout=60000):
    try:
        with page.expect_navigation(timeout=timeout, wait_until="domcontentloaded"):
            action()
    except Exception:
        log(f"   ↳ [{NAME}] {label} : pas de navigation (AJAX ?), on attend le tableau")
    ok = _settle(page)
    time.sleep(1.5)
    return ok


# =========================================================
#        LECTURE DE LA FICHE DE DETAIL (par libelle)
# =========================================================
# On cherche le LIBELLE dans le texte de la page et on prend ce qui suit.
# Plus robuste qu'un identifiant technique si le portail change sa mise en page.
_CHAMPS = {
    "estimation": r"Estimation\s*\(\s*en\s*Dhs\s*TTC\s*\)[\s:*]*([^\n]+)",
    "caution":    r"Caution\s+provisoire[^:\n]{0,30}:?\s*([^\n]+)",
    "quals":      r"Qualifications?[^:\n]{0,30}:?\s*([^\n]+(?:\n(?!\s*\w+\s*:)[^\n]+)*)",
}

# Mots qui annoncent un AUTRE libelle : si la "valeur" lue en commence par
# l'un d'eux, c'est que le vrai champ est vide (on a lu le libelle suivant).
_LIBELLES = re.compile(
    r"^(caution|estimation|qualification|date|lieu|objet|reference|echantillon|"
    r"visite|variante|categorie|lot|domaine|agr[eé]ment|r[eé]union)", re.I)

# Le portail replie certains blocs derriere un (+) : leur texte est dans la page
# mais CACHE, donc inner_text() ne le voit pas. On force l'affichage de tout ce
# qui est cache avant de lire, sans dependre d'un bouton precis.
_REVELER_JS = """
() => {
  const skip = new Set(['HEAD','SCRIPT','STYLE','NOSCRIPT','TEMPLATE','META',
                        'LINK','TITLE','OPTION','SELECT']);
  document.querySelectorAll('body *').forEach(e => {
    if (skip.has(e.tagName)) return;
    const cs = getComputedStyle(e);
    if (cs.display === 'none') {
      const d = e.tagName === 'TR' ? 'table-row'
              : (e.tagName === 'TD' || e.tagName === 'TH') ? 'table-cell'
              : e.tagName === 'SPAN' ? 'inline' : 'block';
      e.style.setProperty('display', d, 'important');
    }
    if (cs.visibility === 'hidden') e.style.setProperty('visibility', 'visible', 'important');
  });
}
"""


_ZONES_JS = """
() => {
  const res = [];
  document.querySelectorAll("[id$='_labelReferentielZoneText']").forEach(v => {
    const bloc = v.closest("[id$='panelReferentielZoneText']");
    const t = bloc ? bloc.querySelector("[id$='_titre']") : null;
    res.push([t ? t.textContent : '', v.textContent]);
  });
  return res;
}
"""


def _valeur_propre(brut):
    """Nettoie une valeur lue ; vide si c'est en fait le libelle suivant."""
    v = re.sub(r"\s+", " ", brut or "")
    v = v.split("@@@@")[0]                    # texte d'info-bulle du portail
    v = re.sub(r"^[\s:*]+", "", v).strip()    # etoile "champ obligatoire", ":"
    if not v or _LIBELLES.match(v):
        return ""
    return v[:400]


def _extraire(texte):
    out = {}
    for cle, motif in _CHAMPS.items():
        m = re.search(motif, texte, re.I)
        out[cle] = _valeur_propre(m.group(1)) if m else ""
    out["classe"] = _classe(out.get("quals", ""))
    out["qualif"] = _qualif_courte(out.get("quals", ""))
    return out


def _extrait_autour(texte, mot, largeur=110):
    """Petit extrait du texte autour du 1er mot (pour comprendre un echec)."""
    plat = re.sub(r"\s+", " ", texte)
    i = plat.lower().find(mot)
    if i < 0:
        return "ABSENT"
    return plat[max(0, i - 15): i + largeur]


def _classe(texte_quals):
    """Extrait le numero de classe : 'Classe 3' -> '3'."""
    m = re.search(r"CLASSE\s*:?\s*(\d)", norm(texte_quals or ""))
    return m.group(1) if m else ""


def _qualif_courte(texte_quals):
    """Garde le libelle metier, sans l'arborescence complete.
    'Agriculture / Amenagement de pistes ... / 7.1.Amenagement ... / Classe 3'
    -> 'Amenagement de pistes agricoles et rurales'"""
    if not texte_quals:
        return ""
    parts = [p.strip() for p in texte_quals.split("/") if p.strip()]
    parts = [p for p in parts if not re.match(r"^\s*classe\s*\d", p, re.I)]
    if not parts:
        return ""
    # on prefere le 2e segment (le libelle), sinon le dernier non numerote
    for p in parts[1:]:
        if not re.match(r"^\d+(\.\d+)*\.?", p):
            return p[:120]
    return parts[-1][:120]


def lire_fiche(context, url):
    """Ouvre la fiche, revele les blocs replies, retourne un dict de champs.
    En cas d'echec, retourne un dict vide : l'alerte partira quand meme."""
    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ico}", lambda r: r.abort())
    try:
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # 1) on essaie de cliquer sur le (+) comme un humain
        for sel in ("a.toggle-detail", "img[src*='plus']", "a[id*='expand']",
                    "span.ui-icon-plus", "a:has-text('+')"):
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=5000)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue

        # 2) et surtout on force l'affichage de tout ce qui reste cache
        try:
            page.evaluate(_REVELER_JS)
        except Exception as e:
            log(f"   ⚠️ [{NAME}] revelation des blocs impossible : {str(e)[:60]}")

        texte = page.locator("body").inner_text()
        out = _extraire(texte)

        # Champ "Estimation" : le portail le range dans un bloc a identifiants
        # fixes (..._titre / ..._labelReferentielZoneText). On le lit DIRECTEMENT,
        # c'est plus fiable que le texte de la page.
        try:
            champs = page.evaluate(_ZONES_JS) or []
            for titre, valeur in champs:
                if re.search(r"estimation", titre or "", re.I):
                    v = _valeur_propre(valeur)
                    if v:
                        out["estimation"] = v
                        break
        except Exception:
            pass

        # 3) dernier recours : textContent (inclut meme le texte cache)
        if not (out["estimation"] or out["caution"] or out["quals"]):
            try:
                brut = page.evaluate("document.body.textContent") or ""
                # textContent n'a plus les retours a la ligne : on les remet
                # avant chaque libelle connu pour que les motifs fonctionnent.
                brut = re.sub(r"\s+", " ", brut)
                brut = re.sub(r"(Estimation|Caution provisoire|Qualification)",
                              r"\n\1", brut, flags=re.I)
                texte = brut
                out = _extraire(texte)
            except Exception:
                pass

        # Diagnostic : si un champ manque, on montre ce que la page contient
        manquants = [k for k in ("estimation", "caution", "quals") if not out.get(k)]
        if manquants or config.DEBUG_SCORING:
            log(f"   🔬 [{NAME}] fiche lue ({len(texte)} car.), manquants={manquants}")
            for mot in ("estimation", "caution", "qualification"):
                log(f"      · {mot} → {_extrait_autour(texte, mot)}")
        return out
    except Exception as e:
        log(f"   ⚠️ [{NAME}] fiche illisible : {str(e)[:70]}")
        return {}
    finally:
        try:
            page.close()
        except Exception:
            pass


# =========================================================
#                   MISE EN FORME
# =========================================================
def _ou(valeur, defaut="Non precise"):
    v = (valeur or "").strip()
    return v if v else defaut


def construire_messages(d):
    """Retourne (message_telegram, parametres_whatsapp)."""
    qualif = _ou(d["qualif"], "")
    classe = d["classe"]
    if qualif and classe:
        qual_txt = f"{qualif} — Classe {classe}"
    elif classe:
        qual_txt = f"Classe {classe}"
    else:
        qual_txt = _ou(qualif)

    msg = (
        f"🏛️🏛️ **APPEL D'OFFRES — TRAVAUX** 🏛️🏛️\n"
        f"_Veille marches publics agricoles_\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 *Acheteur :* {d['buyer']}\n"
        f"🔖 *Reference :* `{d['ref']}`\n"
        f"🏷️ *Theme :* {', '.join(d['concepts'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 {d['objet']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Estimation :* {_ou(d['estimation'])}\n"
        f"🛡️ *Caution provisoire :* {_ou(d['caution'])}\n"
        f"🏗️ *Qualification :* {qual_txt}\n"
        f"📅 *Limite de remise :* `{_ou(d['deadline'])}`\n\n"
        f"🔗 [Voir la consultation]({d['link']})"
    )

    # 8 variables, dans l'ordre du template alerte_ao_travaux
    wa = [
        d["buyer"],
        d["ref"],
        d["objet"],
        _ou(d["deadline"]),
        _ou(d["estimation"]),
        _ou(d["caution"]),
        qual_txt or "Non precise",
        d["link"],
    ]
    return msg, wa


# =========================================================
#                        SCAN
# =========================================================
def run(context):
    """context = BrowserContext partage. Retourne (ok, alertes)."""
    alertes = []
    proches_signales = set()

    today = datetime.now()
    pub_debut = (today - timedelta(days=CH.JOURS_PUBLICATION)).strftime("%d/%m/%Y")
    pub_fin = today.strftime("%d/%m/%Y")
    lim_debut = today.strftime("%d/%m/%Y")
    lim_fin = (today + timedelta(days=CH.JOURS_LIMITE)).strftime("%d/%m/%Y")

    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ico}", lambda r: r.abort())

    try:
        log(f"🌍 [{NAME}] Travaux | publication {pub_debut} → {pub_fin} | "
            f"limite {lim_debut} → {lim_fin}")
        page.goto(config.URL_AO, timeout=90000, wait_until="domcontentloaded")

        page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_procedureType",
                           CH.PROCEDURE_TYPE)
        page.select_option("#ctl0_CONTENU_PAGE_AdvancedSearch_categorie",
                           CH.CATEGORIE)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneStart", lim_debut)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneEnd", lim_fin)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeStart", pub_debut)
        page.fill("#ctl0_CONTENU_PAGE_AdvancedSearch_dateMiseEnLigneCalculeEnd", pub_fin)

        log(f"📝 [{NAME}] recherche...")
        _postback(page,
                  lambda: page.click("#ctl0_CONTENU_PAGE_AdvancedSearch_lancerRecherche"),
                  "recherche")

        if not _settle(page, timeout=30000):
            log(f"⚠️ [{NAME}] aucun resultat.")
            return True, alertes

        try:
            total = int(page.locator(
                "#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text().strip())
            log(f"📊 [{NAME}] {total} travaux publies.")
        except Exception:
            total = 0

        taille_page = 10
        if total > 10:
            log(f"🔄 [{NAME}] passage a 500 resultats par page...")
            _postback(page,
                      lambda: page.select_option(
                          "#ctl0_CONTENU_PAGE_resultSearch_listePageSizeTop", "500"),
                      "taille de page")
            n = _safe_count(page)
            if n > 10:
                taille_page = 500
                log(f"   ↳ [{NAME}] OK, {n} lignes affichees.")
            else:
                log(f"   ↳ [{NAME}] passage a 500 refuse ({n} lignes), on reste a 10.")

        pages_total = max(1, math.ceil(total / taille_page)) if total else 1
        MAX_PAGES = 25
        if pages_total > MAX_PAGES:
            log(f"⚠️ [{NAME}] {pages_total} pages, on s'arrete a {MAX_PAGES}.")
            pages_total = MAX_PAGES

        # ---- Etape 1 : collecte des lignes des acheteurs cibles ----
        retenues = []
        for num_page in range(1, pages_total + 1):
            nb = _safe_count(page)
            log(f"📄 [{NAME}] page {num_page}/{pages_total} ({nb} lignes)")
            if nb == 0:
                break

            lignes = page.locator(ROWS_SELECTOR)
            for i in range(nb):
                try:
                    ligne = lignes.nth(i)
                    if not ligne.is_visible():
                        continue
                    texte = ligne.inner_text()

                    el_b = ligne.locator("div[id*='_panelBlocDenomination']")
                    buyer = (el_b.inner_text()
                             .replace("Acheteur public\n:", "")
                             .replace("Acheteur public :", "").strip()
                             if el_b.count() else "")

                    if not acheteur_cible(buyer):
                        p = acheteur_proche(buyer)
                        if p and buyer and buyer not in proches_signales:
                            proches_signales.add(buyer)
                            log(f"   🔎 [{NAME}] nom PROCHE non retenu : "
                                f"portail='{buyer[:60]}' ≈ cible='{p[:60]}'")
                        continue

                    offer_id = hashlib.md5(texte.encode("utf-8")).hexdigest()
                    # Historique par client : on n'ouvre la fiche (couteux)
                    # que si au moins un abonne ne l'a pas encore recue.
                    if store.tous_ont_vu(CH.ABONNES, offer_id):
                        continue

                    el_o = ligne.locator("div[id*='_panelBlocObjet']")
                    objet = (el_o.inner_text()
                             .replace("Objet\n:", "")
                             .replace("Objet :", "").strip()
                             if el_o.count() else "")

                    cells = ligne.locator("td[headers='cons_dateEnd'] .cloture-line")
                    deadline = (cells.first.inner_text().replace("\n", " ").strip()
                                if cells.count() else "")

                    liens = ligne.locator("td.actions a")
                    href = liens.first.get_attribute("href") if liens.count() else None
                    link = (f"https://www.marchespublics.gov.ma/index.php{href}"
                            if href else config.URL_AO)

                    ref = ""
                    m = re.search(r"\n\s*([0-9A-Za-z][^\n]{2,60})\s*\n\s*Objet", texte)
                    if m:
                        ref = m.group(1).strip()

                    retenues.append(dict(id=offer_id, buyer=buyer, objet=objet,
                                         deadline=deadline, link=link, ref=ref))
                except Exception as e:
                    msg_e = str(e)[:70]
                    if "context was destroyed" in msg_e or "Target closed" in msg_e:
                        log(f"   ⚠️ [{NAME}] navigation en cours, page suivante")
                        break
                    continue

            if num_page < pages_total:
                if not _postback(page,
                                 lambda: page.click(
                                     "#ctl0_CONTENU_PAGE_resultSearch_PagerTop_ctl2"),
                                 "page suivante"):
                    break

        log(f"🎯 [{NAME}] {len(retenues)} annonces d'acheteurs cibles (nouvelles)")

        # ---- Etape 2 : filtre concept + lecture des fiches ----
        candidates = []
        for r in retenues:
            cs = concepts_trouves(r["objet"])
            motif_exclu = exclu(r["objet"])
            if config.DEBUG_SCORING:
                etat = "✅" if (cs and not motif_exclu) else "❌"
                log(f"   {etat} [{NAME}] {cs or '-'} "
                    f"{'(exclu: ' + motif_exclu + ')' if motif_exclu else ''} "
                    f"| {r['objet'][:70]}")
            if cs and not motif_exclu:
                r["concepts"] = cs
                candidates.append(r)

        log(f"🏷️ [{NAME}] {len(candidates)} correspondent a un theme")

        if len(candidates) > CH.MAX_FICHES:
            log(f"✂️ [{NAME}] limite a {CH.MAX_FICHES} fiches ce passage "
                f"(variable HICHAM_MAX_FICHES). {len(candidates) - CH.MAX_FICHES} "
                f"seront traitees au prochain passage.")
            candidates = candidates[:CH.MAX_FICHES]

        for r in candidates:
            detail = lire_fiche(context, r["link"])
            r.update({
                "estimation": detail.get("estimation", ""),
                "caution": detail.get("caution", ""),
                "qualif": detail.get("qualif", ""),
                "classe": detail.get("classe", ""),
            })
            if not detail:
                log(f"   ⚠️ [{NAME}] detail absent pour {r['ref']}, alerte partielle")
            msg, wa = construire_messages(r)
            alertes.append({
                "sort_key": 0,
                "msg": msg,
                "wa_params": wa,
                "wa_template": CH.WA_TEMPLATE,
                "id": r["id"],
                "recipients": CH.ABONNES,
            })
            log(f"   ✅ [{NAME}] retenue : {r['buyer'][:45]} | {r['ref']}")
            time.sleep(0.8)

        # L'historique est tenu PAR CLIENT au moment de l'envoi (notifier.broadcast)
        return True, alertes

    except Exception as e:
        log(f"❌ [{NAME}] erreur : {str(e)[:200]}")
        return False, alertes

    finally:
        try:
            page.close()
        except Exception:
            pass
