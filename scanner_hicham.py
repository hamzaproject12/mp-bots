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
from urllib.parse import urljoin

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


# Le portail range chaque information dans un element a IDENTIFIANT STABLE
# (..._cautionProvisoire, ..._qualification, ..._labelReferentielZoneText).
# On les lit DIRECTEMENT : plus fiable que de fouiller le texte de la page,
# et ca marche meme quand le bloc est replie derriere un (+).
_FICHE_JS = """
() => {
  const T = e => e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const liste = e => {
    if (!e) return '';
    const li = [...e.querySelectorAll('li')].map(x => T(x)).filter(Boolean);
    return li.length ? li.join(' ; ') : T(e);
  };
  const champ = nom => document.querySelector(
      "[id$='idEntrepriseConsultationSummary_" + nom + "']");

  let estimation = '';
  document.querySelectorAll(
    "[id*='idEntrepriseConsultationSummary'][id$='_labelReferentielZoneText']"
  ).forEach(v => {
    const bloc = v.closest("[id$='panelReferentielZoneText']");
    const t = bloc ? bloc.querySelector("[id$='_titre']") : null;
    if (!estimation && t && /estimation/i.test(t.textContent)) estimation = T(v);
  });

  // Lien "Detail des lots" : href="javascript:popUp('index.php?page=...')"
  let urlLots = '';
  const a = document.querySelector("[id$='_linkDetailLots']");
  if (a) {
    const h = (a.getAttribute('href') || '') + ' ' + (a.getAttribute('onclick') || '');
    const m = h.match(/popUp\('([^']+)'/);
    if (m) urlLots = m[1];
  }

  return {estimation: estimation,
          caution: T(champ('cautionProvisoire')),
          quals: liste(champ('qualification')),
          nbLots: T(champ('nbrLots')),
          urlLots: urlLots};
}
"""

# Fenetre "Detail des lots" : memes libelles, mais un jeu par lot, indexe par
# repeaterLots_ctl0, ctl1, ctl2... C'est la SEULE source des montants quand la
# consultation est allotie (la page principale les laisse vides).
_LOTS_JS = """
() => {
  const T = e => e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const liste = e => {
    if (!e) return '';
    const li = [...e.querySelectorAll('li')].map(x => T(x)).filter(Boolean);
    return li.length ? li.join(' ; ') : T(e);
  };
  const index = e => {
    const m = (e.id || '').match(/repeaterLots_ctl(\d+)_/);
    return m ? parseInt(m[1], 10) : null;
  };
  const lots = {};
  const lot = i => (lots[i] = lots[i] || {estimation: '', caution: '', quals: ''});

  document.querySelectorAll(
    "[id*='repeaterLots_ctl'][id$='_labelReferentielZoneText']"
  ).forEach(v => {
    const i = index(v);
    if (i === null) return;
    const bloc = v.closest("[id$='panelReferentielZoneText']");
    const t = bloc ? bloc.querySelector("[id$='_titre']") : null;
    if (t && /estimation/i.test(t.textContent) && !lot(i).estimation)
      lot(i).estimation = T(v);
  });
  document.querySelectorAll(
    "[id*='repeaterLots_ctl'][id$='_cautionProvisoire']"
  ).forEach(v => { const i = index(v); if (i !== null) lot(i).caution = T(v); });
  document.querySelectorAll(
    "[id*='repeaterLots_ctl'][id$='_qualification']"
  ).forEach(v => { const i = index(v); if (i !== null) lot(i).quals = liste(v); });

  return Object.keys(lots).map(Number).sort((a, b) => a - b)
           .map(i => ({n: i + 1, estimation: lots[i].estimation,
                       caution: lots[i].caution, quals: lots[i].quals}));
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


def _lire_lots(context, url):
    """Ouvre la fenetre 'Detail des lots'. Retourne une liste de dicts
    {n, estimation, caution, qualif, classe}, vide si illisible."""
    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ico}", lambda r: r.abort())
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        brut = page.evaluate(_LOTS_JS) or []
    except Exception as e:
        log(f"   ⚠️ [{NAME}] detail des lots illisible : {str(e)[:70]}")
        return []
    finally:
        try:
            page.close()
        except Exception:
            pass

    lots = []
    for l in brut:
        quals = _valeur_propre(l.get("quals"))
        lots.append({
            "n": l.get("n"),
            "estimation": _valeur_propre(l.get("estimation")),
            "caution": _valeur_propre(l.get("caution")),
            "qualif": _qualif_courte(quals),
            "classe": _classe(quals),
        })
    return lots


def lire_fiche(context, url):
    """Lit la fiche de detail. Retourne un dict de champs, plus 'lots' quand
    la consultation est allotie. En cas d'echec : dict vide, l'alerte part
    quand meme avec 'Non precise'."""
    page = context.new_page()
    page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ico}", lambda r: r.abort())
    try:
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        brut = page.evaluate(_FICHE_JS) or {}
        quals = _valeur_propre(brut.get("quals"))
        out = {
            "estimation": _valeur_propre(brut.get("estimation")),
            "caution": _valeur_propre(brut.get("caution")),
            "quals": quals,
            "qualif": _qualif_courte(quals),
            "classe": _classe(quals),
            "lots": [],
        }

        # Consultation allotie : la page principale laisse les montants vides,
        # tout est dans la fenetre "Detail des lots", un jeu par lot.
        relatif = brut.get("urlLots") or ""
        if relatif:
            out["lots"] = _lire_lots(context, urljoin(page.url, relatif))
            if out["lots"]:
                log(f"   📦 [{NAME}] {len(out['lots'])} lots lus "
                    f"({brut.get('nbLots') or 'allotissement'})")

        # Dernier recours : l'ancienne lecture par le texte de la page.
        # On ne devoile les blocs caches QUE la : sur une consultation allotie
        # cela produit un texte enorme et inutilisable.
        if not out["lots"] and not (out["estimation"] or out["caution"] or quals):
            try:
                page.evaluate(_REVELER_JS)
                texte = page.locator("body").inner_text()
                secours = _extraire(texte)
                if any(secours.values()):
                    out.update(secours)
                manquants = [k for k in ("estimation", "caution", "quals")
                             if not out.get(k)]
                log(f"   🔬 [{NAME}] lecture de secours ({len(texte)} car.), "
                    f"manquants={manquants}")
                for mot in ("estimation", "caution", "qualification"):
                    log(f"      · {mot} → {_extrait_autour(texte, mot)}")
            except Exception as e:
                log(f"   ⚠️ [{NAME}] lecture de secours impossible : {str(e)[:60]}")

        if config.DEBUG_SCORING:
            log(f"   🔬 [{NAME}] estimation={out['estimation'] or '-'} | "
                f"caution={out['caution'] or '-'} | lots={len(out['lots'])}")
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


def _qual_txt(qualif, classe):
    qualif = (qualif or "").strip()
    if qualif and classe:
        return f"{qualif} — Classe {classe}"
    if classe:
        return f"Classe {classe}"
    return qualif


def _montant(txt):
    """'1 611 758,40' ou '30 000,00 DH' -> float. None si illisible."""
    t = re.sub(r"[^0-9,.]", "", (txt or "").replace("\u00a0", " "))
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _fmt_montant(v):
    """2007206.4 -> '2 007 206,40' (format du portail)."""
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def _court(txt, maxi):
    """Les variables d'un modele WhatsApp ne doivent pas etre trop longues :
    le corps du message est plafonne par Meta."""
    t = (txt or "").strip()
    return t if len(t) <= maxi else t[:maxi - 1].rstrip() + "…"


def _resume_lots(lots, cle, maxi):
    """'L1 : x · L2 : y' sur UNE SEULE ligne (une variable de modele WhatsApp
    ne peut pas contenir de retour a la ligne). Si tous les lots portent la
    meme valeur, on ne l'ecrit qu'une fois."""
    valeurs = [(l.get("n"), (l.get(cle) or "").strip()) for l in lots]
    remplies = [v for _, v in valeurs if v]
    if not remplies:
        return ""
    if len(remplies) == len(valeurs) and len(set(remplies)) == 1:
        return _court(remplies[0], maxi)
    return _court(" · ".join(f"L{n} : {v or '-'}" for n, v in valeurs), maxi)


def _qualif_lots(lots, maxi):
    """Les lots portent presque toujours la MEME qualification, seule la classe
    change. On ecrit alors le libelle une fois et on liste les classes :
    'Travaux de seguia... — Classes L1 : 4 · L2 : 3'."""
    libelles = {(l.get("qualif") or "").strip() for l in lots}
    libelles.discard("")
    classes = [(l.get("n"), (l.get("classe") or "").strip()) for l in lots]
    if len(libelles) == 1 and any(c for _, c in classes):
        lib = libelles.pop()
        if len({c for _, c in classes}) == 1:
            return _court(_qual_txt(lib, classes[0][1]), maxi)
        suite = " · ".join(f"L{n} : {c or '-'}" for n, c in classes)
        return _court(f"{lib} — Classes {suite}", maxi)
    return _resume_lots(lots, "qual_txt", maxi)


# Meta plafonne le CORPS du message a 1024 caracteres. Le texte fixe du modele
# 'alerte_ao_travaux' en occupe 405 : il reste donc ~595 pour les 8 variables.
# Au-dela, Meta refuse l'envoi ; on degrade donc le detail des lots plutot que
# de perdre l'alerte. Le detail complet reste dans le message Telegram.
BUDGET_WA = 595


def _ajuster(wa, replis):
    """Rabote les variables jusqu'a tenir dans le budget.
    `replis` = [(position, texte_de_repli)] dans l'ORDRE DE SACRIFICE :
    on perd d'abord le detail de l'estimation (le total suffit), puis celui
    de la caution, et la qualification en dernier car c'est elle qui dit a
    l'entreprise si elle peut soumissionner."""
    for pos, repli in replis:
        if sum(len(x) for x in wa) <= BUDGET_WA:
            return wa
        if repli and len(repli) < len(wa[pos]):
            wa[pos] = repli
    if sum(len(x) for x in wa) > BUDGET_WA:
        # Reste trop long : c'est l'objet (variable 3) qui deborde.
        reste = sum(len(x) for i, x in enumerate(wa) if i != 2)
        wa[2] = _court(wa[2], max(60, BUDGET_WA - reste))
    return wa


def construire_messages(d):
    """Retourne (message_telegram, parametres_whatsapp).

    Les 8 variables du modele 'alerte_ao_travaux' sont FIXES. Quand la
    consultation est allotie, on fait donc tenir le detail des lots dans les
    memes variables (estimation / caution / qualification), sur une ligne.
    """
    lots = d.get("lots") or []
    for l in lots:
        l["qual_txt"] = _qual_txt(l.get("qualif"), l.get("classe"))

    if lots:
        est = _resume_lots(lots, "estimation", 300)
        montants = [_montant(l.get("estimation")) for l in lots]
        if est and all(m is not None for m in montants) and len(lots) > 1:
            est = _court(f"Total {_fmt_montant(sum(montants))} "
                         f"({len(lots)} lots) · {est}", 340)
        caution = _resume_lots(lots, "caution", 300)
        qual_txt = _qualif_lots(lots, 300)
        total = (f"Total {_fmt_montant(sum(montants))} ({len(lots)} lots)"
                 if all(m is not None for m in montants) else
                 f"{len(lots)} lots, voir le dossier")
        renvoi = f"Voir le detail des {len(lots)} lots"
        replis = [(4, total), (5, renvoi), (6, renvoi)]
    else:
        est = d.get("estimation") or ""
        caution = d.get("caution") or ""
        qual_txt = _qual_txt(d.get("qualif"), d.get("classe"))
        replis = []

    bloc_lots = ""
    if lots:
        lignes = [f"📦 *Allotissement :* {len(lots)} lots"]
        for l in lots:
            lignes.append(
                f"  • *L{l['n']}* — 💰 {_ou(l.get('estimation'), '-')}"
                f" | 🛡️ {_ou(l.get('caution'), '-')}"
                f" | 🏗️ {_ou(l.get('qual_txt'), '-')}"
            )
        bloc_lots = "\n".join(lignes) + "\n━━━━━━━━━━━━━━━━━━━━\n"

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
        f"{bloc_lots}"
        f"💰 *Estimation :* {_ou(est)}\n"
        f"🛡️ *Caution provisoire :* {_ou(caution)}\n"
        f"🏗️ *Qualification :* {_ou(qual_txt)}\n"
        f"📅 *Limite de remise :* `{_ou(d['deadline'])}`\n\n"
        f"🔗 [Voir la consultation]({d['link']})"
    )

    # 8 variables, dans l'ordre du modele alerte_ao_travaux (inchange).
    # Meta refuse un parametre contenant un retour a la ligne, une tabulation
    # ou 4 espaces de suite : d'ou _une_ligne().
    wa = [
        d["buyer"],
        d["ref"],
        d["objet"],
        _ou(d["deadline"]),
        _ou(est),
        _ou(caution),
        _ou(qual_txt),
        d["link"],
    ]
    wa = _ajuster([_une_ligne(v) for v in wa], replis)
    trop = sum(len(v) for v in wa) - BUDGET_WA
    if trop > 0:
        log(f"   ⚠️ [{NAME}] message WhatsApp encore trop long de {trop} car. "
            f"({d.get('ref')})")
    return msg, wa


def _une_ligne(v):
    return re.sub(r"[ \t]{4,}", "   ", re.sub(r"\s*\n\s*", " ", str(v or ""))).strip()


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
                "lots": detail.get("lots") or [],
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
