"""
Configuration centrale des deux bots (BDC et AO).
Tout ce qui se regle sans toucher a la logique est ici.
"""
import os

# =========================================================
#                    ENVIRONNEMENT
# =========================================================
DATA_PATH = os.getenv("DATA_PATH", "data")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# --- WhatsApp Cloud API ---
WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID", "1318151618051403")
WA_TEMPLATE = os.getenv("WA_TEMPLATE", "alerte_marche_public")
WA_LANG = os.getenv("WA_LANG", "fr")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v25.0")

# --- Modes ---
# RUN_MODE=once  -> un seul passage puis sortie (a utiliser avec le cron Railway)
# RUN_MODE=loop  -> boucle infinie avec sommeil (ancien comportement)
RUN_MODE = os.getenv("RUN_MODE", "once").lower()

# Quels scanners activer : "bdc", "ao", ou "bdc,ao"
SCANNERS = [s.strip().lower() for s in os.getenv("SCANNERS", "bdc,ao").split(",") if s.strip()]

WA_TEST = os.getenv("WA_TEST", "0") == "1"
DEBUG_SCORING = os.getenv("DEBUG_SCORING", "0") == "1"
STARTUP_PING = os.getenv("STARTUP_PING", "0") == "1"   # message Telegram au demarrage

# --- Rythme (mode loop uniquement) ---
SLEEP_OK = int(os.getenv("SLEEP_OK", "14400"))    # 4h apres un passage reussi
SLEEP_FAIL = int(os.getenv("SLEEP_FAIL", "900"))  # 15 min apres un echec

# --- Memoire : nombre max d'offres gardees dans l'historique ---
MAX_SEEN = int(os.getenv("MAX_SEEN", "3000"))


# =========================================================
#                      ABONNÉS
# =========================================================
# whatsapp : format international SANS "+" ni espaces (ex 212700301878)
#            None => pas de WhatsApp pour cet abonne
# telegram : chat_id Telegram, None => pas de Telegram
# bdc / ao : True si l'abonne veut ce flux
# subscriptions : categories du bot BDC ("ALL" = tout)
SUBSCRIBERS = [
    {
        "name": "Moi",
        #"telegram": "1952904877",
        "telegram": None,
        #"whatsapp": "212700301878",
        "whatsapp": None,
        "bdc": True,
        "ao": True,
        "subscriptions": ["ALL"],
    },
    # {
    #     "name": "Zakariya",
    #     "telegram": "8260779046",
    #     "whatsapp": "212660576019",
    #     "bdc": True,
    #     "ao": True,
    #     "subscriptions": ["Event & Formation"],
    # },
    {
        "name": "Hamza",
        "telegram": "8260779046",          # ⚠️ mettre son VRAI chat_id, different de Zakariya
        "whatsapp": "212665803935",
        "bdc": True,
        "ao": True,
        "subscriptions": ["Event & Formation"],
    },
    # {
    #     "name": "Abdeslam",
    #     "telegram": "7943145340",
    #     "whatsapp": None,
    #     "bdc": True, "ao": True,
    #     "subscriptions": ["Mdiq"],
    # },
    # {
    #     "name": "Yassine",
    #     "telegram": "7879373928",
    #     "whatsapp": None,
    #     "bdc": True, "ao": False,
    #     "subscriptions": ["Event & Formation"],
    # },
]


# =========================================================
#              BOT BDC : MOTS-CLÉS ET SEUILS
# =========================================================
SEUIL_EVENT = int(os.getenv("SEUIL_EVENT", "2"))    # Event & Formation : 2 mots mini
SEUIL_DEFAUT = int(os.getenv("SEUIL_DEFAUT", "1"))  # autres categories : 1 mot suffit

KEYWORDS = {
    "Dév & Web": ["développement", "application", "web", "portail", "logiciel",
                  "plateforme", "maintenance", "site internet", "app", "digital"],
    "Data": ["données", "data", "numérisation", "archivage", "ged", "big data",
             "statistique", "traitement", "ia"],
    "Infra": ["hébergement", "cloud", "maintenance", "sécurité", "serveur",
              "réseau", "informatique", "matériel informatique"],
    "Event & Formation": ["formation", "atelier", "renforcement de capacité", "organisation",
                          "animation", "sensibilisation", "impression", "conception",
                          "enquête", "étude", "conseil agricole", "conseil", "agri"],
    "Mdiq": ["mdiq", "MDIQ-FNIDEQ", "MEDIAQ", "MDIQ FNIDEQ", "Sante", "GST"],
}

# Acronymes courts : doivent etre des mots ISOLES.
# Sinon "app" attrape "appel" (present dans "appel d'offres" = toutes les annonces)
# et "ia" attrape "materiaux", "special"...
# Les autres mots-cles matchent en PREFIXE : "agri" attrape "agricole", "agriculture".
MOTS_EXACTS = {"app", "ia", "web", "data", "ged", "gst", "cloud"}

# Zones prioritaires : BONUS seulement (signalement + tri), jamais un
# laissez-passer. Une offre doit d'abord passer le filtre mots-cles.
SPECIAL_ZONES = ["errachidia", "ouarzazate", "midelt", "tafilalet"]

EXCLUSIONS_BDC = [
    "nettoyage", "gardiennage", "construction", "location", "fournitures de bureau",
    "mobilier", "siège", "chaise", "bâtiment", "plomberie", "sanitaire", "toilette",
    "douche", "peinture", "électricité", "jardinage", "espaces verts", "piscine",
    "vêtement", "habillement", "aménagement", "travaux", "voirie", "topographique",
    "topographie", "billet", "billetterie", "aérien", "ensam", "faculte", "faculté",
    "université", "école supérieure", "ecole superieure",
]


# =========================================================
#              BOT AO : WHITELIST ACHETEURS
# =========================================================
TARGET_BUYERS = [
    # --- DIRECTIONS RÉGIONALES (DRA) ---
    "DIRECTION REGIONALE DE L'AGRICULTURE",
    "DIRECTION REGIONALE D'AGRICULTURE",
    "DIRECTEUR REGIONAL DE L'AGRICULTURE",
    "DIRECTEUR REGIONAL D'AGRICULTURE",
    "DIRECTION REGIONALE AGRICULTURE",

    # --- DIRECTIONS PROVINCIALES (DPA) ---
    "DIRECTION PROVINCIALE DE L'AGRICULTURE",
    "DIRECTION PROVINCIALE D'AGRICULTURE",
    "DIRECTEUR PROVINCIAL DE L'AGRICULTURE",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE",
    "DIRECTEUR PROVINCIALE DE L'AGRICULTURE",
    "DIRECTEUR PROVINCIALE D'AGRICULTURE",
    "DIRECTION PROVINCIAL DE L'AGRICULTURE",

    # --- OFFICES DE MISE EN VALEUR (ORMVA) ---
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE",
    "OFFICE REGIONAL DE LA MISE EN VALEUR AGRICOLE",
    "ORMVA",
    "O.R.M.V.A",

    # --- SÉCURITÉ SANITAIRE (ONSSA) ---
    "ONSSA",
    "OFFICE NATIONAL DE SECURITE SANITAIRE",
    "DIRECTION REGIONALE DE L'ONSSA",

    # --- CONSEIL AGRICOLE (ONCA) ---
    "ONCA",
    "OFFICE NATIONAL DU CONSEIL AGRICOLE",
    "DIRECTION REGIONALE DU CONSEIL AGRICOLE",
    "CENTRE DU CONSEIL AGRICOLE",
    "CONSEIL AGRICOLE",

    # --- CHAMBRES ---
    "CHAMBRE D'AGRICULTURE",
    "CHAMBRE REGIONALE D'AGRICULTURE",

    # --- INSTITUTS & ÉCOLES ---
    "ECOLE NATIONALE D'AGRICULTURE",
    "INSTITUT DES TECHNICIENS",
    "INSTITUT TECHNIQUE AGRICOLE",

    # --- AGENCES ---
    "AGENCE NATIONALE POUR LE DEVELOPPEMENT DES ZONES OASIENNES",
    "AGENCE POUR LE DEVELOPPEMENT AGRICOLE",
]

EXCLUSIONS_AO = [
    "nettoyage", "gardiennage", "construction", "bâtiment", "plomberie",
    "sanitaire", "peinture", "électricité", "jardinage", "espaces verts",
    "piscine", "vêtement", "habillement", "carburant", "véhicule",
    "transport", "billet", "aérien", "travaux", "voirie", "topographique",
    "la peche", "secteur de la pêche", "maritime",
]


# =========================================================
#                      DIVERS
# =========================================================
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

URL_BDC = "https://www.marchespublics.gov.ma/bdc/entreprise/consultation/"
URL_AO = ("https://www.marchespublics.gov.ma/index.php"
          "?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons")