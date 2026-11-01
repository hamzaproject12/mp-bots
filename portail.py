"""
Outils partages par les scanners pour lire le portail marchespublics.gov.ma.

Migration d'octobre 2026 : les listes de resultats sont passees de tableaux a
des cartes. Ce module regroupe tout ce qui depend de ce rendu ou du format des
dates, pour que scanner_ao et scanner_hicham ne le dupliquent pas :

  - parse_date()     : toutes les ecritures de date du portail -> datetime
  - id_bdc / id_ao   : identifiants STABLES des offres (a la place du md5 du
                       texte, qui change des que le HTML change)
  - lire_cartes_ao() : extraction des cartes "div.item.tender_item"
"""
import re
import hashlib
import unicodedata
from datetime import datetime
from urllib.parse import urljoin, urlsplit, parse_qs

from notifier import log

PORTAIL = "https://www.marchespublics.gov.ma/"


# =========================================================
#                         DATES
# =========================================================
# Abreviations ET noms complets, sans accent, en minuscules. Une table
# explicite plutot qu'un prefixe de 3 lettres : "jui" serait ambigu entre
# juin et juillet, et on prefere refuser que deviner.
_MOIS = {}
for _num, _noms in enumerate((
    ("jan", "janv", "janvier"),
    ("fev", "fevr", "fevrier"),
    ("mar", "mars"),
    ("avr", "avril"),
    ("mai",),
    ("juin",),
    ("juil", "juillet"),
    ("aou", "aout"),
    ("sep", "sept", "septembre"),
    ("oct", "octobre"),
    ("nov", "novembre"),
    ("dec", "decembre"),
), start=1):
    for _n in _noms:
        _MOIS[_n] = _num

ANNEE_MIN, ANNEE_MAX = 2000, 2100

_RE_NUM = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
_RE_TXT = re.compile(r"(?<!\d)(\d{1,2})\s+([a-z]{3,9})\s+(\d{4})(?!\d)")
# Heure juste apres la date : separee par des espaces, un retour a la ligne
# (ex. "Jeu 07 Jan 2027" puis 11:00 a la ligne), une virgule, un tiret ou un "a" (de "a 10:00").
_RE_HEURE = re.compile(
    r"[\s,;\-]*(?:a\s+)?(?<!\d)(\d{1,2})\s*[:h]\s*(\d{2})(?::(\d{2}))?(?!\d)")


def _sans_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def parse_date(txt):
    """Date du portail -> datetime (naif), ou None si illisible.

    Formes acceptees :
        "27/10/2026"  "27/10/2026 10:00"            (ancien format)
        "Jeu 07 Jan 2027"                           (liste AO)
        "29 nov. 2026"  "29 nov. 2026 a 10:00"      (carte BDC)
        "Jeu 23 Juil 2026 11:00"                    (fiche de detail)
    Insensible a la casse, aux accents et aux points. L'heure est optionnelle
    (minuit par defaut).

    Une date impossible (31 fevrier, mois inconnu, annee hors de 2000-2100,
    heure 25:99) rend None : on n'invente jamais une date."""
    t = _sans_accents(str(txt or "")).lower().replace(".", " ")
    candidats = []

    m = _RE_NUM.search(t)
    if m:
        candidats.append((m.start(), m.end(),
                          int(m.group(3)), int(m.group(2)), int(m.group(1))))

    for m in _RE_TXT.finditer(t):
        mois = _MOIS.get(m.group(2))
        if mois:
            candidats.append((m.start(), m.end(),
                              int(m.group(3)), mois, int(m.group(1))))
            break

    if not candidats:
        return None
    _, fin, annee, mois, jour = min(candidats)

    if not (ANNEE_MIN <= annee <= ANNEE_MAX):
        return None

    heure = minute = seconde = 0
    h = _RE_HEURE.match(t[fin:fin + 16])
    if h:
        heure, minute, seconde = int(h.group(1)), int(h.group(2)), int(h.group(3) or 0)

    try:
        return datetime(annee, mois, jour, heure, minute, seconde)
    except ValueError:
        return None


# =========================================================
#                 IDENTIFIANTS STABLES
# =========================================================
def _repli_md5(texte, scanner):
    log(f"   ⚠️ [{scanner}] identifiant du portail introuvable, "
        f"repli sur le md5 du texte")
    return hashlib.md5((texte or "").encode("utf-8")).hexdigest()


def id_bdc(href, texte="", scanner="BDC"):
    """'/bdc/entreprise/consultation/show/398429' -> 'bdc-398429'."""
    chemin = urlsplit(href or "").path
    m = re.search(r"(\d+)/?$", chemin)
    if m:
        return f"bdc-{m.group(1)}"
    return _repli_md5(texte, scanner)


def ref_consultation(url):
    """Parametre refConsultation d'un lien de detail, ou ''."""
    try:
        q = parse_qs(urlsplit(url or "").query)
    except Exception:
        return ""
    v = (q.get("refConsultation") or [""])[0].strip()
    return v if v.isdigit() else ""


def id_ao(url, texte="", scanner="AO"):
    """Lien de detail -> 'ao-1053059' (le meme pour les scanners AO et hicham :
    c'est la meme consultation)."""
    ref = ref_consultation(url)
    if ref:
        return f"ao-{ref}"
    return _repli_md5(texte, scanner)


# =========================================================
#              CARTES AO : "div.item.tender_item"
# =========================================================
CARTES_AO = "div.item.tender_item"

# Une seule evaluation pour toute la page (jusqu'a 500 cartes) plutot qu'une
# dizaine d'allers-retours par carte.
_CARTES_AO_JS = """
() => {
  const T = e => e ? (e.textContent || '').replace(/\\s+/g, ' ').trim() : '';
  // Texte d'un element sans ses <strong> (les libelles "Objet :", "Acheteur public :")
  const sansLibelle = e => {
    if (!e) return '';
    const c = e.cloneNode(true);
    c.querySelectorAll('strong').forEach(s => s.remove());
    return T(c);
  };
  // Lieu d'execution. Le texte visible est tronque quand il y a plusieurs
  // lieux ("RABAT / OUED ED DAHAB / ..."), la liste complete est dans
  // l'infobulle. On la prend si elle existe, sinon le texte visible sans le
  // libelle, la bulle ni le "...".
  const lieuDe = panel => {
    if (!panel) return '';
    const plein = T(panel.querySelector('.bloc-info-bulle .info-bulle'));
    if (plein) return plein;
    const blocs = panel.querySelectorAll('.limita');
    const c = (blocs.length ? blocs[blocs.length - 1] : panel).cloneNode(true);
    c.querySelectorAll(".bloc-info-bulle, .info-bulle, [id$='panelBlocCodesNuts'], i")
      .forEach(x => x.remove());
    c.querySelectorAll('br').forEach(b => b.replaceWith(', '));
    return T(c).replace(/(,\\s*){2,}/g, ', ').replace(/[,\\s]+$/, '');
  };
  return [...document.querySelectorAll('div.item.tender_item')]
    .filter(c => c.getClientRects().length > 0)
    .map(card => {
      const q = s => card.querySelector(s);
      // Le lien n'est pas un <a> : c'est un onclick sur div.contentColumn.
      const cc = q('div.contentColumn[onclick]')
              || card.closest('div.contentColumn[onclick]')
              || q("[onclick*='location.href']");
      return {
        texte: (card.innerText || '').trim(),
        ref: T(q("[id$='_referencem']")),
        objet: sansLibelle(q('.p-objet')),
        estimation: T(q("[id$='_estimationLabel']")),
        acheteur: sansLibelle(q('.title.p-card')),
        // Blocs .limita dans l'ordre du portail : [0] libelle "Date limite de
        // remise des plis", [1] date ET heure sur deux lignes, [2] libelle
        // "Lieu d'execution"... innerText garde le retour a la ligne.
        limita: [...card.querySelectorAll('.leftColumn .limita')]
                  .map(e => (e.innerText || '').trim()),
        lieu: lieuDe(q("[id$='_panelBlocLieuxExec']")),
        procedure: T(q('.verticalText')),
        onclick: cc ? (cc.getAttribute('onclick') || '') : '',
      };
    });
}
"""

_RE_ONCLICK = re.compile(r"""location\.href\s*=\s*(["'])(.+?)\1""", re.S)


def _nettoyer(valeur, libelle=None):
    """Retire d'eventuels ':' de tete et, si le <strong> n'a pas ete retire,
    le libelle lui-meme ('Objet :')."""
    v = re.sub(r"\s+", " ", valeur or "").strip()
    if libelle:
        v = re.sub(r"^\s*" + libelle + r"\s*:?\s*", "", v, flags=re.I)
    return re.sub(r"^[\s:]+", "", v).strip()


def lien_onclick(onclick):
    """'location.href="https://...&orgAcronyme=i1t&echanges";' -> URL absolue."""
    m = _RE_ONCLICK.search(onclick or "")
    return urljoin(PORTAIL, m.group(2).strip()) if m else ""


def normaliser_carte_ao(brut, scanner="AO"):
    """Brut (retour du JS) -> dict propre. Pure : testable sans navigateur."""
    # Le bloc de la date est celui qui contient une date lisible (le portail
    # le place en [1], apres le libelle). Ses deux lignes, "Jeu 07 Jan 2027"
    # et "11:00", sont recollees avec une espace.
    deadline = ""
    for bloc in brut.get("limita") or []:
        if parse_date(bloc):
            deadline = " ".join(bloc.split())
            break
    lien = lien_onclick(brut.get("onclick"))
    return {
        "ref": _nettoyer(brut.get("ref")),
        "objet": _nettoyer(brut.get("objet"), "objet"),
        "estimation": _nettoyer(brut.get("estimation")),
        "acheteur": _nettoyer(brut.get("acheteur"), r"acheteur\s+public"),
        "deadline": deadline,
        "lieu": _nettoyer(brut.get("lieu"), r"lieux?(?:\s+d['’]ex[ée]cution)?"),
        "procedure": _nettoyer(brut.get("procedure")),
        "lien": lien,
        "texte": brut.get("texte") or "",
        "offer_id": id_ao(lien, brut.get("texte"), scanner),
    }


def lire_total(page):
    """Nombre d'offres annonce par le portail (0 si illisible)."""
    try:
        txt = page.locator("#ctl0_CONTENU_PAGE_resultSearch_nombreElement").inner_text()
        return int(txt.strip())
    except Exception:
        return 0


def lire_cartes_ao(page, scanner="AO"):
    """Toutes les cartes visibles de la page de resultats, normalisees."""
    return [normaliser_carte_ao(b, scanner) for b in page.evaluate(_CARTES_AO_JS)]
