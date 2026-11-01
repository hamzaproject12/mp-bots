# mp-bots

Veille des marches publics marocains (marchespublics.gov.ma), en Python +
Playwright. Un seul conteneur, un seul navigateur Chromium partage, trois
scanners :

| Scanner | Fichier | Flux |
|---|---|---|
| `bdc` | `scanner_bdc.py` | bons de commande |
| `ao` | `scanner_ao.py` | appels d'offres |
| `hicham` | `scanner_hicham.py` | appels d'offres TRAVAUX du secteur agricole |

Les alertes partent sur Telegram, WhatsApp (Cloud API) et, pour le flux
`hicham`, vers un site externe FastAPI (canal `site`).

## Memoire

Tout vit dans `DATA_PATH` (volume Railway monte sur `/app/data`) :

* `seen_client_<slug>.json` — historique **par destinataire**. Supprimer le
  fichier d'un client lui renvoie tout, sans toucher aux autres.
* `attente_client_<slug>.json` — livraisons partielles. Tant qu'un canal
  manque, l'offre n'entre pas dans l'historique : seul le canal en echec est
  retente, jusqu'a `MAX_ESSAIS_CANAL` passages.

Le site est un destinataire comme un autre (`SiteTravaux`) : il a son propre
historique et sa propre memoire de livraison. C'est ce qui lui permet de
rattraper des offres deja parties chez les clients WhatsApp **sans que ceux-ci
recoivent quoi que ce soit**.

## Variables Railway

### Execution

| Variable | Defaut | Role |
|---|---|---|
| `RUN_MODE` | `once` | `once` (un passage) / `loop` (24h/24) / `selftest` / `amorcage` (voir plus bas) |
| `ADMIN_TELEGRAM` | `1952904877` (admin) | chat Telegram de l'administrateur : seul destinataire des alertes de rupture de scraping. Definie vide = aucun envoi |
| `SENTINELLE_SILENCE_H` | `6` | pas deux messages de rupture pour le meme scanner a moins de N heures |
| `SCANNERS` | `bdc,ao` | scanners actifs, separes par des virgules |
| `DATA_PATH` | `data` | dossier persistant (`/app/data/v2` en production) |
| `SLEEP_OK` / `SLEEP_FAIL` | `14400` / `900` | sommeil en mode `loop` (s) |
| `MAX_SEEN` | `3000` | offres gardees par historique |
| `MAX_ESSAIS_CANAL` | `6` | passages avant abandon d'un canal en echec |
| `DEBUG_SCORING` | `0` | journalise le detail du filtrage |

Un deploiement en `once` ou `loop` est **toujours silencieux** : aucun message
de controle n'est envoye. Pour tester un canal : `RUN_MODE=selftest`.

### Mode `amorcage` (apres un changement d'identifiants d'offres)

`RUN_MODE=amorcage` fait un passage complet, marque **toutes** les offres
retenues comme vues pour les **clients** et n'envoie **rien**, sur aucun canal.
Le site (`SiteTravaux`) n'est **pas** marque : il rattrape ainsi les appels
d'offres deja en ligne (son endpoint est idempotent sur `ref_annonce`). Le log
final dit `🌱 amorcage : N offres marquees pour les clients, site exclu`. A lancer une fois apres un deploiement qui
change les identifiants (ex. passage du md5 du texte aux identifiants du portail
`bdc-<n>` / `ao-<n>`), puis revenir en `once` / `loop`.

### Sentinelle

Si le portail annonce N > 0 offres mais qu'un scanner n'en extrait aucune, le
log contient `🚨 [<scanner>] RUPTURE : N annoncees, 0 extraites` et, si
`ADMIN_TELEGRAM` est renseigne, l'administrateur recoit un message qui dit quel
scanner, combien d'offres annoncees, zero extraite, et que les selecteurs sont
a revoir. Aucun client ne recoit jamais ce message.

Une rupture n'est **pas** un echec de passage : le rythme reste celui d'un
passage normal (`SLEEP_OK`), reessayer dans 15 minutes ne servirait a rien. Pour
ne pas inonder l'administrateur, la sentinelle garde une memoire sur disque
(`DATA_PATH/sentinelle.json`, `{"<scanner>": "<horodatage>"}`) : pas deux
messages pour le meme scanner a moins de `SENTINELLE_SILENCE_H` heures
(6 par defaut). La rupture reste journalisee a chaque passage.

### Telegram / WhatsApp

| Variable | Defaut | Role |
|---|---|---|
| `TELEGRAM_TOKEN` | — | jeton du bot |
| `WA_TOKEN` | — | jeton Cloud API (vide = WhatsApp desactive) |
| `WA_PHONE_ID` | `1318151618051403` | numero emetteur |
| `WA_TEMPLATE` | `alerte_veille_mp` | modele par defaut |
| `WA_TEMPLATE_HICHAM` | `alerte_ao_travaux` | modele du flux travaux (8 variables) |
| `WA_LANG` | `fr` | langue du modele |
| `WA_API_VERSION` | `v25.0` | version de l'API Graph |

### Flux `hicham`

| Variable | Defaut | Role |
|---|---|---|
| `HICHAM_MAX_FICHES` | `5` | fiches de detail ouvertes par passage |
| `HICHAM_JOURS_PUB` | `180` | fenetre de publication (jours en arriere) |
| `HICHAM_JOURS_LIMITE` | `180` | fenetre de remise (jours en avant) |
| `HICHAM_PROCEDURE` | `1` | AO ouvert |
| `HICHAM_CATEGORIE` | `1` | 1 = Travaux, 3 = Services |

### Site externe (canal `site`)

| Variable | Defaut | Role |
|---|---|---|
| `SITE_URL` | *(vide)* | endpoint FastAPI, ex. `https://travaux.xxx/api/v1/appels_ouverts` |
| `SITE_TOKEN` | *(vide)* | envoye en `Authorization: Bearer <token>` |
| `SITE_TIMEOUT` | `20` | delai d'attente du POST, en secondes |

**Tant que `SITE_URL` ou `SITE_TOKEN` est vide, le canal n'existe pas** et le
comportement actuel est strictement inchange.

Corps de la requete :

```json
{"appels_ouverts": [{"source": "marchespublics.gov.ma",
                     "ref_annonce": "845321",
                     "org_acronyme": "DPAG", "org_portail": "q1s",
                     "reference": "...", "objet": "...",
                     "acheteur": "DIRECTEUR PROVINCIAL D'AGRICULTURE SAFI",
                     "entite_achat": "M4 / DPAG - DIRECTEUR PROVINCIAL ...",
                     "publie_le": "2026-10-15",
                     "date_limite": "2026-10-27T10:00:00+01:00",
                     "estimation": 1299972.00, "caution_provisoire": 25900.00,
                     "lots": [], "qualifications": [], "lieu": "...",
                     "themes": ["Pistes rurales"], "url": "https://...",
                     "clients": ["Hicham", "Pako"]}]}
```

Cles de rapprochement :

* `ref_annonce` — `refConsultation` de l'URL de la consultation.
* `org_acronyme` — jeton du milieu de `entiteAchat` (`M4 / **DPAG** - ...`).
  Le `orgAcronyme` de l'URL valait `q1s` pour tous les acheteurs avant la
  migration d'octobre 2026 ; il varie maintenant selon l'acheteur, mais le
  jeton de `entiteAchat` reste prioritaire (comportement inchange). L'acronyme
  de l'URL est conserve a part dans `org_portail`.
* `acheteur` — le NOM seul, jamais la chaine prefixee ; la chaine complete
  part dans `entite_achat`.

Une offre n'est **pas** envoyee si `ref_annonce` manque ou si la date limite
est illisible : ce sont les deux refus certains cote site, autant ne pas les
pousser six fois. Le motif est journalise. Un acronyme d'organisme absent
n'est pas bloquant : le rapprochement sur l'acheteur suffit.

Toute reponse 2xx vaut succes ; sinon le canal est retente au passage suivant.

Au premier passage apres activation, le site n'a aucun historique : il
rattrape donc tout ce qui est en ligne, par tranches de `HICHAM_MAX_FICHES`
offres par passage.

## Tests

Sans dependance supplementaire, sans toucher a la production (DATA_PATH est
redirige vers un dossier temporaire, aucun appel reseau) :

```bash
python -m unittest discover -s tests -t .
```

## Fins de ligne

`scanner_bdc.py` et `scanner_ao.py` sont en CRLF, tous les autres fichiers en
LF. A preserver tel quel lors des modifications.
