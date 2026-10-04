"""
Client HICHAM — appels d'offres TRAVAUX du secteur agricole.

Ce fichier est INDEPENDANT de config.py : modifier la liste des acheteurs
ou les concepts ici n'a aucun effet sur les bots BDC et AO existants.

Historique : data/seen_hicham.json (separe des autres clients).
"""
import os

CLIENT = "hicham"

# =========================================================
#                      ABONNES
# =========================================================
# whatsapp : format international SANS "+" ni espaces
# telegram : chat_id, None => pas de Telegram
ABONNES = [
    {"name": "Hicham", "telegram": None, "whatsapp": "212661723762"},
]

# Template WhatsApp dedie (8 variables).
# Repli possible : WA_TEMPLATE_HICHAM=alerte_veille_mp (6 variables).
WA_TEMPLATE = os.getenv("WA_TEMPLATE_HICHAM", "alerte_ao_travaux")

# =========================================================
#                   REGLAGES RUNTIME
# =========================================================
# Nombre maxi de fiches de detail ouvertes par passage.
# Mettre 5 au premier essai, puis monter une fois les logs verifies.
MAX_FICHES = int(os.getenv("HICHAM_MAX_FICHES", "5"))

# Pour tout renvoyer a Hicham : supprimer data/seen_client_hicham.json
# depuis la console Railway. Aucun autre client n'est affecte.

# Fenetres de recherche (jours), identiques a la logique du portail
JOURS_PUBLICATION = int(os.getenv("HICHAM_JOURS_PUB", "180"))   # en arriere
JOURS_LIMITE = int(os.getenv("HICHAM_JOURS_LIMITE", "180"))     # en avant

# Formulaire : AO ouvert = "1", categorie Travaux = "1"
PROCEDURE_TYPE = os.getenv("HICHAM_PROCEDURE", "1")
CATEGORIE = os.getenv("HICHAM_CATEGORIE", "1")   # 1 = Travaux, 3 = Services

# =========================================================
#              ACHETEURS PUBLICS CIBLES (40)
# =========================================================
# Noms releves tels quels sur le portail. La comparaison neutralise
# majuscules, accents, apostrophes et espaces multiples ; un nom proche
# mais non identique est signale dans les logs sans declencher d'alerte.
ACHETEURS = [
    # --- ORMVA ---
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE D'EL HAOUZ",
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE D'EL GHARB",
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE DE LOUKKOUS",
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE DE MELOUIA",
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE DE OUARZAZATE",
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE DE TAFILALET",
    "OFFICE REGIONAL DE MISE EN VALEUR AGRICOLE DE TADLA",

    # --- Directions provinciales (libelle "DE ...") ---
    "DIRECTEUR PROVINCIAL DE L'AGRICULTURE DE BENIMELLAL",
    "Direction Provinciale de l'Agriculture de Driouch",
    "DIRECTEUR PROVINCIAL DE L'AGRICULTURE DE GUERCIF",
    "DIRECTION PROVINCIALE DE L'AGRICULTURE DE JERADA",
    "Direction Provinciale de l'Agriculture de Ouezzane",
    "DIRECTION PROVINCIALE DE L'AGRICULTURE DE RHAMNA",
    "Direction Provinciale de l'Agriculture de Taourirt",
    "DIRECTION PROVINCIALE DE L'AGRICULTURE D'EL YOUSSOUFIA",
    "LE DIRECTEUR PROVINCIAL DE L'AGRICULTURE DE TAZA",

    # --- Directions provinciales (libelle court) ---
    "DIRECTEUR PROVINCIAL D'AGRICULTURE AGADIR",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE- AL HOCEIMA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE CHEFCHAOUEN",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE CHICHAOUA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE D'AZILAL",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE D'ESSAOUIRA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE EL JADIDA",
    "DIRECTEUR PROVINCIALE D'AGRICULTURE-FES",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE FIGUIG BOUARFA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE IFRANE",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE KHEMISSET",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE KHENIFRA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE-KHOURIBGA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE DE MEKNES",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE-MARRAKECH",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE NADOR",
    "DIRECTION PROVINCIALE D'AGRICULTURE OUJDA",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE SEFROU",
    "DIRECTION PROVINCIALE D'AGRICULTURE DE SETTAT",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE DE TANGER",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE TAOUNATE",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE TETOUAN",
    "DIRECTEUR PROVINCIAL D'AGRICULTURE TIZNIT",
]

# =========================================================
#                  CONCEPTS METIER
# =========================================================
# Un concept = une liste d'alternatives.
# Une alternative = des racines qui doivent TOUTES figurer dans l'objet,
# dans n'importe quel ordre. La comparaison se fait par DEBUT de mot :
# "PISTE" attrape "PISTES", "PLANTATION" attrape "PLANTATIONS".
#
# Exemples valides pour "Pompage solaire" :
#   "TRAVAUX D'EQUIPEMENT EN SYSTEME DE POMPAGE SOLAIRE"        -> OK
#   "Equipement des stations de pompage en energie solaire"      -> OK
CONCEPTS = {
    "Pistes rurales":  [["PISTE"]],
    "Plantation":      [["PLANTATION"]],
    "Perimetres PMH":  [["PERIMETRE", "HYDRAUL"],
                        ["PMH"],
                        ["PETITE", "MOYENNE", "HYDRAUL"]],
    "Pompage solaire": [["POMPAGE", "SOLAIRE"],
                        ["POMPE", "SOLAIRE"],
                        ["POMPAGE", "ENERGIE", "SOLAIRE"]],
    "Seguias":         [["SEGUIA"]],
}

# Mots qui excluent une annonce meme si un concept est trouve.
# Vide : Hicham veut les pistes pastorales/forestieres ET l'assistance
# technique. Ajouter ici si cela change.
EXCLUSIONS = []
