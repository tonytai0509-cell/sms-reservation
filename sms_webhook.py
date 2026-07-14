"""
sms_webhook.py

Etape 3 : reception d'un SMS + extraction IA (Claude Haiku) des infos de
reservation (type prive/medical, nom, telephone, prise en charge,
destination, heure) + reponse automatique adaptee + creation automatique
de l'evenement dans Google Agenda.

Fonctionne avec l'app "SMS Gateway for Android" (docs.sms-gate.app).

Variables d'environnement necessaires (a mettre sur Railway) :
  SMS_GATEWAY_SIGNING_KEY     -> cle de signature webhook (app > Parametres > Webhooks)
  SMS_GATEWAY_USERNAME        -> identifiant Cloud/Local de l'app
  SMS_GATEWAY_PASSWORD        -> mot de passe Cloud/Local de l'app
  SMS_GATEWAY_MODE            -> "cloud" ou "local" (defaut: cloud)
  SMS_GATEWAY_LOCAL_URL       -> uniquement si mode local (ex: https://192.168.1.10:8080)
  ANTHROPIC_API_KEY           -> cle API Anthropic (console.anthropic.com), pour l'extraction IA
  GOOGLE_SERVICE_ACCOUNT_JSON -> contenu JSON complet de la cle du compte de service Google
  GOOGLE_CALENDAR_ID          -> adresse email du calendrier Google a utiliser (souvent ton email)
  GOOGLE_MAPS_API_KEY         -> cle API Google Maps (Distance Matrix), pour estimer les temps de trajet
  RESEND_API_KEY              -> cle API Resend (resend.com), pour l'envoi d'email (HTTPS, pas de SMTP bloque)
  EMAIL_DESTINATAIRE          -> adresse email qui recoit les confirmations (ex: tony.tai0509@gmail.com)
  DOSSIER_DONNEES             -> (optionnel) dossier de stockage persistant pour la memoire des
                                  reservations en cours (defaut: /data, prevu pour un Volume Railway)
"""

import hashlib
import hmac
import json
import logging
import os
import random
import re
import string
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sms_webhook")

SIGNING_KEY = os.environ.get("SMS_GATEWAY_SIGNING_KEY", "")
GATEWAY_USERNAME = os.environ.get("SMS_GATEWAY_USERNAME", "")
GATEWAY_PASSWORD = os.environ.get("SMS_GATEWAY_PASSWORD", "")
GATEWAY_MODE = os.environ.get("SMS_GATEWAY_MODE", "cloud")
LOCAL_URL = os.environ.get("SMS_GATEWAY_LOCAL_URL", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
FUSEAU_HORAIRE = ZoneInfo("Europe/Paris")

# Numeros autorises a utiliser la commande rapide "RDV" (voir plus bas).
# Configurable via la variable Railway ADMIN_PHONE_NUMBERS (numeros separes
# par des virgules, format international +336...). Par defaut, seul le
# numero habituel de Tony y a acces.
ADMIN_PHONE_NUMBERS = {
    numero.strip()
    for numero in os.environ.get("ADMIN_PHONE_NUMBERS", "+33624125779").split(",")
    if numero.strip()
}

# Dossier de stockage persistant (Volume Railway monte sur /data). Si le
# volume n'existe pas (ex: en local), on retombe sur le dossier courant
# pour ne jamais planter au demarrage.
DOSSIER_DONNEES = os.environ.get("DOSSIER_DONNEES", "/data")
if not os.path.isdir(DOSSIER_DONNEES):
    try:
        os.makedirs(DOSSIER_DONNEES, exist_ok=True)
    except OSError:
        DOSSIER_DONNEES = "."
FICHIER_MEMOIRE_RESERVATIONS = os.path.join(DOSSIER_DONNEES, "memoire_reservations.json")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_DESTINATAIRE = os.environ.get("EMAIL_DESTINATAIRE", "")

PROMPT_SYSTEME = """Tu geres une conversation SMS avec un client qui reserve un taxi.
Tu recois : les informations DEJA CONNUES sur sa reservation (peuvent etre
vides), et le NOUVEAU message qu'il vient d'envoyer.

Ta tache : renvoyer la version A JOUR complete des informations, en
combinant intelligemment les infos deja connues avec ce que le nouveau
message apporte.

Champs :
- type : "prive" ou "medical". Medical si mention d'un hopital, clinique,
  CHU, service medical (ex: "neurologie", "dialyse"), medecin, consultation,
  etc. Medical AUSSI si le lieu de destination ou de prise en charge
  correspond a l'un de ces etablissements de sante de la region de Nice
  (reconnais-les meme mal orthographies, abreges, ou sans le mot
  "hopital"/"clinique" devant) :
  - Hopital Les Sources
  - Clinique Saint George / Saint-George
  - Hopital Pasteur (Pasteur 1, Pasteur 2)
  - Hopital L'Archet / Archet (Archet 1, Archet 2)
  - Hopitaux Pediatriques de Nice / Fondation Lenval / Lenval
  - Centre Antoine Lacassagne
  - Centre medical et dentaire MGEN
  - Centre Medical Pierre Sola
  - Clinique du Parc Imperial
  - Clinique Saint-Antoine
  - Polyclinique Santa Maria
  - Clinique Saint-Francois
  - Hopital Cimiez
  - Polyclinique Saint Jean (Cagnes-sur-Mer) -- "Saint-Jean" ou "St Jean"
    mentionne seul (sans le mot "polyclinique") compte AUSSI comme medical,
    meme si le nom existe egalement comme quartier residentiel (Saint-Jean
    Cap-Ferrat) : dans le doute, priviligier medical pour ce nom precis.
  - Institut Arnault Tzanck / Tzanck / CRC Nice
  RACCOURCIS A RECONNAITRE SYSTEMATIQUEMENT (meme seuls, en minuscule, sans
  autre contexte medical explicite) : "tzanck", "les sources", "sola" /
  "pierre sola" -> toujours medical des que l'un de ces mots apparait comme
  destination ou prise en charge, meme sans "hopital"/"clinique"/"centre" devant.
  Sinon (adresse residentielle, aeroport, gare, restaurant, etc.) -> prive.
- nom : nom de famille du client. Ne provient JAMAIS d'un mot comme "avenue",
  "rue", "boulevard", "chemin", "allee", "impasse" -- ce sont des adresses,
  pas des noms. Un nom apparait typiquement apres "je suis", "M.", "Mme",
  "pour M./Mme", ou en signature. Si le nouveau message ne contient pas de
  nom clairement identifiable, garde le nom deja connu (ne le remplace pas
  par un mot d'adresse).
- telephone : numero de contact different de l'expediteur, si mentionne.
- prise_en_charge : adresse PRECISE et exploitable par un chauffeur (numero
  + nom de rue, ou un lieu nomme reconnaissable comme un hopital/gare/
  aeroport). IMPORTANT : des termes vagues comme "mon domicile", "chez moi",
  "ma maison", "mon adresse habituelle" NE SONT PAS des adresses valides
  tant qu'aucune adresse concrete n'est donnee avec -> laisse ce champ null
  dans ce cas (il faudra la demander explicitement au client), meme si le
  message semble par ailleurs complet.
- destination : meme regle que prise_en_charge -- doit etre une adresse ou
  un lieu precis, pas une reference vague.
- heure_rdv : heure du rendez-vous/consultation/evenement lui-meme, si le
  client la mentionne (ex: "j'ai rendez-vous a 12h", "consultation a 14h").
  C'est une info indicative, PAS l'heure a laquelle le chauffeur doit venir.
- heure : heure precise a laquelle le CHAUFFEUR doit venir chercher le
  client (ex: "venez me chercher a 8h30", "prise en charge 8h", ou une heure
  donnee directement sans mention de rendez-vous). ATTENTION : si le client
  dit seulement "j'ai rendez-vous a 12h" ou "consultation a 14h" SANS
  preciser separement l'heure de prise en charge souhaitee, ALORS heure
  reste null (ce n'est pas la meme chose que heure_rdv) -- il faudra la lui
  demander explicitement. Une date seule sans heure chiffree (ex: juste
  "demain") NE COMPTE PAS non plus comme une heure valide.
- date : la date evoquee par le client (aujourd'hui, demain, apres-demain,
  une date precise comme "le 20 juillet"), au format exact "AAAA-MM-JJ",
  calculee a partir de la date actuelle donnee plus bas. Le client peut
  ecrire la date sous N'IMPORTE QUELLE forme courante, a interpreter
  systematiquement, AVEC OU SANS zero devant les chiffres < 10 :
  "16 01", "16.01", "16/01", "16-01", "16 janvier", "le 16/01",
  "16/01/2026", "8.8", "8/8", "8 8", "le 8 aout" (jour/mois, ou
  jour/mois/annee si precisee - en France le jour vient toujours en
  premier, jamais le mois). "8.8" ou "8/8" ou "8 8" signifient TOUJOURS le
  8 aout, jamais une heure ni un decimal -- ne confonds jamais deux
  chiffres separes par un point, un slash, un tiret ou un espace avec une
  heure (une heure s'ecrit avec un "h" comme "13h" ou "13h30", jamais avec
  un point ou un slash). Si aucune annee n'est precisee, prends l'annee en
  cours ou la suivante selon que la date est deja passee ou non par
  rapport a aujourd'hui. CONTRAIREMENT a heure/heure_iso, ce champ doit
  etre rempli DES QU'UNE DATE est evoquee, MEME SI aucune heure precise
  n'est encore donnee (ex: "rendez-vous apres-demain" sans heure -> date
  rempli, heure/heure_iso restent null en attendant l'heure precise). Une
  fois rempli, il n'est JAMAIS efface -- garde-le meme si les messages
  suivants ne reparlent pas de la date, sauf si le client mentionne
  clairement une nouvelle date differente.
- heure_iso : UNIQUEMENT si le champ "heure" ci-dessus est rempli, la
  date et heure de prise en charge au format exact "AAAA-MM-JJTHH:MM:00"
  (ex: "2026-07-14T08:30:00"). Utilise en PRIORITE le champ "date" ci-dessus
  (deja connu ou nouvellement mentionne) pour le jour, combine a l'heure
  connue. Sinon null.
- est_question : true si le nouveau message est une question ou une
  remarque du client (ex: "a quelle heure venez-vous ?", "c'est confirme ?",
  "merci") qui n'apporte AUCUNE nouvelle info de reservation exploitable
  (au dela de repeter une info deja connue) ; false s'il apporte une info
  nouvelle ou modifiee.
- plusieurs_courses : true si le nouveau message decrit CLAIREMENT plusieurs
  trajets/courses distincts avec des destinations et/ou heures differentes
  (ex: "aller a X a 14h puis a Y a 15h"). false sinon (un seul trajet).
- annulation : true si le nouveau message demande CLAIREMENT d'annuler la
  reservation en cours (ex: "annulez ma course", "je veux annuler",
  "finalement non merci, annulez", "c'est annule"). false sinon.
- reference_annulation : si annulation est true ET que le client cite un
  code de reference (ex: "annulez ABC123", "annulez la reservation ABC123"),
  ce code exactement tel qu'ecrit (majuscules). Sinon null.
- confirmation_existante : true si le client semble parler d'une
  reservation/rendez-vous DEJA CONVENU par ailleurs et demande juste une
  confirmation (ex: "je dois etre a 9h30 aux sources, c'est ok ?", "c'est
  bien prevu ?", "vous confirmez ?"), plutot que de faire une demande
  complete et explicite de nouvelle reservation. false sinon.
- reference_lookup : si confirmation_existante est true ET que le client
  cite un code de reference, ce code (majuscules). Sinon null.
- reclamation : true si le message exprime un probleme, une plainte ou une
  insatisfaction concernant le service (ex: "mon taxi n'est pas venu",
  "chauffeur en retard", "je ne suis pas content", "personne n'est venu me
  chercher"), plutot qu'une demande de reservation ou une question de
  suivi normale. false sinon.

Regles generales :
- Un champ deja connu ne doit JAMAIS etre efface ou remplace par une valeur
  moins precise ou hors-sujet. Il n'est remplace que si le nouveau message
  apporte clairement une correction ou precision sur ce champ precis.
- Les champs jamais renseignes restent a null.
- EXCEPTION IMPORTANTE : si le nouveau message mentionne un nom de famille
  clairement DIFFERENT de celui deja connu, c'est un NOUVEAU client (le
  numero de telephone est peut-etre partage/reutilise). Dans ce cas,
  IGNORE COMPLETEMENT les anciennes valeurs de prise_en_charge,
  destination, heure_rdv et heure (meme si elles etaient renseignees) :
  ne les reprends que si le nouveau message les mentionne lui-meme a
  nouveau. Seul le nouveau nom est utilise, tout le reste redemarre a zero.

Reponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou apres,
et SANS balises markdown (pas de ```json, pas de backticks du tout).
Ta reponse doit commencer directement par { et finir par }, au format exact :
{"type": "...", "nom": ..., "telephone": ..., "prise_en_charge": ..., "destination": ..., "heure_rdv": ..., "heure": ..., "date": ..., "heure_iso": ..., "est_question": true/false, "plusieurs_courses": true/false, "annulation": true/false, "reference_annulation": ..., "confirmation_existante": true/false, "reference_lookup": ..., "reclamation": true/false}
"""

CHAMPS_OBLIGATOIRES = {
    "nom": "votre nom",
    "prise_en_charge": "l'adresse de prise en charge",
    "destination": "la destination",
    "date": "la date du transport",
    "heure": "l'heure a laquelle le chauffeur doit venir vous chercher",
}

# Memoire des reservations par numero de telephone, qu'elles soient
# completes ou non. Permet de repondre correctement a un message de suivi
# ("a quelle heure ?") sans tout redemander, et de fusionner les infos
# entre plusieurs SMS d'une meme demande.
# Format : { "+336...": {"donnees": {...}, "derniere_maj": ts, "complete": bool} }
# Dictionnaire en RAM, mais persiste sur disque (voir
# charger_memoire_reservations / persister_memoire_reservations) dans
# DOSSIER_DONNEES (Volume Railway monte sur /data) pour survivre a un
# redemarrage du service (deploiement, crash, etc.).
MEMOIRE_RESERVATIONS: dict[str, dict] = {}
DUREE_EXPIRATION_SECONDES = 60 * 60  # 1 heure

# Memoire courte pour le cas "plusieurs reservations trouvees lors d'une
# annulation" : on stocke les codes Ref proposes + leur event_id, pour que
# la reponse suivante du client (meme si c'est juste le code seul, sans le
# mot "annuler") soit reconnue directement sans repasser par l'IA.
# Format : { "+336...": {"options": {"REF1": "event_id1", ...}, "expire": ts} }
MEMOIRE_ANNULATION_EN_ATTENTE: dict[str, dict] = {}
DUREE_ATTENTE_ANNULATION_SECONDES = 15 * 60  # 15 minutes

# Anti-abus : nombre maximum de reservations futures autorisees en meme
# temps pour un seul numero de telephone.
MAX_RESERVATIONS_ACTIVES = 5


def charger_memoire_reservations() -> None:
    """Recharge MEMOIRE_RESERVATIONS depuis le fichier JSON persistant au
    demarrage du service, pour ne pas perdre les reservations en cours de
    saisie en cas de redemarrage (deploiement, crash, etc.)."""
    if not os.path.isfile(FICHIER_MEMOIRE_RESERVATIONS):
        return
    try:
        with open(FICHIER_MEMOIRE_RESERVATIONS, "r", encoding="utf-8") as f:
            donnees_disque = json.load(f)
        maintenant = time.time()
        for numero, entree in donnees_disque.items():
            # On ignore les entrees deja expirees pour ne pas les recharger
            # inutilement (redemande une reservation perimee).
            if maintenant - entree.get("derniere_maj", 0) <= DUREE_EXPIRATION_SECONDES:
                MEMOIRE_RESERVATIONS[numero] = entree
        log.info(
            "Memoire des reservations rechargee depuis %s (%d entrees actives)",
            FICHIER_MEMOIRE_RESERVATIONS, len(MEMOIRE_RESERVATIONS),
        )
    except (OSError, json.JSONDecodeError) as e:
        log.error("Echec du rechargement de la memoire des reservations : %s", e)


def persister_memoire_reservations() -> None:
    """Ecrit MEMOIRE_RESERVATIONS sur disque. Appele apres chaque
    modification pour survivre a un redemarrage du service."""
    try:
        with open(FICHIER_MEMOIRE_RESERVATIONS, "w", encoding="utf-8") as f:
            json.dump(MEMOIRE_RESERVATIONS, f, ensure_ascii=False)
    except OSError as e:
        log.error("Echec de la sauvegarde de la memoire des reservations : %s", e)


charger_memoire_reservations()


def recuperer_entree(numero: str) -> dict | None:
    """Renvoie l'entree memorisee pour ce numero (donnees + statut complete)
    si elle n'a pas expire (plus d'1h sans nouveau message), sinon None."""
    entree = MEMOIRE_RESERVATIONS.get(numero)
    if entree is None:
        return None
    if time.time() - entree["derniere_maj"] > DUREE_EXPIRATION_SECONDES:
        MEMOIRE_RESERVATIONS.pop(numero, None)
        persister_memoire_reservations()
        return None
    return entree


def sauvegarder_entree(
    numero: str,
    donnees: dict,
    complete: bool,
    event_id: str | None = None,
    reference: str | None = None,
) -> None:
    ancienne = MEMOIRE_RESERVATIONS.get(numero, {})
    MEMOIRE_RESERVATIONS[numero] = {
        "donnees": donnees,
        "derniere_maj": time.time(),
        "complete": complete,
        # On garde l'event_id/reference existants si non fournis
        # explicitement, pour ne pas les perdre lors des mises a jour
        # intermediaires.
        "event_id": event_id if event_id is not None else ancienne.get("event_id"),
        "reference": reference if reference is not None else ancienne.get("reference"),
    }
    persister_memoire_reservations()


def extraire_reservation(message: str, connu: dict | None = None) -> dict | None:
    """Appelle Claude Haiku pour extraire/mettre a jour les infos de
    reservation, en lui donnant le contexte deja connu (infos des messages
    precedents de ce numero) pour qu'elle fasse la fusion intelligemment
    plutot que de repartir de zero a chaque SMS."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY non configuree - extraction IA impossible")
        return None

    maintenant = datetime.now(FUSEAU_HORAIRE)
    contenu_utilisateur = (
        f"Date et heure actuelles (Nice, France) : "
        f"{maintenant.strftime('%A %d %B %Y, %H:%M')}\n\n"
        f"Informations deja connues sur cette reservation : "
        f"{json.dumps(connu or {}, ensure_ascii=False)}\n\n"
        f"Nouveau message du client : {message}"
    )

    try:
        reponse = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": PROMPT_SYSTEME,
                "messages": [{"role": "user", "content": contenu_utilisateur}],
            },
            timeout=20,
        )
    except requests.RequestException as e:
        log.error("Echec extraction IA (reseau) : %s", e)
        return None

    if reponse.status_code >= 300:
        log.error(
            "Echec extraction IA (statut %s) : %s",
            reponse.status_code,
            reponse.text[:500],
        )
        return None

    try:
        corps = reponse.json()
        texte = corps["content"][0]["text"].strip()
        # Le modele ajoute parfois des balises markdown (```json ... ```)
        # autour du JSON malgre la consigne ; on les retire si presentes.
        if texte.startswith("```"):
            texte = texte.split("```")[1]
            if texte.startswith("json"):
                texte = texte[4:]
            texte = texte.strip()
        return json.loads(texte)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.error(
            "Echec extraction IA (reponse inattendue) : %s -- corps recu : %s",
            e,
            reponse.text[:500],
        )
        return None


# Adresses completes verifiees des principaux etablissements de sante de
# la region de Nice, utilisees UNIQUEMENT pour ameliorer la precision du
# calcul de trajet (Distance Matrix) quand le client ecrit juste un nom
# court/raccourci (ex: "tzanck") que Google Maps ne localise pas de facon
# fiable tout seul. Le texte affiche au client dans les SMS reste celui
# qu'il a lui-meme ecrit, cette table ne sert qu'en coulisses.
ADRESSES_ETABLISSEMENTS_SANTE = {
    "les sources": "Hopital Les Sources, 10 chemin Rene Pietruschi, 06100 Nice",
    "saint-george": "Clinique Saint George, 2 avenue de Rimiez, 06100 Nice",
    "saint george": "Clinique Saint George, 2 avenue de Rimiez, 06100 Nice",
    "pasteur": "Hopital Pasteur, 30 avenue de la Voie Romaine, 06000 Nice",
    "archet": "Hopital de l'Archet, 151 route Saint-Antoine de Ginestiere, 06200 Nice",
    "lenval": "Hopitaux Pediatriques de Nice CHU-Lenval, 57 avenue de la Californie, 06200 Nice",
    "antoine lacassagne": "Centre Antoine Lacassagne, 33 avenue de Valombrose, 06189 Nice",
    "lacassagne": "Centre Antoine Lacassagne, 33 avenue de Valombrose, 06189 Nice",
    "parc imperial": "Clinique du Parc Imperial, 28 boulevard du Tzarewitch, 06000 Nice",
    "saint-antoine": "Clinique Saint-Antoine, 7 avenue Durante, 06000 Nice",
    "saint antoine": "Clinique Saint-Antoine, 7 avenue Durante, 06000 Nice",
    "santa maria": "Polyclinique Santa Maria, 57 avenue de la Californie, 06200 Nice",
    "saint-francois": "Clinique Saint-Francois, 10 boulevard Pasteur, 06000 Nice",
    "saint francois": "Clinique Saint-Francois, 10 boulevard Pasteur, 06000 Nice",
    "cimiez": "Hopital Cimiez, 4 avenue Reine Victoria, 06003 Nice",
    "saint jean": "Polyclinique Saint Jean, 92 avenue du Docteur Maurice Donat, 06800 Cagnes-sur-Mer",
    "tzanck": "Institut Arnault Tzanck, 231 avenue du Docteur Maurice Donat, 06721 Saint-Laurent-du-Var",
    "crc nice": "Institut Arnault Tzanck, 231 avenue du Docteur Maurice Donat, 06721 Saint-Laurent-du-Var",
}


def resoudre_adresse_medicale(adresse: str) -> str:
    """Si l'adresse contient un raccourci d'etablissement de sante connu
    (ex: 'tzanck', 'les sources'), renvoie l'adresse complete verifiee pour
    ameliorer la fiabilite du geocodage Google Maps. Sinon renvoie l'adresse
    telle quelle."""
    adresse_minuscule = (adresse or "").lower()
    for cle, adresse_complete in ADRESSES_ETABLISSEMENTS_SANTE.items():
        if cle in adresse_minuscule:
            return adresse_complete
    return adresse


# Villes courantes autour de Nice (+ quelques grandes villes francaises que
# les clients mentionnent parfois explicitement) : si aucune de ces villes
# ni aucun code postal n'apparait dans une adresse, on suppose qu'elle se
# trouve a Nice (siege de l'activite), pour eviter que Google Maps ne la
# geocode par erreur sur une rue homonyme d'une autre ville (ex: il existe
# un "Boulevard Gambetta" dans plusieurs villes de France).
VILLES_CONNUES = [
    "nice", "cagnes-sur-mer", "cagnes sur mer", "saint-laurent-du-var",
    "saint laurent du var", "antibes", "cannes", "grasse", "vence", "menton",
    "villeneuve-loubet", "villeneuve loubet", "beaulieu", "villefranche",
    "carros", "mougins", "valbonne", "biot", "roquefort", "marseille",
    "paris", "lyon", "toulon", "monaco", "aix-en-provence", "aix en provence",
]


def completer_adresse_avec_ville(adresse: str) -> str:
    """Ajoute ', Nice, France' si l'adresse ne mentionne ni ville connue ni
    code postal, pour fiabiliser le geocodage Google Maps."""
    adresse_minuscule = (adresse or "").lower()
    if re.search(r"\b\d{5}\b", adresse_minuscule):
        return adresse
    if any(ville in adresse_minuscule for ville in VILLES_CONNUES):
        return adresse
    return f"{adresse}, Nice, France"


def estimer_duree_trajet(origine: str, destination: str) -> int | None:
    """Estime la duree du trajet en minutes entre deux adresses via l'API
    Google Distance Matrix. Renvoie None si indisponible ou en echec."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        reponse = requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": origine,
                "destinations": destination,
                "region": "fr",
                "language": "fr",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=10,
        )
        corps = reponse.json()
        element = corps["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            log.warning("Distance Matrix statut non-OK pour %s -> %s : %s", origine, destination, element.get("status"))
            return None
        return round(element["duration"]["value"] / 60)
    except Exception as e:
        log.error("Erreur estimation trajet %s -> %s : %s", origine, destination, e)
        return None


def parser_heure_texte(texte: str) -> tuple[int, int] | None:
    """Extrait une heure/minute d'un texte du type '14h', '14h30', '14:30'."""
    trouve = re.search(r"(\d{1,2})\s*[h:]\s*(\d{2})?", texte or "")
    if not trouve:
        return None
    heure = int(trouve.group(1))
    minute = int(trouve.group(2)) if trouve.group(2) else 0
    if 0 <= heure <= 23 and 0 <= minute <= 59:
        return heure, minute
    return None


def libelle_date_relative(dt: datetime) -> str:
    """Renvoie 'aujourd'hui', 'demain', 'apres-demain' ou une date ecrite
    (ex: 'le 20/07'), selon l'ecart avec la date du jour."""
    aujourd_hui = datetime.now(FUSEAU_HORAIRE).date()
    ecart = (dt.date() - aujourd_hui).days
    if ecart == 0:
        return "aujourd'hui"
    if ecart == 1:
        return "demain"
    if ecart == 2:
        return "apres-demain"
    return dt.strftime("le %d/%m")


def construire_reponse(
    donnees: dict, reference: str = "", heure_estimee: bool = False,
    premier_message: bool = False,
) -> str:
    """Construit le SMS de reponse selon que la reservation est complete ou non.
    Si premier_message est True, la reponse commence par une courte
    presentation de Kelly (uniquement pour le tout premier message d'une
    nouvelle conversation, pas les relances suivantes)."""
    manquants = [
        champ
        for champ in CHAMPS_OBLIGATOIRES
        if not donnees.get(champ)
    ]
    intro = "Bonjour, je suis Kelly, la secretaire de la Centrale des Taxis Nicois. " if premier_message else ""

    if manquants:
        # Cas particulier frequent en medical : le client a donne l'heure de
        # son rendez-vous mais pas l'heure a laquelle le chauffeur doit
        # venir -> on le precise clairement pour eviter la confusion.
        if manquants == ["heure"] and donnees.get("heure_rdv"):
            return (
                f"{intro}Votre rendez-vous est note a {donnees['heure_rdv']}. "
                "A quelle heure souhaitez-vous que le chauffeur vienne vous chercher ?"
            )
        libelles = [CHAMPS_OBLIGATOIRES[c] for c in manquants]
        return f"{intro}Pour reserver votre taxi, il me manque juste :\n" + "\n".join(
            f"- {libelle}" for libelle in libelles
        )

    nom = donnees["nom"]
    depart = donnees["prise_en_charge"]
    destination = donnees["destination"]

    # On precise toujours la date (aujourd'hui/demain/apres-demain/date
    # exacte) en plus de l'heure, pour eviter toute ambiguite -- calculee a
    # partir de heure_iso qui contient la date exacte resolue par l'IA.
    try:
        debut_dt = datetime.fromisoformat(donnees["heure_iso"]) if donnees.get("heure_iso") else None
    except ValueError:
        debut_dt = None

    if debut_dt:
        moment = f"{libelle_date_relative(debut_dt)} a {debut_dt.strftime('%Hh%M')}"
    else:
        moment = donnees["heure"]

    reponse = (
        f"Reservation confirmee pour M. {nom} : prise en charge {moment} "
        f"au {depart}, direction {destination}. "
        "Un chauffeur vous contactera peu avant son arrivee."
    )
    if heure_estimee:
        reponse += " (Heure de prise en charge estimee selon le trajet, sera ajustee si besoin.)"
    if reference:
        reponse += f" Ref: {reference} (a rappeler pour annuler)."
    return reponse


def generer_reference() -> str:
    """Genere un code de reference court et facile a lire/dicter par SMS
    (pas de 0/O ni 1/I pour eviter les confusions)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(alphabet, k=6))


def extraire_reference_de_description(description: str) -> str:
    """Recupere le code de reference ecrit dans la description d'un
    evenement Google Agenda (ligne 'REF : XXXXXX')."""
    trouve = re.search(r"REF\s*:\s*([A-Z0-9]+)", description or "", re.IGNORECASE)
    return trouve.group(1).upper() if trouve else "?"


def extraire_champ_de_description(description: str, libelle: str) -> str:
    """Recupere la valeur d'un champ donne (ex: 'DEST', 'PC') ecrit sur sa
    propre ligne dans la description d'un evenement Google Agenda."""
    trouve = re.search(rf"^{libelle}\s*:\s*(.+)$", description or "", re.IGNORECASE | re.MULTILINE)
    return trouve.group(1).strip() if trouve else "?"


def construire_recap_depuis_evenement(ev: dict) -> str:
    """Construit un message de confirmation lisible a partir des infos
    stockees dans la description d'un evenement Google Agenda existant."""
    description = ev.get("description", "")
    pc = extraire_champ_de_description(description, "PC")
    dest = extraire_champ_de_description(description, "DEST")
    rdv = extraire_champ_de_description(description, "RDV")
    return f"Oui, c'est bien prevu : prise en charge {pc}, direction {dest} ({rdv})."


def lister_evenements_du_jour(date_cible: "date") -> list[dict]:
    """Liste tous les evenements Google Agenda dont le debut tombe le jour
    donne (utilise pour les rappels J-1 aux clients)."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return []
    try:
        service = _construire_service_agenda()
        debut_jour = datetime.combine(date_cible, datetime.min.time(), tzinfo=FUSEAU_HORAIRE)
        fin_jour = debut_jour + timedelta(days=1)
        resultat = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=debut_jour.isoformat(),
                timeMax=fin_jour.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return resultat.get("items", [])
    except Exception as e:
        log.error("Erreur listing evenements du jour (%s) : %s", date_cible, e)
        return []


def _construire_service_agenda():
    infos_compte = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        infos_compte, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


def rechercher_evenements(texte_recherche: str, seulement_futur: bool = True) -> list[dict]:
    """Recherche les evenements Google Agenda contenant ce texte (numero de
    telephone ou code de reference) dans leur contenu. Utilise pour
    retrouver une reservation a annuler."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return []
    try:
        service = _construire_service_agenda()
        parametres = {
            "calendarId": GOOGLE_CALENDAR_ID,
            "q": texte_recherche,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if seulement_futur:
            parametres["timeMin"] = datetime.now(FUSEAU_HORAIRE).isoformat()
        resultat = service.events().list(**parametres).execute()
        return resultat.get("items", [])
    except Exception as e:
        log.error("Erreur recherche evenements Agenda : %s", e)
        return []


def creer_evenement_agenda(
    donnees: dict, numero_expediteur: str = "", reference: str = ""
) -> tuple[bool, str, str | None]:
    """Cree l'evenement correspondant dans Google Agenda via un compte de
    service. Renvoie (succes, message_ou_lien, event_id)."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return False, "Google Agenda non configure (variables manquantes sur Railway)", None

    if not donnees.get("heure_iso"):
        return False, "Pas de date/heure exacte disponible (heure_iso manquant)", None

    try:
        debut_dt = datetime.fromisoformat(donnees["heure_iso"])
    except ValueError as e:
        return False, f"Date/heure invalide ({donnees['heure_iso']}) : {e}", None

    fin_dt = debut_dt + timedelta(hours=1)
    type_label = "MEDICAL" if donnees.get("type") == "medical" else "PRIVE"
    type_tag = "[MED]" if type_label == "MEDICAL" else "[PRIVE]"
    # Si le client n'a pas donne un numero different, on utilise celui qui a
    # envoye le SMS -- c'est le seul moyen de le recontacter dans ce cas.
    telephone = donnees.get("telephone") or numero_expediteur or "(non renseigne)"
    heure_aff = debut_dt.strftime("%Hh%M")
    reference = reference or generer_reference()

    # Le champ RDV doit afficher l'heure REELLE du rendez-vous (heure_rdv),
    # differente de l'heure de prise en charge (PC) quand elle a ete estimee
    # a partir du trajet. Si aucune heure_rdv n'est connue (course privee
    # sans rendez-vous a proprement parler), PC et RDV sont la meme chose.
    heure_rdv_minute = parser_heure_texte(donnees.get("heure_rdv") or "")
    heure_rdv_aff = (
        f"{heure_rdv_minute[0]:02d}h{heure_rdv_minute[1]:02d}" if heure_rdv_minute else heure_aff
    )

    titre = (
        f"PC {heure_aff} M. {donnees['nom']} | "
        f"PC : {donnees['prise_en_charge']} | "
        f"DEST : {donnees['destination']} | "
        f"RDV : {heure_rdv_aff} {type_tag} | "
        f"TEL : {telephone} | REF : {reference}"
    ).upper()
    description = (
        f"REF : {reference}\n"
        f"PC : {donnees['prise_en_charge']}\n"
        f"DEST : {donnees['destination']}\n"
        f"RDV : {heure_rdv_aff} {type_tag}\n"
        f"TEL : {telephone}"
    ).upper()

    try:
        service = _construire_service_agenda()
        evenement = {
            "summary": titre,
            "description": description,
            "start": {"dateTime": debut_dt.isoformat(), "timeZone": "Europe/Paris"},
            "end": {"dateTime": fin_dt.isoformat(), "timeZone": "Europe/Paris"},
            # colorId 5 = "Banana" (jaune) dans la palette Google Agenda,
            # pour distinguer d'un coup d'oeil les reservations venues du SMS.
            "colorId": "5",
        }
        resultat = (
            service.events()
            .insert(calendarId=GOOGLE_CALENDAR_ID, body=evenement)
            .execute()
        )
        return True, resultat.get("htmlLink", "evenement cree"), resultat.get("id")
    except Exception as e:
        return False, str(e), None


def supprimer_evenement_agenda(event_id: str) -> tuple[bool, str]:
    """Supprime un evenement de Google Agenda a partir de son ID."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return False, "Google Agenda non configure (variables manquantes sur Railway)"
    try:
        service = _construire_service_agenda()
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        return True, "evenement supprime"
    except Exception as e:
        return False, str(e)


def envoyer_email_confirmation(donnees: dict, numero_expediteur: str, reference: str) -> tuple[bool, str]:
    """Envoie un email de confirmation de reservation via l'API Resend
    (HTTPS, contrairement au SMTP qui est bloque en sortie sur Railway)."""
    if not (RESEND_API_KEY and EMAIL_DESTINATAIRE):
        return False, "Email non configure (variables manquantes sur Railway)"

    type_label = "MEDICAL" if donnees.get("type") == "medical" else "PRIVE"
    telephone = donnees.get("telephone") or numero_expediteur or "(non renseigne)"

    try:
        debut_dt = datetime.fromisoformat(donnees["heure_iso"]) if donnees.get("heure_iso") else None
    except ValueError:
        debut_dt = None
    moment = (
        f"{libelle_date_relative(debut_dt)} a {debut_dt.strftime('%Hh%M')}"
        if debut_dt else donnees.get("heure", "")
    )

    corps = (
        f"Nouvelle reservation SMS confirmee\n\n"
        f"Reference : {reference}\n"
        f"Type : {type_label}\n"
        f"Nom : {donnees['nom']}\n"
        f"Telephone : {telephone}\n"
        f"Prise en charge : {moment} - {donnees['prise_en_charge']}\n"
        f"Destination : {donnees['destination']}\n"
    )

    try:
        reponse = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                # Domaine de test fourni par Resend, fonctionne sans
                # verification de domaine mais uniquement vers l'adresse
                # email utilisee pour creer le compte Resend.
                "from": "EasyTaxi <onboarding@resend.dev>",
                "to": [EMAIL_DESTINATAIRE],
                "subject": f"Reservation taxi - {donnees['nom']} - Ref {reference}",
                "text": corps,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        return False, f"Erreur reseau : {e}"

    if reponse.status_code >= 300:
        return False, f"Statut {reponse.status_code} : {reponse.text[:300]}"
    return True, "email envoye"


if GATEWAY_MODE == "local":
    SEND_URL = f"{LOCAL_URL.rstrip('/')}/3rdparty/v1/messages"
else:
    SEND_URL = "https://api.sms-gate.app/3rdparty/v1/messages"


def verifier_signature(corps_brut: bytes, timestamp: str, signature: str) -> bool:
    """Verifie la signature HMAC-SHA256 envoyee par l'app SMS Gateway."""
    if not SIGNING_KEY:
        # Pas de cle configuree -> on ne bloque pas en phase de test,
        # mais il faut absolument la configurer avant la mise en prod.
        log.warning("SMS_GATEWAY_SIGNING_KEY non configuree - signature non verifiee")
        return True
    message = corps_brut + timestamp.encode()
    attendu = hmac.new(SIGNING_KEY.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(attendu, signature or "")


def envoyer_sms(numero: str, texte: str) -> None:
    """Envoie une reponse SMS via l'API SMS Gateway."""
    try:
        reponse = requests.post(
            f"{SEND_URL}?skipPhoneValidation=true",
            json={
                "textMessage": {"text": texte},
                "phoneNumbers": [numero],
                "priority": 100,
            },
            auth=(GATEWAY_USERNAME, GATEWAY_PASSWORD),
            timeout=15,
        )
        log.info("Envoi SMS a %s -> statut %s : %s", numero, reponse.status_code, reponse.text)
    except requests.RequestException as e:
        log.error("Echec envoi SMS a %s : %s", numero, e)


@app.route("/webhook/sms", methods=["POST"])
def webhook_sms():
    corps_brut = request.get_data()
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")

    if not verifier_signature(corps_brut, timestamp, signature):
        log.warning("Signature invalide, requete ignoree")
        return jsonify({"error": "signature invalide"}), 401

    donnees = request.get_json(silent=True) or {}
    if donnees.get("event") != "sms:received":
        # On ignore les autres types d'evenements (sms:sent, sms:delivered, etc.)
        return jsonify({"status": "ignore"}), 200

    payload = donnees.get("payload", {})
    expediteur = payload.get("sender", "")
    message = payload.get("message", "")

    log.info("SMS recu de %s : %s", expediteur, message)

    if expediteur and message:
        # Commande rapide "RDV" : permet de creer une reservation en un
        # seul message (utile quand un chauffeur tape lui-meme la demande
        # d'un client, ex: dans la voiture). Toujours traite sans memoire
        # (stateless) pour ne jamais bloquer/melanger plusieurs reservations
        # tapees a la suite depuis le meme telephone. Le telephone du
        # client est obligatoire ici (contrairement au flux normal ou on le
        # deduit du numero expediteur), puisque l'expediteur est celui qui
        # tape la commande, pas forcement le client.
        message_sans_espaces = message.strip()
        if re.match(r"^rdv\b", message_sans_espaces, re.IGNORECASE) and expediteur in ADMIN_PHONE_NUMBERS:
            contenu_rdv = re.sub(r"^rdv\b", "", message_sans_espaces, count=1, flags=re.IGNORECASE).strip()
            if not contenu_rdv:
                texte_reponse = (
                    "Rendez Vous Admin :\n\n"
                    "Nom : \n"
                    "Tel :\n"
                    "Date :\n"
                    "PC : \n"
                    "DEST :\n"
                    "Heure PC : \n"
                    "Heure RDV :"
                )
                envoyer_sms(expediteur, texte_reponse)
                return jsonify({"status": "ok"}), 200

            donnees_rdv = extraire_reservation(contenu_rdv, {})
            if donnees_rdv is None:
                envoyer_sms(expediteur, "Erreur technique lors de l'analyse, merci de reessayer.")
                return jsonify({"status": "ok"}), 200

            for cle_meta in (
                "est_question", "plusieurs_courses", "annulation", "reference_annulation",
                "confirmation_existante", "reference_lookup",
            ):
                donnees_rdv.pop(cle_meta, None)

            champs_manquants_rdv = [c for c in CHAMPS_OBLIGATOIRES if not donnees_rdv.get(c)]
            if not donnees_rdv.get("telephone"):
                champs_manquants_rdv.append("telephone")

            if champs_manquants_rdv:
                libelles_rdv = [CHAMPS_OBLIGATOIRES.get(c, "le telephone du client") for c in champs_manquants_rdv]
                texte_reponse = "Il manque encore :\n" + "\n".join(f"- {l}" for l in libelles_rdv)
                envoyer_sms(expediteur, texte_reponse)
                return jsonify({"status": "ok"}), 200

            reference_rdv = generer_reference()
            succes_rdv, detail_rdv, event_id_rdv = creer_evenement_agenda(
                donnees_rdv, numero_expediteur=expediteur, reference=reference_rdv,
            )
            if succes_rdv:
                texte_reponse = construire_reponse(donnees_rdv, reference_rdv)
            else:
                log.error("Echec creation evenement Agenda (commande RDV) pour %s : %s", expediteur, detail_rdv)
                texte_reponse = f"Erreur lors de la creation de la reservation : {detail_rdv}"
            envoyer_sms(expediteur, texte_reponse)
            return jsonify({"status": "ok"}), 200

        # Priorite absolue : si on avait propose un choix de reservations a
        # annuler, et que ce nouveau message correspond a l'un des codes
        # Ref proposes (meme envoye seul, sans le mot "annuler"), on traite
        # ca directement sans repasser par l'IA -- plus fiable.
        attente = MEMOIRE_ANNULATION_EN_ATTENTE.get(expediteur)
        if attente and time.time() < attente["expire"]:
            message_normalise = message.strip().upper()
            ref_trouvee = next(
                (ref for ref in attente["options"] if ref in message_normalise), None
            )
            if ref_trouvee:
                event_id = attente["options"][ref_trouvee]
                succes, detail = supprimer_evenement_agenda(event_id)
                MEMOIRE_ANNULATION_EN_ATTENTE.pop(expediteur, None)
                if succes:
                    texte_reponse = f"Votre reservation (Ref: {ref_trouvee}) a bien ete annulee."
                    # Si la reservation memorisee pour ce numero correspond a
                    # celle qu'on vient d'annuler, on la retire de la memoire
                    # pour eviter que le bot ne la reconfirme par erreur au
                    # prochain message (ex: un simple "bonjour").
                    entree_courante = recuperer_entree(expediteur)
                    if entree_courante and entree_courante.get("reference") == ref_trouvee:
                        MEMOIRE_RESERVATIONS.pop(expediteur, None)
                        persister_memoire_reservations()
                else:
                    log.error("Echec suppression evenement (choix ref %s) : %s", ref_trouvee, detail)
                    texte_reponse = "Erreur technique lors de l'annulation, merci de nous rappeler directement."
                envoyer_sms(expediteur, texte_reponse)
                return jsonify({"status": "ok"}), 200
            # Le message ne correspond a aucune des options proposees -> on
            # abandonne l'attente et on traite le message normalement.
            MEMOIRE_ANNULATION_EN_ATTENTE.pop(expediteur, None)

        entree_existante = recuperer_entree(expediteur)
        donnees_existantes = entree_existante["donnees"] if entree_existante else {}

        # Pour un numero admin, une fois la course precedente complete, on
        # ne transmet plus son contexte a l'IA : chaque nouvelle course
        # tapee par l'admin doit repartir de zero, jamais se meler aux
        # infos de la course precedente (nom, adresse, heure...).
        contexte_extraction = donnees_existantes
        if expediteur in ADMIN_PHONE_NUMBERS and entree_existante and entree_existante.get("complete"):
            contexte_extraction = {}

        donnees_extraites = extraire_reservation(message, contexte_extraction)

        if donnees_extraites is None:
            # Echec de l'IA (cle manquante, erreur reseau, etc.) -> on previent
            # quand meme le client au lieu de rester silencieux.
            texte_reponse = (
                "Bien recu, votre demande de reservation a ete notee. "
                "Un chauffeur vous recontactera pour confirmer."
            )
        else:
            log.info("Extraction IA (avec contexte) : %s", donnees_extraites)
            est_question = donnees_extraites.pop("est_question", False)
            plusieurs_courses = donnees_extraites.pop("plusieurs_courses", False)
            annulation = donnees_extraites.pop("annulation", False)
            confirmation_existante = donnees_extraites.pop("confirmation_existante", False)
            reference_lookup = (donnees_extraites.pop("reference_lookup", None) or "").strip().upper()
            reclamation = donnees_extraites.pop("reclamation", False)

            # Garde-fou : l'IA marque parfois est_question=true alors que le
            # message apporte quand meme de nouvelles infos exploitables
            # (ex: "je suis M. X, adresse Y, rdv a Zh, vous partez a quelle
            # heure ?"). On ne prend le raccourci "juste une question" que
            # si aucun champ nouveau n'a ete apporte par rapport a ce qu'on
            # savait deja -- sinon on traite l'info normalement malgre le
            # tour de phrase interrogatif.
            apporte_info_nouvelle = any(
                valeur and donnees_extraites.get(champ) != donnees_existantes.get(champ)
                for champ, valeur in donnees_extraites.items()
            )

            if reclamation:
                # Le client signale un probleme (taxi pas venu, retard,
                # etc.) -> priorite absolue sur tout le reste. On repond de
                # facon humaine et on alerte immediatement l'administrateur
                # par SMS pour qu'il puisse rappeler le client lui-meme.
                log.info("Reclamation detectee pour %s : %s", expediteur, message)
                texte_reponse = (
                    "Je suis vraiment desolee pour ce desagrement. "
                    "Je transmets immediatement votre message a un responsable "
                    "qui va vous recontacter au plus vite."
                )
                for numero_admin in ADMIN_PHONE_NUMBERS:
                    envoyer_sms(
                        numero_admin,
                        f"RECLAMATION de {expediteur} : {message}",
                    )
                envoyer_sms(expediteur, texte_reponse)
                return jsonify({"status": "ok"}), 200

            if annulation:
                # Le client demande d'annuler une reservation.
                reference_citee = (donnees_extraites.get("reference_annulation") or "").strip().upper()

                if reference_citee:
                    # Le client a cite un code precis -> on cherche cet
                    # evenement precis dans Google Agenda.
                    evenements = rechercher_evenements(reference_citee, seulement_futur=False)
                    evenements = [
                        e for e in evenements
                        if extraire_reference_de_description(e.get("description", "")) == reference_citee
                    ]
                    if len(evenements) == 1:
                        succes, detail = supprimer_evenement_agenda(evenements[0]["id"])
                        if succes:
                            texte_reponse = f"Votre reservation (Ref: {reference_citee}) a bien ete annulee."
                        else:
                            log.error("Echec suppression evenement (ref %s) : %s", reference_citee, detail)
                            texte_reponse = "Erreur technique lors de l'annulation, merci de nous rappeler directement."
                        if entree_existante and entree_existante.get("reference") == reference_citee:
                            MEMOIRE_RESERVATIONS.pop(expediteur, None)
                            persister_memoire_reservations()
                    else:
                        texte_reponse = (
                            f"Reference {reference_citee} introuvable. "
                            "Verifiez le code Ref recu par SMS lors de la confirmation."
                        )
                else:
                    # Pas de code cite -> on cherche toutes les reservations
                    # futures liees a ce numero de telephone dans l'agenda.
                    evenements = rechercher_evenements(expediteur, seulement_futur=True)
                    if not evenements:
                        texte_reponse = "Nous n'avons pas de reservation en cours a annuler pour ce numero."
                    elif len(evenements) == 1:
                        ref_trouvee = extraire_reference_de_description(evenements[0].get("description", ""))
                        succes, detail = supprimer_evenement_agenda(evenements[0]["id"])
                        if succes:
                            texte_reponse = "Votre reservation a bien ete annulee. N'hesitez pas a nous recontacter si besoin."
                        else:
                            log.error("Echec suppression evenement pour %s : %s", expediteur, detail)
                            texte_reponse = "Erreur technique lors de l'annulation, merci de nous rappeler directement."
                        MEMOIRE_RESERVATIONS.pop(expediteur, None)
                        persister_memoire_reservations()
                    else:
                        # Plusieurs reservations trouvees -> on les liste
                        # avec leur reference, et on memorise les options
                        # pour reconnaitre la reponse suivante du client
                        # meme si elle ne contient que le code seul.
                        lignes = []
                        options = {}
                        for i, ev in enumerate(evenements[:5], start=1):
                            description_ev = ev.get("description", "")
                            ref = extraire_reference_de_description(description_ev)
                            destination_ev = extraire_champ_de_description(description_ev, "DEST")
                            options[ref] = ev["id"]
                            debut_iso = ev.get("start", {}).get("dateTime", "")
                            try:
                                date_aff = datetime.fromisoformat(debut_iso).strftime("%d/%m a %Hh%M")
                            except ValueError:
                                date_aff = debut_iso
                            lignes.append(f"{i}) {ref} - {date_aff} - {destination_ev}")
                        MEMOIRE_ANNULATION_EN_ATTENTE[expediteur] = {
                            "options": options,
                            "expire": time.time() + DUREE_ATTENTE_ANNULATION_SECONDES,
                        }
                        texte_reponse = (
                            "Vos reservations en cours :\n"
                            + "\n".join(lignes)
                            + "\n\nRepondez avec le code Ref de celle a annuler (ex: 'annulez ABC123')."
                        )
            elif confirmation_existante:
                # Le client demande une confirmation d'un rendez-vous deja
                # convenu (pas une nouvelle demande de reservation).
                if reference_lookup:
                    evenements = rechercher_evenements(reference_lookup, seulement_futur=False)
                    evenements = [
                        e for e in evenements
                        if extraire_reference_de_description(e.get("description", "")) == reference_lookup
                    ]
                    if len(evenements) == 1:
                        texte_reponse = construire_recap_depuis_evenement(evenements[0])
                    else:
                        texte_reponse = (
                            f"Reference {reference_lookup} introuvable. "
                            "Verifiez le code Ref recu par SMS lors de la confirmation."
                        )
                else:
                    evenements = rechercher_evenements(expediteur, seulement_futur=True)
                    if not evenements:
                        texte_reponse = (
                            "Nous n'avons pas de reservation en cours pour ce numero. "
                            "Merci de nous communiquer le numero de reference (Ref) recu par SMS."
                        )
                    elif len(evenements) == 1:
                        texte_reponse = construire_recap_depuis_evenement(evenements[0])
                    else:
                        texte_reponse = (
                            "Merci de nous communiquer le numero de reference (Ref) "
                            "de la reservation concernee, recu par SMS lors de la confirmation."
                        )
            elif plusieurs_courses:
                # Le client decrit plusieurs trajets distincts dans le meme
                # SMS -> on lui demande de les envoyer un par un, on ne
                # touche pas a la memoire existante.
                log.info("Demande multi-courses detectee pour %s", expediteur)
                texte_reponse = (
                    "Merci de nous envoyer une seule course a la fois : "
                    "un SMS par trajet (depart, destination, heure). "
                    "Renvoyez-nous d'abord la premiere course."
                )
            elif est_question and entree_existante and not apporte_info_nouvelle:
                # Le SMS est une question/remarque de suivi (ex: "a quelle
                # heure venez-vous ?"), pas une nouvelle info -> on rappelle
                # la reservation en cours plutot que de la modifier.
                log.info("Question de suivi detectee pour %s, rappel de la reservation", expediteur)
                texte_reponse = construire_reponse(donnees_existantes)
                sauvegarder_entree(expediteur, donnees_existantes, entree_existante["complete"])
            else:
                # L'IA a deja fusionne les infos connues avec le nouveau
                # message (elle recoit le contexte), on utilise son resultat
                # directement.
                donnees_completes = donnees_extraites
                log.info("Donnees cumulees pour %s : %s", expediteur, donnees_completes)

                heure_estimee = False
                if (
                    not donnees_completes.get("heure")
                    and donnees_completes.get("heure_rdv")
                    and donnees_completes.get("date")
                    and donnees_completes.get("prise_en_charge")
                    and donnees_completes.get("destination")
                ):
                    # Le client connait son heure de rendez-vous mais pas
                    # l'heure de prise en charge -> on l'estime a partir du
                    # temps de trajet reel + une marge de securite.
                    heure_minute = parser_heure_texte(donnees_completes["heure_rdv"])
                    duree_trajet = estimer_duree_trajet(
                        completer_adresse_avec_ville(resoudre_adresse_medicale(donnees_completes["prise_en_charge"])),
                        completer_adresse_avec_ville(resoudre_adresse_medicale(donnees_completes["destination"])),
                    )
                    if heure_minute and duree_trajet is not None:
                        heure_rdv_h, heure_rdv_m = heure_minute
                        date_rdv = datetime.fromisoformat(donnees_completes["date"]).replace(
                            hour=heure_rdv_h, minute=heure_rdv_m, tzinfo=FUSEAU_HORAIRE
                        )
                        # Marge de securite adaptee a la longueur du trajet :
                        # 15 min pour les petites courses (< 30 min de trajet),
                        # 30 min pour les grosses courses (>= 30 min de trajet).
                        marge_securite_minutes = 15 if duree_trajet < 30 else 30
                        heure_pc_dt = date_rdv - timedelta(minutes=duree_trajet + marge_securite_minutes)

                        # Arrondi au multiple de 5 minutes le plus proche
                        # (ex: 11h34 -> 11h35, 11h33 -> 11h30).
                        minutes_totales = heure_pc_dt.hour * 60 + heure_pc_dt.minute
                        minutes_arrondies = round(minutes_totales / 5) * 5
                        heure_pc_dt = heure_pc_dt.replace(hour=0, minute=0) + timedelta(minutes=minutes_arrondies)

                        donnees_completes["heure_iso"] = heure_pc_dt.replace(tzinfo=None).isoformat()
                        donnees_completes["heure"] = heure_pc_dt.strftime("%Hh%M")
                        heure_estimee = True
                        log.info(
                            "Heure de prise en charge estimee pour %s : %s (trajet %d min + marge %d min, arrondi)",
                            expediteur, donnees_completes["heure"], duree_trajet, marge_securite_minutes,
                        )

                champs_manquants = [
                    c for c in CHAMPS_OBLIGATOIRES if not donnees_completes.get(c)
                ]
                # Si le nom du client differe de celui deja connu pour ce
                # numero, c'est une reservation DIFFERENTE (ex: le numero
                # admin de Tony sert a creer plusieurs reservations pour
                # des clients differents a la suite) -> on ne doit surtout
                # pas recycler l'ancien evenement/reference, meme si
                # l'ancienne reservation etait deja complete.
                # De plus, pour un numero admin, CHAQUE message complet est
                # toujours une nouvelle course independante (meme si c'est
                # le meme nom de client qui revient avec un autre trajet),
                # car l'admin ne "corrige" jamais une reservation en cours
                # via un message de suivi -- il tape chaque course d'un
                # bloc. On ne recycle donc JAMAIS l'ancien evenement pour
                # un expediteur admin.
                nom_precedent = (donnees_existantes or {}).get("nom")
                nouveau_client_different = bool(
                    nom_precedent and donnees_completes.get("nom")
                    and donnees_completes["nom"] != nom_precedent
                )
                etait_deja_complete = bool(
                    entree_existante and entree_existante["complete"]
                    and not nouveau_client_different
                    and expediteur not in ADMIN_PHONE_NUMBERS
                )
                est_complete_maintenant = not champs_manquants
                event_id_a_conserver = None
                reference_a_conserver = None

                # Protection anti-doublon : si la reservation etait deja
                # complete ET que rien n'a change par rapport a avant, on
                # ne renvoie pas de SMS (evite le spam en cas de livraison
                # en double du meme SMS par SMS Gateway).
                if etait_deja_complete and donnees_completes == donnees_existantes:
                    log.info("Message identique a une reservation deja complete pour %s, ignore", expediteur)
                    return jsonify({"status": "ok", "info": "doublon ignore"}), 200

                if est_complete_maintenant and not etait_deja_complete:
                    # Limite anti-abus : pas plus de MAX_RESERVATIONS_ACTIVES
                    # reservations futures en meme temps pour un numero.
                    reservations_en_cours = rechercher_evenements(expediteur, seulement_futur=True)
                    if len(reservations_en_cours) >= MAX_RESERVATIONS_ACTIVES:
                        texte_reponse = (
                            f"Vous avez deja {MAX_RESERVATIONS_ACTIVES} reservations en cours. "
                            "Merci d'en annuler une avant d'en ajouter une nouvelle."
                        )
                        envoyer_sms(expediteur, texte_reponse)
                        return jsonify({"status": "ok"}), 200
                    # Premiere fois que cette reservation est complete ->
                    # on cree l'evenement dans Google Agenda avec une
                    # nouvelle reference.
                    reference_a_conserver = generer_reference()
                    succes, detail, event_id = creer_evenement_agenda(
                        donnees_completes, expediteur, reference_a_conserver
                    )
                    if succes:
                        log.info("Evenement Google Agenda cree pour %s : %s", expediteur, detail)
                        event_id_a_conserver = event_id
                        succes_email, detail_email = envoyer_email_confirmation(
                            donnees_completes, expediteur, reference_a_conserver
                        )
                        if succes_email:
                            log.info("Email de confirmation envoye pour %s", expediteur)
                        else:
                            log.error("Echec envoi email pour %s : %s", expediteur, detail_email)
                    else:
                        log.error("Echec creation evenement Agenda pour %s : %s", expediteur, detail)
                        reference_a_conserver = None
                elif etait_deja_complete:
                    # Reservation deja complete auparavant (ex: correction
                    # mineure apres coup) -> on garde l'event_id/reference
                    # existants.
                    event_id_a_conserver = entree_existante.get("event_id")
                    reference_a_conserver = entree_existante.get("reference")

                texte_reponse = construire_reponse(
                    donnees_completes, reference_a_conserver if est_complete_maintenant else "",
                    heure_estimee=heure_estimee, premier_message=(entree_existante is None),
                )

                sauvegarder_entree(
                    expediteur, donnees_completes, complete=est_complete_maintenant,
                    event_id=event_id_a_conserver, reference=reference_a_conserver,
                )

        envoyer_sms(expediteur, texte_reponse)

    return jsonify({"status": "ok"}), 200


@app.route("/admin/reinitialiser-numero", methods=["GET"])
def reinitialiser_numero():
    """Vide la memoire (reservation en cours + attente d'annulation) pour un
    numero donne, pour repartir de zero sur un scenario de test sans
    attendre l'expiration naturelle (1h). Usage :
    /admin/reinitialiser-numero?numero=0624125779 (accepte le format
    francais local 06... ou le format international +336...)."""
    numero_brut = request.args.get("numero", "").strip()
    if not numero_brut:
        return jsonify({"erreur": "Merci de fournir ?numero=0612345678 dans l'URL."}), 400

    # Normalisation basique du format francais local -> international,
    # pour matcher le format utilise par SMS Gateway (+336...).
    numero = numero_brut
    if numero.startswith("0") and len(numero) == 10:
        numero = "+33" + numero[1:]
    elif not numero.startswith("+"):
        numero = "+" + numero

    existait_reservation = numero in MEMOIRE_RESERVATIONS
    existait_attente = numero in MEMOIRE_ANNULATION_EN_ATTENTE
    MEMOIRE_RESERVATIONS.pop(numero, None)
    MEMOIRE_ANNULATION_EN_ATTENTE.pop(numero, None)
    persister_memoire_reservations()

    return jsonify({
        "numero_normalise": numero,
        "reservation_effacee": existait_reservation,
        "attente_annulation_effacee": existait_attente,
        "message": f"Memoire reinitialisee pour {numero}. Tu peux recommencer un test comme un nouveau client.",
    }), 200


@app.route("/admin/rappels-demain", methods=["GET"])
def rappels_demain():
    """A appeler une fois par jour (voir cron) : envoie a chaque client
    ayant une reservation le LENDEMAIN un SMS de rappel/confirmation, avec
    les infos TELLES QU'ELLES SONT ACTUELLEMENT dans Google Agenda -- donc
    a jour meme si la prise en charge (ou autre) a ete modifiee a la main
    par Tony directement dans l'agenda apres la creation initiale."""
    demain = (datetime.now(FUSEAU_HORAIRE) + timedelta(days=1)).date()
    evenements = lister_evenements_du_jour(demain)

    resultats = []
    for ev in evenements:
        description = ev.get("description", "")
        telephone = extraire_champ_de_description(description, "TEL")
        pc = extraire_champ_de_description(description, "PC")
        dest = extraire_champ_de_description(description, "DEST")
        rdv = extraire_champ_de_description(description, "RDV")
        reference = extraire_champ_de_description(description, "REF")

        if telephone in ("?", "", None):
            resultats.append({"evenement": ev.get("summary"), "envoye": False, "raison": "telephone introuvable"})
            continue
        if reference in ("?", "", None):
            resultats.append({"evenement": ev.get("summary"), "envoye": False, "raison": "reference introuvable (pas une reservation SMS)"})
            continue

        # L'heure de prise en charge n'est pas un champ separe dans la
        # description, elle correspond a l'heure de debut de l'evenement
        # (ce qui reflete aussi un decalage fait a la main dans l'agenda).
        debut_iso = ev.get("start", {}).get("dateTime")
        heure_pc = "?"
        if debut_iso:
            try:
                heure_pc = datetime.fromisoformat(debut_iso).strftime("%Hh%M")
            except ValueError:
                pass

        texte_rappel = (
            f"Rappel : votre taxi de demain est confirme a {heure_pc}. "
            f"Prise en charge {pc}, direction {dest} ({rdv}). "
            f"Ref: {reference}. A demain !"
        )
        envoyer_sms(telephone, texte_rappel)
        resultats.append({"telephone": telephone, "reference": reference})

    return jsonify({
        "date_ciblee": demain.isoformat(),
        "nombre_reservations": len(evenements),
        "rappels": resultats,
    }), 200


@app.route("/admin/verifier-reservation", methods=["GET"])
def verifier_reservation():
    """Cherche directement dans Google Agenda un evenement contenant ce
    code de reference, pour verifier qu'une reservation confirmee par SMS
    a bien ete creee cote agenda. Usage :
    /admin/verifier-reservation?reference=ZRTK9Z"""
    reference = request.args.get("reference", "").strip().upper()
    if not reference:
        return jsonify({"erreur": "Merci de fournir ?reference=XXXXXX dans l'URL."}), 400

    evenements = rechercher_evenements(reference, seulement_futur=False)
    resultats = [
        {
            "titre": e.get("summary"),
            "debut": e.get("start", {}).get("dateTime"),
            "lien": e.get("htmlLink"),
        }
        for e in evenements
        if extraire_reference_de_description(e.get("description", "")) == reference
        or reference in (e.get("summary") or "")
    ]
    return jsonify({
        "reference": reference,
        "trouve": bool(resultats),
        "evenements": resultats,
        "calendar_id_utilise": GOOGLE_CALENDAR_ID,
    }), 200


@app.route("/admin/verifier-memoire", methods=["GET"])
def verifier_memoire():
    """Affiche l'etat de la memoire des reservations en cours, et confirme
    si le fichier de persistance existe bien sur le volume /data. Utile
    pour verifier que la persistance survit a un redemarrage."""
    fichier_existe = os.path.isfile(FICHIER_MEMOIRE_RESERVATIONS)
    taille_fichier = os.path.getsize(FICHIER_MEMOIRE_RESERVATIONS) if fichier_existe else 0
    return jsonify({
        "dossier_donnees": DOSSIER_DONNEES,
        "fichier_persistance": FICHIER_MEMOIRE_RESERVATIONS,
        "fichier_existe_sur_disque": fichier_existe,
        "taille_fichier_octets": taille_fichier,
        "nombre_entrees_en_memoire": len(MEMOIRE_RESERVATIONS),
        "numeros_en_memoire": list(MEMOIRE_RESERVATIONS.keys()),
    }), 200


@app.route("/", methods=["GET"])
def racine():
    return (
        "SMS reservation webhook - operationnel<br><br>"
        '<a href="/admin/enregistrer-webhook">'
        "Cliquer ici pour enregistrer le webhook aupres de SMS Gateway</a>"
    ), 200


@app.route("/admin/enregistrer-webhook", methods=["GET"])
def enregistrer_webhook():
    """
    Page a visiter dans le navigateur : supprime tous les webhooks
    existants pour "sms:received" puis en recree un seul, propre,
    pointant vers notre URL. Evite les doublons qui causent des
    reponses envoyees plusieurs fois pour un seul SMS.
    """
    # Railway termine le HTTPS en amont et transmet en http:// en interne,
    # donc request.host_url renvoie parfois "http://..." meme si l'acces
    # public est bien en https. On force https ici car SMS Gateway l'exige.
    hote = request.host_url.rstrip("/").split("://", 1)[-1]
    url_webhook = f"https://{hote}/webhook/sms"

    try:
        existants = requests.get(
            "https://api.sms-gate.app/3rdparty/v1/webhooks",
            auth=(GATEWAY_USERNAME, GATEWAY_PASSWORD),
            timeout=15,
        )
        existants.raise_for_status()
        supprimes = []
        for hook in existants.json():
            requests.delete(
                f"https://api.sms-gate.app/3rdparty/v1/webhooks/{hook['id']}",
                auth=(GATEWAY_USERNAME, GATEWAY_PASSWORD),
                timeout=15,
            )
            supprimes.append(hook["id"])

        reponse = requests.post(
            "https://api.sms-gate.app/3rdparty/v1/webhooks",
            json={"url": url_webhook, "event": "sms:received"},
            auth=(GATEWAY_USERNAME, GATEWAY_PASSWORD),
            timeout=15,
        )
        reponse.raise_for_status()
    except requests.RequestException as e:
        return f"Erreur de connexion a SMS Gateway : {e}", 500

    return (
        f"Anciens webhooks supprimes : {supprimes}<br><br>"
        "Nouveau webhook enregistre avec succes !<br><br>"
        f"Reponse du serveur : {reponse.text}<br><br>"
        "Tu peux maintenant envoyer un SMS de test au numero du telephone dedie."
    ), 200


@app.route("/admin/lister-webhooks", methods=["GET"])
def lister_webhooks():
    """Affiche tous les webhooks actuellement enregistres, pour verifier
    qu'il n'y en a pas plusieurs en double."""
    try:
        reponse = requests.get(
            "https://api.sms-gate.app/3rdparty/v1/webhooks",
            auth=(GATEWAY_USERNAME, GATEWAY_PASSWORD),
            timeout=15,
        )
        reponse.raise_for_status()
    except requests.RequestException as e:
        return f"Erreur : {e}", 500
    return jsonify(reponse.json())


@app.route("/admin/verifier-cle-ia", methods=["GET"])
def verifier_cle_ia():
    """Verifie rapidement si la cle Anthropic est configuree et valide,
    en affichant le detail directement dans le navigateur (pas besoin
    d'aller chercher dans les logs Railway)."""
    if not ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY n'est PAS configuree sur Railway.", 200

    try:
        reponse = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": PROMPT_SYSTEME,
                "messages": [{"role": "user", "content": "Test taxi demain 8h, 5 rue de France, gare, Jean Dupont"}],
            },
            timeout=20,
        )
    except requests.RequestException as e:
        return f"Erreur reseau en appelant Anthropic : {e}", 200

    return (
        f"Statut HTTP recu d'Anthropic : {reponse.status_code}<br><br>"
        f"Corps de la reponse (brut) :<br>{reponse.text[:1500]}"
    ), 200


@app.route("/admin/tester-agenda", methods=["GET"])
def tester_agenda():
    """Cree un evenement de test dans Google Agenda pour verifier que la
    configuration (compte de service + calendrier partage) fonctionne,
    sans avoir besoin d'envoyer un SMS."""
    demain = datetime.now(FUSEAU_HORAIRE) + timedelta(days=1)
    donnees_test = {
        "type": "prive",
        "nom": "Test EasyTaxi",
        "telephone": "0600000000",
        "prise_en_charge": "1 rue de Test",
        "destination": "Aeroport",
        "heure": "test",
        "heure_iso": demain.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
    }
    succes, detail, _event_id = creer_evenement_agenda(donnees_test)
    if succes:
        return f"Evenement de test cree avec succes !<br><br>Lien : {detail}", 200
    return f"Echec de la creation de l'evenement de test :<br><br>{detail}", 200


@app.route("/admin/tester-email", methods=["GET"])
def tester_email():
    """Envoie un email de test pour verifier la configuration SMTP Gmail."""
    demain = datetime.now(FUSEAU_HORAIRE) + timedelta(days=1)
    donnees_test = {
        "type": "prive",
        "nom": "Test EasyTaxi",
        "telephone": "0600000000",
        "prise_en_charge": "1 rue de Test",
        "destination": "Aeroport",
        "heure": "test",
        "heure_iso": demain.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
    }
    succes, detail = envoyer_email_confirmation(donnees_test, "0600000000", "TEST01")
    if succes:
        return "Email de test envoye avec succes ! Verifie ta boite mail.", 200
    return f"Echec de l'envoi de l'email de test :<br><br>{detail}", 200


@app.route("/admin/tester-trajet", methods=["GET"])
def tester_trajet():
    """Teste l'estimation du temps de trajet entre 2 adresses (Nice -> Marseille par defaut)."""
    origine = request.args.get("origine", "Nice, France")
    destination = request.args.get("destination", "Hopital de la Timone, Marseille, France")
    duree = estimer_duree_trajet(origine, destination)
    if duree is None:
        return (
            f"Impossible d'estimer le trajet de '{origine}' vers '{destination}'. "
            "Verifie GOOGLE_MAPS_API_KEY et que la Distance Matrix API est activee."
        ), 200
    return f"Trajet estime de '{origine}' vers '{destination}' : {duree} minutes.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
