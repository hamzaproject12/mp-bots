"""
Tests de la migration du portail (octobre 2026) : listes en cartes, dates en
lettres, identifiants stables, mode amorcage, sentinelle.

Les pages sont chargees dans un vrai Chromium via file:// (Playwright), a
partir de CAPTURES REELLES du portail (tests/fixtures/reel_*.html) :

    reel_bdc_liste.html     50 cartes bdc-card, compteur 413
    reel_ao_travaux.html    10 cartes tender_item, 1806 annoncees
    reel_ao_services.html   10 cartes tender_item, 1686 annoncees
    reel_fiche_detail.html  fiche de detail, non allotie
    rupture.html            page ou le portail annonce des offres sans carte

    PYTHONUTF8=1 python -m unittest discover -s tests -t .
"""
import os
import re
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import config_site
import notifier
import store
import portail
import sentinelle
import scanner_bdc
import scanner_hicham
import main
from tests.test_site import FICHE

FIXTURES = Path(__file__).parent / "fixtures"


def uri(nom):
    """URI file:// d'une fixture (nom) ou d'un fichier temporaire (Path)."""
    chemin = nom if isinstance(nom, Path) else FIXTURES / nom
    return chemin.resolve().as_uri()


def lire(nom):
    return (FIXTURES / nom).read_text(encoding="utf-8")


def articles_bdc():
    """Les blocs <article> de la capture reelle de la liste BDC."""
    return re.findall(r"<article.*?</article>", lire("reel_bdc_liste.html"), re.S)


def bloc_bdc(ref):
    """Le bloc <article> de la capture reelle dont la reference est `ref`."""
    for a in articles_bdc():
        if f">{ref}<" in a:
            return a
    raise AssertionError(f"carte {ref} absente de la capture")


class _Navigateur(unittest.TestCase):
    """Un Chromium par classe de tests ; saute les tests s'il est indisponible."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as e:                                  # pragma: no cover
            raise unittest.SkipTest(f"Chromium indisponible : {str(e)[:80]}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls._browser.close()
            cls._pw.stop()
        except Exception:
            pass

    def setUp(self):
        self.page = self._browser.new_page()

    def tearDown(self):
        self.page.close()


class _ContexteFixture:
    """Faux BrowserContext : la 1re navigation tombe sur la fixture choisie,
    les suivantes (pages 2, 3... d'une liste de 413 offres) sur une page vide."""

    def __init__(self, browser, nom):
        self.browser, self.nom = browser, nom

    def new_page(self):
        page = self.browser.new_page()
        vraie = page.goto
        appels = []

        def goto(url, **kw):
            appels.append(url)
            return vraie(uri(self.nom) if len(appels) == 1 else "about:blank")
        page.goto = goto
        return page


# =========================================================
#  1. Dates
# =========================================================
class TestParseDate(unittest.TestCase):

    def test_les_quatre_formes(self):
        cas = {
            "Jeu 07 Jan 2027": datetime(2027, 1, 7),
            "29 nov. 2026": datetime(2026, 11, 29),
            "Jeu 23 Juil 2026 11:00": datetime(2026, 7, 23, 11, 0),
            "27/10/2026": datetime(2026, 10, 27),
            "27/10/2026 10:00": datetime(2026, 10, 27, 10, 0),
            "29 nov. 2026 à 10:00": datetime(2026, 11, 29, 10, 0),
        }
        for txt, attendu in cas.items():
            self.assertEqual(portail.parse_date(txt), attendu, txt)

    def test_accents_points_et_casse(self):
        self.assertEqual(portail.parse_date("12 AOÛT 2026"), datetime(2026, 8, 12))
        self.assertEqual(portail.parse_date("8 Déc. 2026"), datetime(2026, 12, 8))
        self.assertEqual(portail.parse_date("3 FÉV 2027"), datetime(2027, 2, 3))
        self.assertEqual(portail.parse_date("Mar 03 Mars 2027"), datetime(2027, 3, 3))
        self.assertEqual(portail.parse_date("23 JUIL. 2026 11h30"),
                         datetime(2026, 7, 23, 11, 30))

    def test_forme_exacte_du_bloc_limita(self):
        """Le bloc .limita[1] du portail : date ET heure separees par un \\n."""
        self.assertEqual(portail.parse_date("Jeu 07 Jan 2027\n11:00"),
                         datetime(2027, 1, 7, 11, 0))
        # Le jour de la semaine peut etre "Mar" : ce n'est pas le mois.
        self.assertEqual(portail.parse_date("Mar 29 Dec 2026\n09:30"),
                         datetime(2026, 12, 29, 9, 30))

    def test_heure_separee_par_un_retour_a_la_ligne(self):
        for txt in ("Jeu 07 Jan 2027\n11:00", "Jeu 07 Jan 2027 11:00",
                    "Jeu 07 Jan 2027 \n  11:00", "29 nov. 2026\nà 10:00"):
            d = portail.parse_date(txt)
            self.assertNotEqual((d.hour, d.minute), (0, 0), repr(txt))

    def test_juin_et_juillet_ne_se_confondent_pas(self):
        self.assertEqual(portail.parse_date("5 juin 2026").month, 6)
        self.assertEqual(portail.parse_date("5 juil 2026").month, 7)
        self.assertIsNone(portail.parse_date("5 jui 2026"))     # ambigu : refuse

    def test_les_douze_mois(self):
        mois = "jan fev mar avr mai juin juil aou sep oct nov dec".split()
        for i, m in enumerate(mois, start=1):
            self.assertEqual(portail.parse_date(f"10 {m} 2026").month, i, m)

    def test_date_absurde_ou_illisible_rend_none(self):
        for txt in ("32/13/2026", "31 fév 2026", "29/02/2027", "12 jan 1999",
                    "12 jan 2300", "25 Foo 2026", "25 déc. 2026 à 25:99",
                    "bientôt", "", None, "Procedure ouverte",
                    "Date limite de remise des plis"):
            self.assertIsNone(portail.parse_date(txt), repr(txt))

    def test_les_dates_de_hicham_utilisent_la_meme_fonction(self):
        self.assertEqual(scanner_hicham._iso_limite("Jeu 23 Juil 2026 11:00"),
                         "2026-07-23T11:00:00+01:00")
        self.assertEqual(scanner_hicham._iso_limite("27/10/2026 10:00"),
                         "2026-10-27T10:00:00+01:00")
        self.assertEqual(scanner_hicham._date_iso("Jeu 07 Jan 2027"), "2027-01-07")
        self.assertIsNone(scanner_hicham._iso_limite("31 fév 2026"))
        self.assertIsNone(scanner_hicham._date_iso("n'importe quoi"))

    def test_payload_site_lit_la_date_en_lettres(self):
        fiche = dict(FICHE, date_limite="Jeu 23 Juil 2026 11:00", deadline="")
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertEqual(p["date_limite"], "2026-07-23T11:00:00+01:00")

    def test_payload_site_se_rabat_sur_la_date_de_la_liste(self):
        """Fiche muette ou illisible : la date de la carte sauve l'envoi."""
        fiche = dict(FICHE, date_limite="", deadline="Jeu 07 Jan 2027 11:00")
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertEqual(p["date_limite"], "2027-01-07T11:00:00+01:00")
        fiche = dict(FICHE, date_limite="???", deadline="Jeu 07 Jan 2027 11:00")
        self.assertEqual(scanner_hicham.construire_payload_site(fiche)["date_limite"],
                         "2027-01-07T11:00:00+01:00")

    def test_payload_site_sans_date_valide_pas_d_envoi(self):
        fiche = dict(FICHE, date_limite="31 fév 2026", deadline="")
        self.assertIsNone(scanner_hicham.construire_payload_site(fiche))


# =========================================================
#  2. Identifiants stables
# =========================================================
class TestIdentifiants(unittest.TestCase):

    def test_bdc(self):
        self.assertEqual(portail.id_bdc("/bdc/entreprise/consultation/show/398429"),
                         "bdc-398429")
        self.assertEqual(portail.id_bdc(
            "https://www.marchespublics.gov.ma/bdc/entreprise/consultation/show/398429?x=1"),
            "bdc-398429")

    def test_ao(self):
        url = ("https://www.marchespublics.gov.ma/?page=entreprise."
               "EntrepriseDetailsConsultation&refConsultation=1053059"
               "&orgAcronyme=i1t&echanges")
        self.assertEqual(portail.id_ao(url), "ao-1053059")

    def test_repli_md5_journalise(self):
        sorties = []
        ancien = portail.log
        portail.log = sorties.append
        try:
            a = portail.id_ao("https://x/?page=y", "texte de la carte", "AO")
            b = portail.id_ao("https://x/?page=y", "texte de la carte", "AO")
            c = portail.id_bdc("/bdc/liste", "autre texte", "BDC")
        finally:
            portail.log = ancien
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)                  # md5
        self.assertFalse(a.startswith("ao-"))
        self.assertEqual(len(c), 32)
        self.assertEqual(len(sorties), 3)
        self.assertTrue(all("repli" in s for s in sorties))


# =========================================================
#  3. Liste BDC (capture reelle)
# =========================================================
class TestCartesBdc(_Navigateur):

    def test_compteur_et_nombre_de_cartes(self):
        self.page.goto(uri("reel_bdc_liste.html"))
        self.assertIn("413", self.page.locator(".content__resultat").inner_text())
        cartes = scanner_bdc.lire_cartes(self.page)
        self.assertEqual(len(cartes), 50)
        for c in cartes:                       # rien de vide dans la capture
            self.assertTrue(c["ref"] and c["objet"] and c["acheteur"] and c["lieu"]
                            and c["date_limite"], c["href"])
            self.assertTrue(c["lien"].startswith(
                "https://www.marchespublics.gov.ma/bdc/entreprise/consultation/show/"))
        self.assertEqual(len({c["href"] for c in cartes}), 50)

    def test_premiere_carte_reelle(self):
        self.page.goto(uri("reel_bdc_liste.html"))
        c = scanner_bdc.lire_cartes(self.page)[0]
        self.assertEqual(c["ref"], "05/INV/2026")
        self.assertTrue(c["objet"].startswith("Etudes topographiques des"))
        self.assertEqual(c["lien"], "https://www.marchespublics.gov.ma"
                                    "/bdc/entreprise/consultation/show/398429")
        self.assertEqual(c["date_limite"], "29 nov. 2026 à 10:00")
        self.assertEqual(c["lieu"], "MAROC, TANGER-ASSILAH")
        self.assertTrue(c["acheteur"].startswith("DIRECTION PROVINCIALE DE L"))
        self.assertTrue(c["acheteur"].endswith("TANGER ASSILA"))
        self.assertEqual(portail.id_bdc(c["href"]), "bdc-398429")

    def test_badge_annule_detecte(self):
        self.page.goto(uri("reel_bdc_liste.html"))
        cartes = scanner_bdc.lire_cartes(self.page)
        annulees = [portail.id_bdc(c["href"]) for c in cartes if c["annule"]]
        self.assertEqual(annulees, ["bdc-398429", "bdc-398124"])
        self.assertEqual(cartes[0]["statut"], "Annulé")
        self.assertEqual(cartes[1]["statut"], "")


class _ScanBdc(_Navigateur):
    """run() complet : un dossier de donnees jetable, un client BDC, un admin."""

    def setUp(self):
        super().setUp()
        self.dossier = tempfile.mkdtemp(prefix="mpbots_portail_")
        self._sauv = (config.DATA_PATH, config.SUBSCRIBERS, config.ADMIN_TELEGRAM,
                      scanner_bdc.ATTENTE_CARTES_MS, notifier.send_telegram)
        config.DATA_PATH = self.dossier
        config.SUBSCRIBERS = [{"name": "Testeur", "telegram": "111", "whatsapp": None,
                               "bdc": True, "ao": False, "subscriptions": ["ALL"]}]
        config.ADMIN_TELEGRAM = "999"
        scanner_bdc.ATTENTE_CARTES_MS = 300
        self.envois = []
        notifier.send_telegram = lambda chat, txt: self.envois.append((chat, txt)) or True
        store._cache.clear()

    def tearDown(self):
        (config.DATA_PATH, config.SUBSCRIBERS, config.ADMIN_TELEGRAM,
         scanner_bdc.ATTENTE_CARTES_MS, notifier.send_telegram) = self._sauv
        store._cache.clear()
        shutil.rmtree(self.dossier, ignore_errors=True)
        super().tearDown()

    def _scan(self, fixture):
        return scanner_bdc.run(_ContexteFixture(self._browser, fixture))

    def _page_bdc(self, article, nb=1):
        """Ecrit une page BDC jetable : le compteur + les <article> donnes."""
        chemin = Path(self.dossier) / f"page{abs(hash(article))}.html"
        chemin.write_text(
            '<!doctype html><html><head><meta charset="utf-8"></head><body>'
            f'<div class="content__resultat">Nombre de résultats : {nb}</div>'
            f"{article}</body></html>", encoding="utf-8")
        return chemin


class TestScanBdcReel(_ScanBdc):

    def test_liste_reelle_retenues_et_identifiants_stables(self):
        ok, alerts = self._scan("reel_bdc_liste.html")
        self.assertTrue(ok)
        ids = [a["id"] for a in alerts]
        self.assertEqual(sorted(ids), ["bdc-397695", "bdc-399861",
                                       "bdc-400712", "bdc-401729"])
        self.assertTrue(all(re.fullmatch(r"bdc-\d+", i) for i in ids))
        self.assertEqual(self.envois, [])            # pas de rupture, pas de message

    def test_le_message_affiche_l_acheteur(self):
        _, alerts = self._scan("reel_bdc_liste.html")
        a = next(x for x in alerts if x["id"] == "bdc-397695")
        self.assertIn("OFFICE NATIONAL DE SECURITE SANITAIRE DES PRODUITS "
                      "ALIMENTAIRES - DIRECTION REGIONALE DE LA REGION DAKHLA "
                      "OUED EDDAHAB", a["msg"])
        self.assertIn("0022-2026", a["msg"])
        self.assertIn("09 nov. 2026 à 11:00", a["msg"])
        self.assertIn("https://www.marchespublics.gov.ma/bdc/entreprise/"
                      "consultation/show/397695", a["msg"])

    def test_carte_annulee_ignoree(self):
        """Meme carte, meme score : seul le badge 'Annulé' change l'issue."""
        normale = bloc_bdc("0022-2026")
        _, alerts = self._scan(self._page_bdc(normale))
        self.assertEqual([a["id"] for a in alerts], ["bdc-397695"])

        annulee = normale.replace(
            '<span class="bdc-card__ref">0022-2026</span>',
            '<span class="bdc-card__ref">0022-2026</span>'
            '<span class="badge bg-danger">Annulé</span>')
        self.assertNotEqual(normale, annulee)
        store._cache.clear()
        _, alerts = self._scan(self._page_bdc(annulee))
        self.assertEqual(alerts, [])

    def test_rupture_ok_true_et_alerte_l_admin_et_lui_seul(self):
        ok, alerts = self._scan("rupture.html")
        self.assertTrue(ok)             # une rupture n'est pas un echec de passage
        self.assertEqual(alerts, [])
        self.assertEqual(len(self.envois), 1)
        chat, texte = self.envois[0]
        self.assertEqual(chat, "999")                   # l'admin, jamais "111"
        self.assertIn("bdc", texte)
        self.assertIn("40", texte)
        self.assertIn("0 extraites", texte)
        self.assertIn("selecteurs sont a revoir", texte)


class TestAcheteurNonScore(_ScanBdc):
    """L'acheteur s'affiche mais n'entre jamais dans le texte score."""

    ACHETEUR_AGRI = "DIRECTION PROVINCIALE DE L'AGRICULTURE DE TEST"

    def _carte(self, objet, acheteur):
        a = bloc_bdc("0022-2026")
        a = re.sub(r'(class="bdc-card__title">).*?(</a>)',
                   lambda m: m.group(1) + objet + m.group(2), a, flags=re.S)
        a = re.sub(r'(<i class="fa-solid fa-building"></i>).*?(</span>)',
                   lambda m: m.group(1) + acheteur + m.group(2), a, count=1,
                   flags=re.S)
        # lieu neutre : aucun mot-cle, aucune zone prioritaire
        a = re.sub(r'(<i class="fa-solid fa-location-dot"></i>).*?(</span>)',
                   lambda m: m.group(1) + "MAROC, RABAT" + m.group(2), a, count=1,
                   flags=re.S)
        return a

    def test_seul_l_acheteur_contient_agri_pas_retenue(self):
        """'Formation' = 1 point (seuil Event & Formation : 2). Si l'acheteur
        etait score, 'agri' ferait 2 et l'offre passerait."""
        carte = self._carte("Formation du personnel", self.ACHETEUR_AGRI)
        c = scanner_bdc.normaliser_carte({
            "ref": "X/1", "objet": "Formation du personnel", "href": "/s/1",
            "lieu": "MAROC, RABAT", "acheteur": self.ACHETEUR_AGRI, "badges": []})
        score, cat, _ = scanner_bdc.scorer(scanner_bdc.texte_score(c).lower())
        self.assertFalse(scanner_bdc.passe_le_seuil(score, cat), (score, cat))

        _, alerts = self._scan(self._page_bdc(carte))
        self.assertEqual(alerts, [])

    def test_temoin_acheteur_neutre_meme_resultat(self):
        carte = self._carte("Formation du personnel", "COMMUNE DE TEST")
        _, alerts = self._scan(self._page_bdc(carte))
        self.assertEqual(alerts, [])

    def test_offre_qui_merite_d_etre_retenue_affiche_l_acheteur(self):
        carte = self._carte("Formation et organisation d'ateliers", self.ACHETEUR_AGRI)
        _, alerts = self._scan(self._page_bdc(carte))
        self.assertEqual(len(alerts), 1)
        self.assertIn(self.ACHETEUR_AGRI, alerts[0]["msg"])
        # Retenue grace a formation + organisation (+ atelier), pas a 'agri'.
        self.assertNotIn("🚜", alerts[0]["msg"])


# =========================================================
#  4. Listes AO (captures reelles, partagees par scanner_ao et scanner_hicham)
# =========================================================
class TestCartesAo(_Navigateur):

    def _cartes(self, nom):
        self.page.goto(uri(nom))
        return portail.lire_cartes_ao(self.page)

    def test_travaux_compteur_et_nombre_de_cartes(self):
        cartes = self._cartes("reel_ao_travaux.html")
        self.assertEqual(portail.lire_total(self.page), 1806)
        self.assertEqual(len(cartes), 10)
        self.assertEqual(len({c["offer_id"] for c in cartes}), 10)

    def test_services_compteur_et_nombre_de_cartes(self):
        cartes = self._cartes("reel_ao_services.html")
        self.assertEqual(portail.lire_total(self.page), 1686)
        self.assertEqual(len(cartes), 10)

    def test_premiere_carte_travaux(self):
        c = self._cartes("reel_ao_travaux.html")[0]
        self.assertEqual(c["ref"], "01/2027")
        # Le libelle "Objet :" part, le reste du texte est intact.
        self.assertTrue(c["objet"].startswith("Travaux d"))
        self.assertIn("kariat ben aouda (province de kenitra)", c["objet"])
        self.assertNotIn("Objet", c["objet"])
        self.assertEqual(c["estimation"], "1 386 563,64")
        self.assertTrue(c["acheteur"].startswith("Collectivit"))
        self.assertTrue(c["acheteur"].endswith("territoriale de KARIAT BEN AOUDA"))
        self.assertNotIn("Acheteur", c["acheteur"])
        self.assertEqual(c["procedure"], "AOO")
        self.assertEqual(c["lieu"], "KENITRA")

        q = parse_qs(urlsplit(c["lien"]).query)
        self.assertEqual(q["page"], ["entreprise.EntrepriseDetailsConsultation"])
        self.assertEqual(q["refConsultation"], ["1053059"])
        self.assertEqual(q["orgAcronyme"], ["i1t"])
        self.assertTrue(c["lien"].startswith("https://www.marchespublics.gov.ma/"))
        self.assertEqual(c["offer_id"], "ao-1053059")

    def test_date_limite_recollee(self):
        """Le bloc .limita[1] donne 'Jeu 07 Jan 2027\\n11:00' : on recolle."""
        c = self._cartes("reel_ao_travaux.html")[0]
        self.assertEqual(c["deadline"], "Jeu 07 Jan 2027 11:00")
        self.assertEqual(portail.parse_date(c["deadline"]), datetime(2027, 1, 7, 11, 0))

    def test_toutes_les_dates_limites_sont_lisibles_avec_leur_heure(self):
        for nom in ("reel_ao_travaux.html", "reel_ao_services.html"):
            for c in self._cartes(nom):
                d = portail.parse_date(c["deadline"])
                self.assertIsNotNone(d, c["deadline"])
                self.assertNotIn("Date limite", c["deadline"])
                self.assertIn(":", c["deadline"])           # l'heure est la
                self.assertGreaterEqual(d.year, 2026)

    def test_l_acronyme_d_organisme_varie_et_n_est_pas_code_en_dur(self):
        cartes = self._cartes("reel_ao_travaux.html")
        orgs = [parse_qs(urlsplit(c["lien"]).query)["orgAcronyme"][0] for c in cartes]
        self.assertEqual(orgs[0], "i1t")
        self.assertEqual(orgs[1], "g8e")
        self.assertGreater(len(set(orgs)), 1)
        self.assertEqual(cartes[1]["offer_id"], "ao-1043336")
        self.assertEqual(cartes[1]["ref"], "239/2026")

    def test_lieu_complet_sans_les_points_de_suspension(self):
        for nom in ("reel_ao_travaux.html", "reel_ao_services.html"):
            for c in self._cartes(nom):
                self.assertTrue(c["lieu"], nom)
                self.assertNotIn("...", c["lieu"])
                self.assertNotIn("Lieu d", c["lieu"])
        # Plusieurs lieux : la liste complete de l'infobulle, pas le texte tronque.
        c = self._cartes("reel_ao_services.html")[0]
        self.assertEqual(c["lieu"], "RABAT, OUED ED DAHAB, AOUSSERD, DAKHLA")
        self.assertEqual(c["offer_id"], "ao-1027574")
        self.assertEqual(c["ref"], "8/2026/DPDPM")
        self.assertEqual(c["estimation"], "3 000 000,00")

    def test_estimation_vide_reste_vide(self):
        cartes = self._cartes("reel_ao_services.html")
        self.assertEqual(sum(1 for c in cartes if not c["estimation"]), 1)

    def test_le_libelle_acheteur_part_et_le_nom_reste_celui_de_la_liste_cible(self):
        """Le nom lu doit etre identique a l'ancien : la liste d'acheteurs
        cibles de hicham le compare apres normalisation."""
        cartes = self._cartes("reel_ao_services.html")
        cibles = [c["acheteur"] for c in cartes if scanner_hicham.acheteur_cible(c["acheteur"])]
        self.assertEqual(cibles, ["DIRECTEUR PROVINCIAL D'AGRICULTURE DE TANGER"])

    def test_normalisation_du_bloc_limita(self):
        """Forme exacte du portail : [libelle, date\\nheure, libelle]."""
        brut = {"ref": "01/2027", "objet": "Objet : x", "limita": [
            "Date limite de remise des plis",
            "Jeu 07 Jan 2027\n11:00",
            "Lieu d'exécution"],
            "onclick": 'location.href="https://www.marchespublics.gov.ma/?page='
                       'entreprise.EntrepriseDetailsConsultation&refConsultation='
                       '1053059&orgAcronyme=i1t&echanges";'}
        c = portail.normaliser_carte_ao(brut)
        self.assertEqual(c["deadline"], "Jeu 07 Jan 2027 11:00")
        self.assertEqual(portail.parse_date(c["deadline"]), datetime(2027, 1, 7, 11, 0))
        self.assertEqual(c["offer_id"], "ao-1053059")
        self.assertEqual(c["objet"], "x")

    def test_bloc_sans_date_donne_une_echeance_vide(self):
        c = portail.normaliser_carte_ao({"ref": "1", "limita": ["Date limite de remise des plis"]})
        self.assertEqual(c["deadline"], "")

    def test_page_sans_carte(self):
        self.page.goto(uri("rupture.html"))
        self.assertEqual(portail.lire_cartes_ao(self.page), [])
        self.assertEqual(portail.lire_total(self.page), 40)

    def test_scanner_ao_et_hicham_partagent_le_meme_selecteur(self):
        import scanner_ao
        self.assertEqual(scanner_ao.ROWS_SELECTOR, "div.item.tender_item")
        self.assertEqual(scanner_hicham.ROWS_SELECTOR, "div.item.tender_item")


# =========================================================
#  5. Fiche de detail reelle (lire_fiche n'a pas change)
# =========================================================
class TestFicheReelle(_Navigateur):

    def test_lire_fiche_sur_la_capture_reelle(self):
        fiche = scanner_hicham.lire_fiche(
            _ContexteFixture(self._browser, "reel_fiche_detail.html"),
            "https://www.marchespublics.gov.ma/?page=x&refConsultation=1")
        self.assertEqual(fiche["estimation"], "1 997 184,00")
        self.assertEqual(fiche["caution"], "39 000,00 MAD")
        self.assertEqual(fiche["classe"], "4")
        self.assertEqual(fiche["date_limite"], "Jeu 23 Juil 2026 11:00")
        self.assertEqual(fiche["lots"], [])
        self.assertIn("DORMVAO", fiche["entite_achat"])
        self.assertTrue(fiche["objet_complet"].startswith("Travaux d"))
        self.assertEqual(portail.parse_date(fiche["date_limite"]),
                         datetime(2026, 7, 23, 11, 0))

    def test_payload_du_site_avec_la_fiche_reelle(self):
        fiche = scanner_hicham.lire_fiche(
            _ContexteFixture(self._browser, "reel_fiche_detail.html"), "https://x/")
        carte = dict(
            id="ao-1053059", buyer="X", ref="01/2027", objet="objet de la liste",
            deadline="Jeu 07 Jan 2027 11:00", concepts=["Travaux"],
            link=("https://www.marchespublics.gov.ma/?page=entreprise."
                  "EntrepriseDetailsConsultation&refConsultation=1053059"
                  "&orgAcronyme=i1t&echanges"))
        carte.update(estimation=fiche["estimation"], caution=fiche["caution"],
                     quals=fiche["quals"], qualif=fiche["qualif"],
                     classe=fiche["classe"], lots=[],
                     date_limite=fiche["date_limite"],
                     entite_achat=fiche["entite_achat"],
                     objet_complet=fiche["objet_complet"])
        p = scanner_hicham.construire_payload_site(carte)
        self.assertEqual(p["ref_annonce"], "1053059")
        self.assertEqual(p["date_limite"], "2026-07-23T11:00:00+01:00")
        self.assertEqual(p["org_acronyme"], "DORMVAO")
        self.assertEqual(p["org_portail"], "i1t")
        self.assertEqual(p["estimation"], 1997184.0)

    def test_estimation_de_la_liste_en_repli(self):
        """Fiche muette : l'estimation de la carte prend le relais."""
        d = dict(FICHE, estimation="", lots=[])
        # Meme operation que scanner_hicham.run() apres lire_fiche().
        detail = {}
        r = {"estimation_liste": "1 386 563,64"}
        d["estimation"] = detail.get("estimation", "") or r.get("estimation_liste", "")
        msg, wa = scanner_hicham.construire_messages(d)
        self.assertIn("1 386 563,64", msg)


# =========================================================
#  6. Sentinelle
# =========================================================
class TestSentinelle(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp(prefix="mpbots_sentinelle_")
        self._sauv = (config.DATA_PATH, config.ADMIN_TELEGRAM, config.SENTINELLE_SILENCE_H,
                      notifier.send_telegram, notifier.send_whatsapp, notifier.broadcast,
                      sentinelle.log, sentinelle._maintenant)
        config.DATA_PATH = self.dossier
        config.ADMIN_TELEGRAM = "999"
        config.SENTINELLE_SILENCE_H = 6
        self.envois = []
        self.reussit = True
        notifier.send_telegram = lambda chat, txt: (
            self.envois.append((chat, txt)) or self.reussit)

        def interdit(*a, **k):
            raise AssertionError("la sentinelle ne doit jamais toucher les clients")
        notifier.send_whatsapp = interdit
        notifier.broadcast = interdit
        self.journal = []
        sentinelle.log = self.journal.append
        self.t = datetime(2026, 11, 2, 8, 0, 0)
        sentinelle._maintenant = lambda: self.t

    def tearDown(self):
        (config.DATA_PATH, config.ADMIN_TELEGRAM, config.SENTINELLE_SILENCE_H,
         notifier.send_telegram, notifier.send_whatsapp, notifier.broadcast,
         sentinelle.log, sentinelle._maintenant) = self._sauv
        shutil.rmtree(self.dossier, ignore_errors=True)

    def test_pas_de_rupture_si_des_offres_sont_extraites(self):
        self.assertFalse(sentinelle.verifier("ao", 413, 400))
        self.assertEqual(self.envois, [])

    def test_pas_de_rupture_si_le_portail_annonce_zero(self):
        self.assertFalse(sentinelle.verifier("ao", 0, 0))
        self.assertEqual(self.envois, [])
        self.assertEqual(self.journal, [])

    def test_rupture_journalisee_et_envoyee_a_l_admin(self):
        self.assertTrue(sentinelle.verifier("bdc", 413, 0))
        self.assertIn("🚨 [bdc] RUPTURE : 413 annoncees, 0 extraites — le portail a change",
                      self.journal)
        self.assertEqual(len(self.envois), 1)
        chat, texte = self.envois[0]
        self.assertEqual(chat, "999")
        self.assertIn("scanner bdc", texte)
        self.assertIn("413 offres annoncees", texte)
        self.assertIn("0 extraites", texte)
        self.assertIn("le portail a probablement change, les selecteurs sont a revoir",
                      texte.lower())
        self.assertIn("02/11/2026 08:00", texte)

    def test_deux_ruptures_a_10_minutes_un_seul_envoi(self):
        self.assertTrue(sentinelle.verifier("bdc", 413, 0))
        self.t += timedelta(minutes=10)
        self.assertTrue(sentinelle.verifier("bdc", 413, 0))   # la rupture est toujours journalisee
        self.assertEqual(len(self.envois), 1)

    def test_la_memoire_survit_a_un_redemarrage(self):
        """Sur disque : un nouveau processus ne renvoie pas le message."""
        sentinelle.verifier("bdc", 413, 0)
        with open(os.path.join(self.dossier, "sentinelle.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"bdc": "2026-11-02T08:00:00"})
        self.t += timedelta(minutes=10)
        sentinelle.verifier("bdc", 413, 0)                    # "autre processus"
        self.assertEqual(len(self.envois), 1)

    def test_un_nouveau_message_apres_la_fenetre_de_silence(self):
        sentinelle.verifier("bdc", 413, 0)
        self.t += timedelta(hours=5, minutes=59)
        sentinelle.verifier("bdc", 413, 0)
        self.assertEqual(len(self.envois), 1)
        self.t += timedelta(minutes=2)                        # 6 h 01
        sentinelle.verifier("bdc", 413, 0)
        self.assertEqual(len(self.envois), 2)

    def test_la_fenetre_est_reglable(self):
        config.SENTINELLE_SILENCE_H = 1
        sentinelle.verifier("ao", 10, 0)
        self.t += timedelta(minutes=61)
        sentinelle.verifier("ao", 10, 0)
        self.assertEqual(len(self.envois), 2)

    def test_le_silence_est_par_scanner(self):
        sentinelle.verifier("bdc", 10, 0)
        sentinelle.verifier("ao", 10, 0)
        sentinelle.verifier("hicham", 10, 0)
        self.assertEqual(len(self.envois), 3)

    def test_un_envoi_echoue_n_est_pas_memorise(self):
        self.reussit = False
        sentinelle.verifier("bdc", 10, 0)
        self.reussit = True
        self.t += timedelta(minutes=10)
        sentinelle.verifier("bdc", 10, 0)
        self.assertEqual(len(self.envois), 2)                 # retente

    def test_memoire_corrompue_ne_bloque_rien(self):
        with open(os.path.join(self.dossier, "sentinelle.json"), "w") as f:
            f.write("{pas du json")
        sentinelle.verifier("bdc", 10, 0)
        self.assertEqual(len(self.envois), 1)

    def test_admin_vide_aucun_envoi_mais_rupture_journalisee(self):
        config.ADMIN_TELEGRAM = ""
        self.assertTrue(sentinelle.verifier("ao", 5, 0))
        self.assertEqual(self.envois, [])
        self.assertTrue(any("RUPTURE" in s for s in self.journal))
        self.assertFalse(os.path.exists(os.path.join(self.dossier, "sentinelle.json")))


# =========================================================
#  7. Mode amorcage
# =========================================================
class _FauxScanner:
    def __init__(self, alerts, ok=True):
        self.alerts, self.ok = alerts, ok

    def run(self, context):
        return self.ok, list(self.alerts)


class _FauxPlaywright:
    class _Browser:
        def new_context(self, **kw):
            return type("Ctx", (), {"close": lambda s: None})()

        def close(self):
            pass

    def __enter__(self):
        outer = self
        return type("PW", (), {"chromium": type(
            "Chromium", (), {"launch": lambda s, **kw: outer._Browser()})()})()

    def __exit__(self, *a):
        return False


class TestAmorcage(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp(prefix="mpbots_amorcage_")
        self._sauv = (config.DATA_PATH, config.SCANNERS, main.SCANNERS,
                      main.sync_playwright, main.log, notifier.broadcast,
                      notifier.send_telegram, notifier.send_whatsapp)
        config.DATA_PATH = self.dossier
        config.SCANNERS = ["faux"]
        self.a = {"name": "Alice", "telegram": "1", "whatsapp": None}
        self.b = {"name": "Bob", "telegram": None, "whatsapp": "212600000000"}
        self.site = dict(config_site.ABONNE)
        main.SCANNERS = {"faux": _FauxScanner([
            {"id": "ao-1", "msg": "m", "wa_params": [],
             "recipients": [self.a, self.b, self.site]},
            {"id": "ao-2", "msg": "m", "wa_params": [], "recipients": [self.a]},
            # Offre qui ne concerne que le site : rien a marquer pour les clients
            {"id": "ao-3", "msg": "m", "wa_params": [], "recipients": [self.site]},
        ])}
        main.sync_playwright = lambda: _FauxPlaywright()
        self.journal = []
        main.log = self.journal.append

        def interdit(*a, **k):
            raise AssertionError("amorcage : rien ne doit etre envoye")
        notifier.broadcast = interdit
        notifier.send_telegram = interdit
        notifier.send_whatsapp = interdit
        store._cache.clear()
        store._attente.clear()

    def tearDown(self):
        (config.DATA_PATH, config.SCANNERS, main.SCANNERS, main.sync_playwright,
         main.log, notifier.broadcast, notifier.send_telegram,
         notifier.send_whatsapp) = self._sauv
        store._cache.clear()
        store._attente.clear()
        shutil.rmtree(self.dossier, ignore_errors=True)

    def test_marque_les_clients_et_n_envoie_rien(self):
        ok = main.run_once(amorcage=True)
        self.assertTrue(ok)
        self.assertTrue(store.client_a_vu("Alice", "ao-1"))
        self.assertTrue(store.client_a_vu("Alice", "ao-2"))
        self.assertTrue(store.client_a_vu("Bob", "ao-1"))
        self.assertFalse(store.client_a_vu("Bob", "ao-2"))   # pas destinataire
        self.assertIn("🌱 amorcage : 2 offres marquees pour les clients, site exclu",
                      self.journal)

    def test_le_site_n_est_jamais_amorce(self):
        main.run_once(amorcage=True)
        for oid in ("ao-1", "ao-2", "ao-3"):
            self.assertFalse(store.client_a_vu(self.site["name"], oid), oid)

    def test_le_site_rattrape_apres_l_amorcage(self):
        """Apres amorcage, les clients ont tout vu mais pas le site : tous_ont_vu
        reste faux tant que le site est destinataire, donc l'offre est reprise."""
        main.run_once(amorcage=True)
        destinataires = [self.a, self.b, self.site]
        self.assertFalse(store.tous_ont_vu(destinataires, "ao-1"))
        self.assertTrue(store.tous_ont_vu([self.a, self.b], "ao-1"))

    def test_l_historique_est_ecrit_sur_disque(self):
        main.run_once(amorcage=True)
        store._cache.clear()
        self.assertTrue(store.client_a_vu("Alice", "ao-1"))
        self.assertTrue(os.path.exists(store.chemin_client("Alice")))
        self.assertFalse(os.path.exists(store.chemin_client(self.site["name"])))

    def test_efface_une_livraison_partielle_en_attente(self):
        store.noter_livraison("Alice", "ao-1", {"telegram"}, {"telegram", "whatsapp"})
        self.assertEqual(store.canaux_livres("Alice", "ao-1"), {"telegram"})
        main.run_once(amorcage=True)
        self.assertEqual(store.canaux_livres("Alice", "ao-1"), set())

    def test_la_livraison_en_attente_du_site_est_conservee(self):
        store.noter_livraison(self.site["name"], "ao-1", set(), {"site"})
        main.run_once(amorcage=True)
        self.assertIn("ao-1", store._charger_attente(self.site["name"])["data"])

    def test_un_scanner_en_rupture_rend_ok_et_le_passage_reste_normal(self):
        """Une rupture n'est pas un echec : run() rend ok=True, le sommeil du
        mode loop reste SLEEP_OK (pas SLEEP_FAIL)."""
        main.SCANNERS = {"faux": _FauxScanner([], ok=True)}
        self.assertTrue(main.run_once(amorcage=True))

    def test_le_mode_est_reconnu(self):
        """RUN_MODE=amorcage (ou amorçage, avec cedille) donne 'amorcage'."""
        import subprocess
        racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for valeur in ("amorcage", "AMORCAGE", "amorçage"):
            env = dict(os.environ, RUN_MODE=valeur, PYTHONUTF8="1")
            out = subprocess.run([sys.executable, "-c",
                                  "import config; print(config.RUN_MODE)"],
                                 cwd=racine, env=env, capture_output=True,
                                 text=True, encoding="utf-8")
            self.assertEqual(out.stdout.strip(), "amorcage", valeur)


if __name__ == "__main__":
    unittest.main()
