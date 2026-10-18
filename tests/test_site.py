"""
Tests de la sortie SITE. Aucune I/O reseau, aucun fichier de production :
DATA_PATH est redirige vers un dossier temporaire et les fonctions d'envoi
sont remplacees par des doublures.

    python -m unittest discover -s tests -t .
"""
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import config_site
import notifier
import store
import scanner_hicham


def _site_configure(url="https://site.test/api/v1/appels_ouverts", token="jeton"):
    config_site.SITE_URL = url
    config_site.SITE_TOKEN = token


def _site_absent():
    config_site.SITE_URL = ""
    config_site.SITE_TOKEN = ""


# =========================================================
#  1. SITE_URL vide -> le canal "site" n'existe pas
# =========================================================
class TestCanalDesactive(unittest.TestCase):

    def setUp(self):
        self._sauv = (config_site.SITE_URL, config_site.SITE_TOKEN)

    def tearDown(self):
        config_site.SITE_URL, config_site.SITE_TOKEN = self._sauv

    def test_sans_url_pas_de_canal_site(self):
        _site_absent()
        self.assertFalse(config_site.actif())
        self.assertNotIn("site", notifier.canaux(config_site.ABONNE))
        self.assertNotIn("site", notifier.canaux(
            {"name": "X", "whatsapp": "212600000000", "site": True}))

    def test_token_seul_ne_suffit_pas(self):
        _site_configure(url="", token="jeton")
        self.assertNotIn("site", notifier.canaux(config_site.ABONNE))
        _site_configure(url="https://site.test/x", token="")
        self.assertNotIn("site", notifier.canaux(config_site.ABONNE))

    def test_configure_le_canal_apparait(self):
        _site_configure()
        self.assertIn("site", notifier.canaux(config_site.ABONNE))
        # Un abonne ordinaire ne gagne pas le canal pour autant.
        self.assertNotIn("site", notifier.canaux(
            {"name": "Hicham", "telegram": None, "whatsapp": "212661723762"}))


# =========================================================
#  2. Payload construit a partir d'une fiche connue
# =========================================================
FICHE = {
    "id": "abc123",
    "ref": "12/2026/DPA",
    "buyer": "DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI",
    "objet": "Travaux d'amenagement de pistes agricoles dans la province...",
    "objet_complet": "Travaux d'amenagement de pistes agricoles dans la "
                     "province de Safi, lot unique, exercice 2026",
    "entite_achat": "M4 / DPAG - DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI",
    "deadline": "27/10/2026 10:00",
    "date_limite": "27/10/2026 10:00",
    "publie": "Procedure ouverte 15/10/2026",
    "estimation": "1 299 972,00",
    "caution": "25 900,00",
    "quals": "Agriculture / Amenagement de pistes agricoles et rurales / "
             "7.1.Amenagement / Classe 5",
    "qualif": "Amenagement de pistes agricoles et rurales",
    "classe": "5",
    "lieu": "Province de Safi",
    "categorie": "Travaux",
    "mode_passation": "Appel d'offres ouvert",
    "concepts": ["Pistes rurales"],
    "lots": [],
    "link": ("https://www.marchespublics.gov.ma/index.php?page=entreprise."
             "EntrepriseDetailsConsultation&refConsultation=845321"
             "&orgAcronyme=g4h"),
}


class TestDecouperEntite(unittest.TestCase):
    """Le portail prefixe l'entite : '<ministere> / <ACRONYME> - <nom>'."""

    def test_chaines_du_portail(self):
        self.assertEqual(
            scanner_hicham._decouper_entite(
                "M4 / DPAG - DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI"),
            ("DPAG", "DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI"))
        self.assertEqual(
            scanner_hicham._decouper_entite(
                "M4 / ORMVAH - OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE "
                "D'EL HAOUZ"),
            ("ORMVAH", "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE D'EL HAOUZ"))

    def test_nom_contenant_un_tiret(self):
        """Seul le PREMIER tiret separe : les noms du portail en contiennent."""
        self.assertEqual(
            scanner_hicham._decouper_entite(
                "M4 / DPAA - DIRECTEUR PROVINCIAL D'AGRICULTURE- AL HOCEIMA"),
            ("DPAA", "DIRECTEUR PROVINCIAL D'AGRICULTURE- AL HOCEIMA"))

    def test_motif_absent_rend_la_chaine_telle_quelle(self):
        for txt in ("Direction Provinciale de l'Agriculture de Safi", "", None):
            self.assertEqual(scanner_hicham._decouper_entite(txt),
                             ("", (txt or "").strip()))


class TestPayload(unittest.TestCase):

    def test_cles_de_rapprochement(self):
        p = scanner_hicham.construire_payload_site(FICHE)
        self.assertEqual(p["ref_annonce"], "845321")
        self.assertEqual(p["source"], "marchespublics.gov.ma")
        self.assertEqual(p["url"], FICHE["link"])
        # L'acronyme vient de entiteAchat, pas de l'URL (qui porte celui
        # du ministere, identique pour tous les acheteurs).
        self.assertEqual(p["org_acronyme"], "DPAG")
        self.assertEqual(p["org_portail"], "g4h")

    def test_acronyme_de_repli_sur_l_url(self):
        fiche = dict(FICHE, entite_achat="")
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertEqual(p["org_acronyme"], "g4h")
        self.assertEqual(p["acheteur"], FICHE["buyer"])

    def test_acheteur_jamais_prefixe(self):
        p = scanner_hicham.construire_payload_site(FICHE)
        self.assertNotIn("/", p["acheteur"])
        self.assertFalse(p["acheteur"].startswith("M4"))
        # La chaine complete reste disponible a cote.
        self.assertEqual(p["entite_achat"], FICHE["entite_achat"])

    def test_date_limite_iso(self):
        p = scanner_hicham.construire_payload_site(FICHE)
        self.assertEqual(p["date_limite"], "2026-10-27T10:00:00+01:00")

    def test_publie_le_et_montants(self):
        p = scanner_hicham.construire_payload_site(FICHE)
        self.assertEqual(p["publie_le"], "2026-10-15")
        self.assertEqual(p["estimation"], 1299972.00)
        self.assertEqual(p["caution_provisoire"], 25900.00)

    def test_objet_complet_et_qualifications(self):
        p = scanner_hicham.construire_payload_site(FICHE)
        self.assertEqual(p["objet"], FICHE["objet_complet"])
        self.assertNotIn("...", p["objet"])
        self.assertEqual(p["qualifications"],
                         [{"libelle": "Amenagement de pistes agricoles et rurales",
                           "classe": "5"}])
        self.assertEqual(p["themes"], ["Pistes rurales"])
        self.assertEqual(p["acheteur"], "DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI")
        self.assertEqual(p["lieu"], "Province de Safi")

    def test_montant_illisible_donne_null_jamais_zero(self):
        fiche = dict(FICHE, estimation="", caution="Non precise")
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertIsNone(p["estimation"])
        self.assertIsNone(p["caution_provisoire"])

    def test_publie_le_illisible_donne_null(self):
        """Seule la date LIMITE est bloquante ; publie_le peut manquer."""
        fiche = dict(FICHE, publie="Procedure ouverte")
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertIsNone(p["publie_le"])
        self.assertEqual(p["date_limite"], "2026-10-27T10:00:00+01:00")

    def test_sans_ref_annonce_pas_de_payload(self):
        fiche = dict(FICHE, link="https://www.marchespublics.gov.ma/index.php"
                                 "?page=entreprise.EntrepriseDetailsConsultation"
                                 "&orgAcronyme=g4h")
        self.assertIsNone(scanner_hicham.construire_payload_site(fiche))

    def test_sans_date_limite_pas_de_payload(self):
        fiche = dict(FICHE, date_limite="", deadline="")
        self.assertIsNone(scanner_hicham.construire_payload_site(fiche))
        # Une date presente mais absurde est illisible, donc bloquante aussi.
        self.assertIsNone(scanner_hicham.construire_payload_site(
            dict(FICHE, date_limite="32/13/2026 10:00", deadline="")))

    def test_sans_org_acronyme_l_offre_part_quand_meme(self):
        """Le site sait rapprocher sur le seul nom de l'acheteur."""
        fiche = dict(FICHE, link="https://www.marchespublics.gov.ma/index.php"
                                 "?page=entreprise.EntrepriseDetailsConsultation"
                                 "&refConsultation=845321",
                     entite_achat="")
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertIsNotNone(p)
        self.assertIsNone(p["org_acronyme"])
        self.assertIsNone(p["org_portail"])
        self.assertEqual(p["acheteur"], FICHE["buyer"])

    def test_lots(self):
        fiche = dict(FICHE, estimation="", caution="", lots=[
            {"n": 1, "estimation": "600 000,00", "caution": "12 000,00",
             "qualif": "Amenagement de pistes agricoles et rurales", "classe": "4",
             "quals": "Agriculture / Amenagement de pistes agricoles et rurales "
                      "/ Classe 4"},
            {"n": 2, "estimation": "699 972,00", "caution": "13 900,00",
             "qualif": "", "classe": "", "quals": ""},
        ])
        p = scanner_hicham.construire_payload_site(fiche)
        self.assertEqual(len(p["lots"]), 2)
        self.assertEqual(p["lots"][0]["lot"], 1)
        self.assertIsNone(p["lots"][0]["intitule"])
        self.assertEqual(p["lots"][0]["estimation"], 600000.00)
        self.assertEqual(p["lots"][0]["qualifications"][0]["classe"], "4")
        self.assertEqual(p["lots"][1]["qualifications"], [])


# =========================================================
#  3. Site en panne, WhatsApp OK -> seul le site est retente
# =========================================================
HICHAM = {"name": "Hicham", "telegram": None, "whatsapp": "212661723762"}


class TestRattrapageCanal(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp(prefix="mpbots_test_")
        self._data = config.DATA_PATH
        config.DATA_PATH = self.dossier
        store._cache.clear()
        store._attente.clear()
        store._amorce = None

        self._sauv = (config_site.SITE_URL, config_site.SITE_TOKEN)
        _site_configure()

        self._tg, self._wa = notifier.send_telegram, notifier.send_whatsapp
        self.appels = {"whatsapp": 0, "site": 0}
        self.site_ok = False
        self.dernier = None

        import site_travaux
        self._envoyer = site_travaux.envoyer
        self.site_travaux = site_travaux

        def faux_wa(to, params, template=None):
            self.appels["whatsapp"] += 1
            return True

        def faux_site(payload, clients):
            self.appels["site"] += 1
            self.dernier = (payload, clients)
            return self.site_ok

        notifier.send_whatsapp = faux_wa
        notifier.send_telegram = lambda *a, **k: self.fail("Telegram inattendu")
        site_travaux.envoyer = faux_site

    def tearDown(self):
        notifier.send_telegram, notifier.send_whatsapp = self._tg, self._wa
        self.site_travaux.envoyer = self._envoyer
        config_site.SITE_URL, config_site.SITE_TOKEN = self._sauv
        config.DATA_PATH = self._data
        store._cache.clear()
        store._attente.clear()
        store._amorce = None
        shutil.rmtree(self.dossier, ignore_errors=True)

    def _alerte(self):
        return [{
            "sort_key": 0,
            "id": "offre-1",
            "msg": "message telegram",
            "wa_params": ["a"] * 8,
            "wa_template": "alerte_ao_travaux",
            "recipients": [HICHAM, config_site.ABONNE],
            "site": {"ref_annonce": "845321", "org_acronyme": "g4h"},
        }]

    def test_seul_le_site_est_retente(self):
        # --- passage 1 : WhatsApp passe, le site tombe ---
        self.site_ok = False
        notifier.broadcast(self._alerte())
        self.assertEqual(self.appels, {"whatsapp": 1, "site": 1})
        self.assertTrue(store.client_a_vu("Hicham", "offre-1"))
        self.assertFalse(store.client_a_vu("SiteTravaux", "offre-1"))
        # Le site recoit la liste des clients servis, sans lui-meme.
        self.assertEqual(self.dernier[1], ["Hicham"])

        # --- passage 2 : le site est encore en panne ---
        notifier.broadcast(self._alerte())
        self.assertEqual(self.appels, {"whatsapp": 1, "site": 2},
                         "le WhatsApp ne doit pas etre renvoye")

        # --- passage 3 : le site repond ---
        self.site_ok = True
        notifier.broadcast(self._alerte())
        self.assertEqual(self.appels, {"whatsapp": 1, "site": 3})
        self.assertTrue(store.client_a_vu("SiteTravaux", "offre-1"))

        # --- passage 4 : plus personne n'est concerne ---
        notifier.broadcast(self._alerte())
        self.assertEqual(self.appels, {"whatsapp": 1, "site": 3})

    def test_abandon_apres_max_essais(self):
        self.site_ok = False
        for _ in range(store.MAX_ESSAIS):
            notifier.broadcast(self._alerte())
        self.assertEqual(self.appels["site"], store.MAX_ESSAIS)
        self.assertTrue(store.client_a_vu("SiteTravaux", "offre-1"))
        notifier.broadcast(self._alerte())
        self.assertEqual(self.appels["site"], store.MAX_ESSAIS,
                         "apres abandon, l'offre ne doit plus etre proposee")
        self.assertEqual(self.appels["whatsapp"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
