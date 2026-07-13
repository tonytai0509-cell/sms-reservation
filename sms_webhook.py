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
  RESEND_API_KEY              -> cle API Resend (resend.com), pour l'envoi d'email (HTTPS, pas de SMTP bloque)
  EMAIL_DESTINATAIRE          -> adresse email qui recoit les confirmations (ex: tony.tai0509@gmail.com)
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
FUSEAU_HORAIRE = ZoneInfo("Europe/Paris")

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
- prise_en_charge : adresse ou lieu de depart complet (numero + nom de rue).
- destination : adresse ou lieu d'arrivee.
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
  calculee a partir de la date actuelle donnee plus bas. CONTRAIREMENT a
  heure/heure_iso, ce champ doit etre rempli DES QU'UNE DATE est evoquee,
  MEME SI aucune heure precise n'est encore donnee (ex: "rendez-vous
  apres-demain" sans heure -> date rempli, heure/heure_iso restent null en
  attendant l'heure precise). Une fois rempli, il n'est JAMAIS efface --
  garde-le meme si les messages suivants ne reparlent pas de la date, sauf
  si le client mentionne clairement une nouvelle date differente.
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
{"type": "...", "nom": ..., "telephone": ..., "prise_en_charge": ..., "destination": ..., "heure_rdv": ..., "heure": ..., "date": ..., "heure_iso": ..., "est_question": true/false, "plusieurs_courses": true/false, "annulation": true/false, "reference_annulation": ...}
"""

CHAMPS_OBLIGATOIRES = {
    "nom": "votre nom",
    "prise_en_charge": "l'adresse de prise en charge",
    "destination": "la destination",
    "heure": "l'heure a laquelle le chauffeur doit venir vous chercher",
}

# Memoire des reservations par numero de telephone, qu'elles soient
# completes ou non. Permet de repondre correctement a un message de suivi
# ("a quelle heure ?") sans tout redemander, et de fusionner les infos
# entre plusieurs SMS d'une meme demande.
# Format : { "+336...": {"donnees": {...}, "derniere_maj": ts, "complete": bool} }
# Simple dictionnaire en RAM : suffisant pour ce volume, se vide si le
# service redemarre (acceptable, cas rare).
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


def recuperer_entree(numero: str) -> dict | None:
    """Renvoie l'entree memorisee pour ce numero (donnees + statut complete)
    si elle n'a pas expire (plus d'1h sans nouveau message), sinon None."""
    entree = MEMOIRE_RESERVATIONS.get(numero)
    if entree is None:
        return None
    if time.time() - entree["derniere_maj"] > DUREE_EXPIRATION_SECONDES:
        MEMOIRE_RESERVATIONS.pop(numero, None)
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


def construire_reponse(donnees: dict, reference: str = "") -> str:
    """Construit le SMS de reponse selon que la reservation est complete ou non."""
    manquants = [
        champ
        for champ in CHAMPS_OBLIGATOIRES
        if not donnees.get(champ)
    ]

    if manquants:
        # Cas particulier frequent en medical : le client a donne l'heure de
        # son rendez-vous mais pas l'heure a laquelle le chauffeur doit
        # venir -> on le precise clairement pour eviter la confusion.
        if manquants == ["heure"] and donnees.get("heure_rdv"):
            return (
                f"Votre rendez-vous est note a {donnees['heure_rdv']}. "
                "A quelle heure souhaitez-vous que le chauffeur vienne vous chercher ?"
            )
        libelles = [CHAMPS_OBLIGATOIRES[c] for c in manquants]
        return "Merci de preciser : " + ", ".join(libelles) + "."

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

    titre = (
        f"PC {heure_aff} M. {donnees['nom']} | "
        f"PC : {donnees['prise_en_charge']} | "
        f"DEST : {donnees['destination']} | "
        f"RDV : {heure_aff} {type_tag} | "
        f"TEL : {telephone} | REF : {reference}"
    ).upper()
    description = (
        f"REF : {reference}\n"
        f"PC : {donnees['prise_en_charge']}\n"
        f"DEST : {donnees['destination']}\n"
        f"RDV : {heure_aff} {type_tag}\n"
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

        donnees_extraites = extraire_reservation(message, donnees_existantes)

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
            elif est_question and entree_existante:
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

                champs_manquants = [
                    c for c in CHAMPS_OBLIGATOIRES if not donnees_completes.get(c)
                ]
                etait_deja_complete = bool(entree_existante and entree_existante["complete"])
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
                    donnees_completes, reference_a_conserver if est_complete_maintenant else ""
                )

                sauvegarder_entree(
                    expediteur, donnees_completes, complete=est_complete_maintenant,
                    event_id=event_id_a_conserver, reference=reference_a_conserver,
                )

        envoyer_sms(expediteur, texte_reponse)

    return jsonify({"status": "ok"}), 200


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

