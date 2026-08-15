"""
reservation_web.py

Deuxieme moyen de reservation, INDEPENDANT du bot SMS (sms_webhook.py,
non modifie). Sert une page web simple de reservation de taxi, pensee
pour les clients qui sont mal a l'aise avec l'envoi de SMS. Le lien de
cette page (/reserver) peut etre colle sur un QR code.

Cree un evenement dans le MEME Google Agenda que le bot SMS (memes
variables GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_CALENDAR_ID), envoie un
email de confirmation, et un SMS de confirmation avec la reference au
client (via SMS Gateway, memes identifiants que le bot SMS).

Deploiement recommande : un DEUXIEME service Railway, dans le MEME
projet et le MEME depot GitHub que le bot SMS, mais qui lance CE fichier
(pas sms_webhook.py). Les deux services peuvent partager les memes
variables d'environnement Railway (copier/coller depuis le service SMS) :
  GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_CALENDAR_ID, GOOGLE_MAPS_API_KEY
  RESEND_API_KEY, EMAIL_DESTINATAIRE
  SMS_GATEWAY_USERNAME, SMS_GATEWAY_PASSWORD, SMS_GATEWAY_MODE (optionnel),
  SMS_GATEWAY_LOCAL_URL (optionnel, si mode local)
  MAX_RESERVATIONS_ACTIVES (optionnel, defaut 5)

Commande de lancement Railway pour ce service (Start Command) :
  python reservation_web.py
"""

import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, render_template_string, send_from_directory, redirect
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reservation_web")

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
FUSEAU_HORAIRE = ZoneInfo("Europe/Paris")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_DESTINATAIRE = os.environ.get("EMAIL_DESTINATAIRE", "")

GATEWAY_USERNAME = os.environ.get("SMS_GATEWAY_USERNAME", "")
GATEWAY_PASSWORD = os.environ.get("SMS_GATEWAY_PASSWORD", "")
GATEWAY_MODE = os.environ.get("SMS_GATEWAY_MODE", "cloud")
LOCAL_URL = os.environ.get("SMS_GATEWAY_LOCAL_URL", "")
if GATEWAY_MODE == "local":
    SEND_URL = f"{LOCAL_URL.rstrip('/')}/3rdparty/v1/messages"
else:
    SEND_URL = "https://api.sms-gate.app/3rdparty/v1/messages"

MAX_RESERVATIONS_ACTIVES = int(os.environ.get("MAX_RESERVATIONS_ACTIVES", "5"))

# Code secret pour le mode admin (voir /reserver?admin=CE_CODE). Changez-le
# en ajoutant une variable ADMIN_ACCESS_CODE sur Railway pour ce service.
ADMIN_ACCESS_CODE = os.environ.get("ADMIN_ACCESS_CODE", "kelly-admin-2026")

# Code secret partage pour les secretaires (voir /reserver?admin=CE_CODE).
# Meme comportement que le mode admin (aucun SMS/email envoye au client),
# mais code different pour ne pas partager le code personnel de Tony.
# Changez-le via une variable SECRETAIRE_ACCESS_CODE sur Railway.
SECRETAIRE_ACCESS_CODE = os.environ.get("SECRETAIRE_ACCESS_CODE", "kelly-secretaire-2026")


def determiner_role(code_saisi: str | None) -> str | None:
    """Renvoie 'admin', 'secretaire' ou None selon le code fourni dans
    l'URL (?admin=...) ou le champ cache du formulaire."""
    if not code_saisi:
        return None
    if code_saisi == ADMIN_ACCESS_CODE:
        return "admin"
    if code_saisi == SECRETAIRE_ACCESS_CODE:
        return "secretaire"
    return None


# ---------------------------------------------------------------------------
# Aide a l'adressage (memes tables que le bot SMS, dupliquees ici pour que
# ce fichier reste totalement independant)
# ---------------------------------------------------------------------------

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

VILLES_CONNUES = [
    "nice", "cagnes-sur-mer", "cagnes sur mer", "saint-laurent-du-var",
    "saint laurent du var", "antibes", "cannes", "grasse", "vence", "menton",
    "villeneuve-loubet", "villeneuve loubet", "beaulieu", "villefranche",
    "carros", "mougins", "valbonne", "biot", "roquefort", "marseille",
    "paris", "lyon", "toulon", "monaco", "aix-en-provence", "aix en provence",
]


def resoudre_adresse_medicale(adresse: str) -> str:
    adresse_minuscule = (adresse or "").lower()
    for cle, adresse_complete in ADRESSES_ETABLISSEMENTS_SANTE.items():
        if cle in adresse_minuscule:
            return adresse_complete
    return adresse


def completer_adresse_avec_ville(adresse: str) -> str:
    adresse_minuscule = (adresse or "").lower()
    if re.search(r"\b\d{5}\b", adresse_minuscule):
        return adresse
    if any(ville in adresse_minuscule for ville in VILLES_CONNUES):
        return adresse
    return f"{adresse}, Nice, France"


def estimer_duree_trajet(origine: str, destination: str) -> int | None:
    """Estime la duree du trajet en minutes via Google Distance Matrix."""
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
            log.warning("Distance Matrix statut non-OK %s -> %s : %s", origine, destination, element.get("status"))
            return None
        return round(element["duration"]["value"] / 60)
    except Exception as e:
        log.error("Erreur estimation trajet %s -> %s : %s", origine, destination, e)
        return None


def normaliser_numero_francais(numero: str) -> str:
    """Convertit 0612345678 -> +33612345678. Laisse tel quel les autres formats."""
    numero_nettoye = re.sub(r"[\s.\-]", "", numero or "")
    if numero_nettoye.startswith("0") and len(numero_nettoye) == 10 and numero_nettoye.isdigit():
        return "+33" + numero_nettoye[1:]
    return numero_nettoye


def generer_reference() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(alphabet, k=6))


def extraire_reference_de_description(description: str) -> str:
    """Recupere le code de reference ecrit dans la description d'un
    evenement Google Agenda (ligne 'REF : XXXXXX'). Meme logique que le
    bot SMS, pour reconnaitre une reservation deja creee."""
    trouve = re.search(r"REF\s*:\s*([A-Z0-9]+)", description or "", re.IGNORECASE)
    return trouve.group(1).upper() if trouve else "?"


def libelle_date_relative(dt: datetime) -> str:
    aujourd_hui = datetime.now(FUSEAU_HORAIRE).date()
    ecart = (dt.date() - aujourd_hui).days
    if ecart == 0:
        return "aujourd'hui"
    if ecart == 1:
        return "demain"
    if ecart == 2:
        return "apres-demain"
    return dt.strftime("le %d/%m")


# ---------------------------------------------------------------------------
# Google Agenda (meme calendrier que le bot SMS)
# ---------------------------------------------------------------------------

def _construire_service_agenda():
    infos_compte = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        infos_compte, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


def rechercher_evenements(texte_recherche: str, seulement_futur: bool = True) -> list[dict]:
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


def creer_evenement_agenda(donnees: dict, reference: str) -> tuple[bool, str, str | None]:
    """Cree l'evenement dans Google Agenda. Meme format de titre/description
    que le bot SMS, pour que les deux sources de reservation soient
    indiscernables une fois dans l'agenda (chauffeurs, rappels J-1, etc.)."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return False, "Google Agenda non configure (variables manquantes sur Railway)", None

    try:
        debut_dt = datetime.fromisoformat(donnees["heure_iso"])
    except (ValueError, KeyError) as e:
        return False, f"Date/heure invalide : {e}", None

    fin_dt = debut_dt + timedelta(hours=1)
    type_tag = "[MED]" if donnees.get("type") == "medical" else "[PRIVE]"
    emoji_type = "💊" if donnees.get("type") == "medical" else "🚕"
    telephone = donnees.get("telephone") or "(non renseigne)"
    heure_aff = debut_dt.strftime("%Hh%M")
    heure_rdv_aff = donnees.get("heure_rdv") or heure_aff
    nom_pour_agenda = donnees.get("nom_agenda") or donnees["nom"]

    titre = (
        f"PC {heure_aff} {emoji_type} M. {nom_pour_agenda} | "
        f"PC : {donnees['prise_en_charge']} | "
        f"DEST : {donnees['destination']} | "
        f"RDV : {heure_rdv_aff} {type_tag} | "
        f"TEL : {telephone} | REF : {reference}"
        + (f" [{donnees['nom_infirmiere']}]" if donnees.get("nom_infirmiere") else "")
        + (" [ACCOMPAGNANT]" if donnees.get("accompagnant") else "")
        + (" [BT AU RETOUR]" if donnees.get("bto_retour") else "")
    ).upper()
    role = donnees.get("role")
    if role == "admin":
        source_label = "reservation prise par Tony (admin)"
    elif role == "secretaire":
        source_label = "reservation prise par une/un secretaire"
    else:
        source_label = "reservation en ligne (client)"

    description = (
        f"REF : {reference}\n"
        f"PC : {donnees['prise_en_charge']}\n"
        f"DEST : {donnees['destination']}\n"
        f"RDV : {heure_rdv_aff} {type_tag}\n"
        f"TEL : {telephone}\n"
        f"SOURCE : {source_label}"
        + (f"\nINFIRMIERE : {donnees['nom_infirmiere']}" if donnees.get("nom_infirmiere") else "")
        + ("\nACCOMPAGNANT : OUI" if donnees.get("accompagnant") else "")
        + ("\nBT : AU RETOUR UNIQUEMENT" if donnees.get("bto_retour") else "")
        + ("\nRAPPEL : NON" if donnees.get("mode_admin") else "")
    ).upper()

    try:
        service = _construire_service_agenda()
        evenement = {
            "summary": titre,
            "description": description,
            "start": {"dateTime": debut_dt.isoformat(), "timeZone": "Europe/Paris"},
            "end": {"dateTime": fin_dt.isoformat(), "timeZone": "Europe/Paris"},
            # Meme colorId (5, jaune) que le bot SMS pour reconnaitre d'un
            # coup d'oeil les reservations automatiques (SMS ou web) dans
            # l'agenda, par opposition aux evenements ajoutes a la main.
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


def envoyer_email_confirmation(donnees: dict, reference: str) -> tuple[bool, str]:
    if not (RESEND_API_KEY and EMAIL_DESTINATAIRE):
        return False, "Email non configure (variables manquantes sur Railway)"

    type_label = "MEDICAL" if donnees.get("type") == "medical" else "PRIVE"
    debut_dt = datetime.fromisoformat(donnees["heure_iso"])
    moment = f"{libelle_date_relative(debut_dt)} a {debut_dt.strftime('%Hh%M')}"

    corps = (
        f"Nouvelle reservation EN LIGNE confirmee\n\n"
        f"Reference : {reference}\n"
        f"Type : {type_label}\n"
        f"Nom : {donnees['nom']}\n"
        f"Telephone : {donnees.get('telephone', '(non renseigne)')}\n"
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
                "from": "EasyTaxi <onboarding@resend.dev>",
                "to": [EMAIL_DESTINATAIRE],
                "subject": f"Reservation en ligne - {donnees['nom']} - Ref {reference}",
                "text": corps,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        return False, f"Erreur reseau : {e}"

    if reponse.status_code >= 300:
        return False, f"Statut {reponse.status_code} : {reponse.text[:300]}"
    return True, "email envoye"


def envoyer_sms(numero: str, texte: str) -> None:
    if not (GATEWAY_USERNAME and GATEWAY_PASSWORD):
        log.warning("SMS non configure - confirmation SMS non envoyee a %s", numero)
        return
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


def construire_sms_confirmation(donnees: dict, reference: str, heure_estimee: bool) -> str:
    nom = donnees["nom"]
    depart = donnees["prise_en_charge"]
    destination = donnees["destination"]
    debut_dt = datetime.fromisoformat(donnees["heure_iso"])
    moment = f"{libelle_date_relative(debut_dt)} a {debut_dt.strftime('%Hh%M')}"

    if donnees.get("heure_rdv") and heure_estimee:
        reponse = (
            f"Reservation confirmee pour M. {nom} : rendez-vous a {donnees['heure_rdv']}. "
            f"Le chauffeur passera vous chercher {moment} au {depart}, direction {destination} "
            "(heure de prise en charge calculee automatiquement selon le trajet et une marge de securite). "
            "Un chauffeur vous contactera peu avant son arrivee."
        )
    elif donnees.get("heure_rdv"):
        reponse = (
            f"Reservation confirmee pour M. {nom} : rendez-vous a {donnees['heure_rdv']}, "
            f"prise en charge {moment} au {depart}, direction {destination}. "
            "Un chauffeur vous contactera peu avant son arrivee."
        )
    else:
        reponse = (
            f"Reservation confirmee pour M. {nom} : prise en charge {moment} "
            f"au {depart}, direction {destination}. "
            "Un chauffeur vous contactera peu avant son arrivee."
        )
    return reponse + f" Ref: {reference} (a rappeler pour annuler)."


def parser_heure_texte(texte: str) -> tuple[int, int] | None:
    trouve = re.search(r"(\d{1,2})\s*[h:]\s*(\d{2})?", texte or "")
    if not trouve:
        return None
    heure = int(trouve.group(1))
    minute = int(trouve.group(2)) if trouve.group(2) else 0
    if 0 <= heure <= 23 and 0 <= minute <= 59:
        return heure, minute
    return None


# ---------------------------------------------------------------------------
# Pages web
# ---------------------------------------------------------------------------

FORMULAIRE_RESERVATION_HTML = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reserver un taxi - Centrale des Taxis Nicois</title>
<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="manifest" href="/manifest.json{% if admin_code %}?admin={{ admin_code }}{% endif %}">
<meta name="theme-color" content="#0d2a52">
<style>
  :root {
    color-scheme: light;
    --navy: #0d2a52;
    --navy-dark: #081b38;
    --vert: #1e8e3e;
    --vert-clair: #e7f6ec;
    --bordure: #dde2e8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 60px; background: #f4f5f7;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: #1a1a1a;
  }
  .page { max-width: 480px; margin: 0 auto; }
  .grille-haut { display: contents; }

  @media (min-width: 720px) {
    body { padding: 40px 24px 60px; }
    .page { max-width: 880px; }
    .grille-haut {
      display: grid; grid-template-columns: 340px 1fr; gap: 16px; align-items: start;
    }
    .grille-haut .carte { margin-bottom: 0; min-width: 0; }
    .raccourcis { flex-wrap: wrap; overflow-x: visible; }
  }
  .entete {
    display: flex; flex-direction: column; align-items: center;
    text-align: center; margin-bottom: 26px; padding-top: 4px;
  }
  .logo-entete {
    display: block; width: min(88vw, 620px); max-width: 100%;
    height: auto; object-fit: contain; margin: 0 auto;
  }
  .entete-soustitre {
    margin: 14px 0 0; color: #667; font-size: 14px; font-weight: 400;
  }
  @media (max-width: 380px) {
    .logo-entete { width: min(84vw, 340px); }
    .entete { margin-bottom: 20px; }
    .entete-soustitre { margin-top: 10px; font-size: 13px; }
  }
  .badge-partenaire {
    display: inline-flex; align-items: center; justify-content: center; gap: 5px;
    margin: 14px auto 0; background: var(--navy); color: #fff; padding: 5px 12px;
    border-radius: 20px; font-size: 13px; font-weight: 700;
    animation: pulsation-douce 2.6s ease-in-out infinite;
  }
  @keyframes pulsation-douce {
    0%, 100% { box-shadow: 0 0 0 rgba(13, 42, 82, 0); }
    50% { box-shadow: 0 0 14px rgba(13, 42, 82, 0.45); }
  }

  .carte {
    background: #ffffff; border-radius: 16px; padding: 20px 20px 14px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.06); margin-bottom: 12px;
  }
  .section-titre {
    display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
  }
  .numero {
    width: 26px; height: 26px; border-radius: 50%; background: var(--navy);
    color: #fff; font-weight: 700; font-size: 14px;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .section-titre h2 { margin: 0; font-size: 16px; color: var(--navy); }
  .badge-medical {
    display: inline-flex; align-items: center; gap: 5px; background: var(--vert-clair);
    color: var(--vert); padding: 5px 12px; border-radius: 20px; font-size: 13px; font-weight: 700;
  }
  .infirmiere-affichage {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    margin-top: 16px; font-size: 13px; color: #444; flex-wrap: nowrap;
  }
  .infirmiere-affichage span {
    display: inline-flex; align-items: center; gap: 6px; color: #667;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
  }
  .infirmiere-affichage span svg { flex-shrink: 0; color: #8a95a3; }
  .infirmiere-affichage strong { color: #1a1a1a; }
  .infirmiere-affichage button {
    display: inline-flex; align-items: center; gap: 5px; background: none; border: none;
    color: var(--navy); font-weight: 600; font-size: 13px; cursor: pointer; padding: 4px 6px;
    flex-shrink: 0;
  }

  label {
    display: block; font-weight: 600; margin: 14px 0 6px; font-size: 14px; color: #333;
  }
  label:first-of-type { margin-top: 0; }

  .champ-icone { position: relative; }
  .champ-icone svg {
    position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
    color: #8a95a3; pointer-events: none;
  }
  .champ-icone input {
    padding-left: 42px !important;
  }

  input[type=text], input[type=tel], input[type=date], input[type=time] {
    width: 100%; padding: 13px 14px; font-size: 16px; border: 1.5px solid var(--bordure);
    border-radius: 10px; background: #fafbfc;
  }
  input:focus {
    outline: none; border-color: var(--navy);
    box-shadow: 0 0 0 3px rgba(13, 42, 82, 0.15), 0 0 12px rgba(13, 42, 82, 0.25);
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
  }
  input::placeholder { font-size: 14px; }

  .ligne-double { display: flex; gap: 10px; }
  .ligne-double > div { flex: 1; }
  .ligne-double-souple { display: flex; flex-direction: column; gap: 10px; }
  .ligne-double-souple > div { flex: none; width: 100%; min-width: 0; }

  .choix { display: flex; gap: 10px; margin: 6px 0 4px; }
  .choix label {
    flex: 1; margin: 0; display: flex; align-items: center; justify-content: center; gap: 8px;
    text-align: center; padding: 13px 6px; border: 1.5px solid var(--bordure);
    border-radius: 10px; font-weight: 600; cursor: pointer; font-size: 14px; color: #444;
  }
  .choix input { display: none; }
  .choix label svg { flex-shrink: 0; }
  #type_prive:checked ~ .choix-fill-prive,
  label:has(#type_prive:checked) { border-color: var(--navy); background: #eef2f7; color: var(--navy); }
  label:has(#type_medical:checked) { border-color: var(--vert); background: var(--vert-clair); color: var(--vert); }

  .adresses { position: relative; }
  .bouton-inverser {
    position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
    width: 30px; height: 30px; border-radius: 50%; background: #fff;
    border: 1.5px solid var(--bordure); display: flex; align-items: center;
    justify-content: center; cursor: pointer; z-index: 2; color: var(--navy);
  }

  .case-auto {
    margin-top: 12px; display: flex; align-items: flex-start; gap: 8px;
    font-size: 13px; color: #555;
  }
  .case-auto input { width: auto; margin-top: 2px; }

  button.envoyer {
    width: 100%; margin-top: 4px; padding: 16px; font-size: 17px; font-weight: 700;
    background: var(--navy); color: #fff; border: none; border-radius: 12px; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    box-shadow: 0 0 16px rgba(13, 42, 82, 0.35);
    transition: box-shadow 0.2s ease, background 0.15s ease;
  }
  button.envoyer:active { background: var(--navy-dark); box-shadow: 0 0 24px rgba(13, 42, 82, 0.55); }
  button.envoyer:disabled { opacity: 0.7; }

  .pied {
    text-align: center; font-size: 13px; color: #667; margin-top: 16px;
  }
  .pied .ligne { display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 8px; }
  .pied a { color: var(--navy); font-weight: 600; text-decoration: none; }

  .erreur {
    background: #ffe9e9; color: #a30000; border: 1px solid #f3a3a3;
    padding: 12px 14px; border-radius: 10px; font-size: 14px; margin-bottom: 14px;
  }
  .banniere-admin {
    background: #0d2a52; color: #fff; padding: 10px 14px; border-radius: 10px;
    font-size: 13px; margin-bottom: 14px; text-align: center; font-weight: 600;
    animation: pulsation-douce 2.6s ease-in-out infinite;
  }
  .raccourcis {
    display: flex; gap: 8px; overflow-x: auto; margin: 8px 0 4px; padding-bottom: 2px;
    -webkit-overflow-scrolling: touch;
  }
  .raccourcis::-webkit-scrollbar { height: 4px; }
  .raccourcis button {
    flex-shrink: 0; padding: 8px 13px; border-radius: 20px; border: 1.5px solid var(--bordure);
    background: #fafbfc; font-size: 13px; font-weight: 600; color: #444; cursor: pointer;
    white-space: nowrap; display: inline-flex; align-items: center; gap: 6px;
  }
  .raccourcis button:active { background: #eef2f7; border-color: var(--navy); color: var(--navy); }

  /* --- Nouveaux styles page publique (client) uniquement --- */
  .badge-mode {
    display: inline-flex; align-items: center; gap: 6px; margin-top: 14px;
    padding: 6px 13px; border-radius: 20px; font-size: 13px; font-weight: 700;
    border: 1.5px solid transparent;
  }
  @media (max-width: 380px) {
    .badge-mode, .badge-partenaire { margin-top: 10px; }
  }
  .badge-mode-prive { background: #eef2f7; color: var(--navy); border-color: #d5deea; }
  .badge-mode-medical { background: var(--vert-clair); color: var(--vert); border-color: #cdeed9; }
  .badge-mode svg { flex-shrink: 0; }

  .choix-client { display: flex; gap: 10px; margin: 6px 0 4px; }
  .choix-client label {
    flex: 1; margin: 0; display: flex; align-items: center; gap: 8px;
    padding: 12px 12px; border: 1.5px solid var(--bordure);
    border-radius: 10px; font-weight: 600; cursor: pointer; font-size: 14px; color: #444;
  }
  .choix-client input { display: none; }
  .choix-client label svg.icone-transport { flex-shrink: 0; }
  .choix-client .choix-texte { flex: 1; }
  .choix-client .choix-coche {
    display: none; width: 20px; height: 20px; border-radius: 50%; flex-shrink: 0;
    align-items: center; justify-content: center; color: #fff;
  }
  .choix-client label:has(#type_prive:checked) {
    border-color: var(--navy); background: #eef2f7; color: var(--navy);
  }
  .choix-client label:has(#type_prive:checked) .choix-coche { display: inline-flex; background: var(--navy); }
  .choix-client label:has(#type_medical:checked) {
    border-color: var(--vert); background: var(--vert-clair); color: var(--vert);
  }
  .choix-client label:has(#type_medical:checked) .choix-coche { display: inline-flex; background: var(--vert); }

  .switch-option {
    display: flex; align-items: center; gap: 10px; margin-top: 12px;
    font-size: 14px; color: #444; cursor: pointer;
  }
  .switch-option input { display: none; }
  .switch-track {
    width: 38px; height: 22px; border-radius: 20px; background: #dde2e8;
    position: relative; flex-shrink: 0; transition: background 0.2s ease;
  }
  .switch-thumb {
    position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%;
    background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.25); transition: transform 0.2s ease;
  }
  .switch-option input:checked ~ .switch-track { background: var(--vert); }
  .switch-option input:checked ~ .switch-track .switch-thumb { transform: translateX(16px); }
  .switch-option svg.icone-switch { color: var(--vert); flex-shrink: 0; }
  .switch-option-bleu input:checked ~ .switch-track { background: var(--navy); }
</style>
</head>
<body>
<div class="page">

  <div class="entete">
    <img class="logo-entete" src="/logo-horizontal.png" alt="Centrale des Taxis Niçois">
    {% if role == 'secretaire' %}
    <p class="entete-soustitre">Réservez le transport de votre patient en quelques instants</p>
    <div class="badge-partenaire">Mode partenaire</div>
    {% elif mode_admin %}
    <p class="entete-soustitre">Réservez votre course en quelques instants</p>
    {% else %}
    <p id="soustitre_dynamique" class="entete-soustitre">{% if valeurs.get('type_course') == 'medical' %}Votre transport médical en toute sérénité{% else %}Réservez votre course en quelques instants{% endif %}</p>
    <div id="badge_mode" class="badge-mode {% if valeurs.get('type_course') == 'medical' %}badge-mode-medical{% else %}badge-mode-prive{% endif %}">
      <span id="badge_mode_icone" style="display:inline-flex;">
        {% if valeurs.get('type_course') == 'medical' %}
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M12 8v6M9 11h6"/></svg>
        {% else %}
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>
        {% endif %}
      </span>
      <span id="badge_mode_texte">{% if valeurs.get('type_course') == 'medical' %}Conventionné • Disponible 24h/24{% else %}Disponible 24h/24{% endif %}</span>
    </div>
    {% endif %}
  </div>

  {% if erreur %}<div class="erreur">{{ erreur }}</div>{% endif %}
  {% if mode_admin and role != 'secretaire' %}
    <div class="banniere-admin">MODE ADMINISTRATEUR</div>
  {% endif %}

  <form method="POST" action="/reserver">
    {% if mode_admin %}<input type="hidden" name="admin_code" value="{{ admin_code }}">{% endif %}

    <div class="grille-haut">
    <div class="carte">
      <div class="section-titre">
        <div class="numero">1</div>
        <h2>{% if role == 'secretaire' %}Patient{% else %}Vos coordonnees{% endif %}</h2>
      </div>

      {% if role == 'secretaire' %}
      <label for="patient_nom_complet">Nom et prénom du patient</label>
      <div class="champ-icone">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
        <input type="text" id="patient_nom_complet" name="patient_nom_complet" placeholder="Ex. : Dupont Jean"
               value="{{ valeurs.get('patient_nom_complet', '') }}" required>
      </div>
      {% else %}
      <div class="ligne-double">
        <div>
          <label for="prenom">Prénom</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
            <input type="text" id="prenom" name="prenom" placeholder="Ex : Jean (pas obligatoire)" value="{{ valeurs.get('prenom', '') }}">
          </div>
        </div>
        <div>
          <label for="nom">Nom</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
            <input type="text" id="nom" name="nom" value="{{ valeurs.get('nom', '') }}" required>
          </div>
        </div>
      </div>
      {% endif %}

      <label for="telephone">{% if role == 'secretaire' %}Telephone de contact{% else %}Téléphone{% endif %}</label>
      <div class="champ-icone">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.81.3 1.6.54 2.37a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.71-1.11a2 2 0 0 1 2.11-.45c.77.24 1.56.42 2.37.54A2 2 0 0 1 22 16.92z"/></svg>
        <input type="tel" id="telephone" name="telephone" placeholder="06 12 34 56 78"
               value="{{ valeurs.get('telephone', '') }}" required>
      </div>

      {% if role == 'secretaire' %}
      <div id="bloc_infirmiere_affichage" class="infirmiere-affichage" style="display:none;">
        <span>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
          Reservation par : <strong id="nom_infirmiere_affiche"></strong>
        </span>
        <button type="button" id="bouton_modifier_infirmiere">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          Modifier
        </button>
      </div>
      <div id="bloc_infirmiere_saisie">
        <label for="nom_infirmiere">Nom de la secretaire / infirmiere</label>
        <div class="champ-icone">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
          <input type="text" id="nom_infirmiere" name="nom_infirmiere"
                 value="{{ valeurs.get('nom_infirmiere', '') }}" required>
        </div>
      </div>
      {% endif %}
    </div>

    <div class="carte">
      <div class="section-titre">
        <div class="numero">2</div>
        <h2>{% if role == 'secretaire' %}Trajet{% else %}Votre trajet{% endif %}</h2>
        {% if role == 'secretaire' %}
        <span class="badge-medical">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>
          Transport medical
        </span>
        {% endif %}
      </div>

      {% if role == 'secretaire' %}
        <input type="hidden" name="type_course" value="medical">
      {% elif mode_admin %}
      <div class="choix">
        <label for="type_prive">
          <input type="radio" id="type_prive" name="type_course" value="prive"
                 {% if valeurs.get('type_course', 'prive') == 'prive' %}checked{% endif %}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 11l1.5-4.5A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.5L19 11"/><rect x="3" y="11" width="18" height="6" rx="2"/><circle cx="7.5" cy="17.5" r="1.5"/><circle cx="16.5" cy="17.5" r="1.5"/></svg>
          Course privee
        </label>
        <label for="type_medical">
          <input type="radio" id="type_medical" name="type_course" value="medical"
                 {% if valeurs.get('type_course') == 'medical' %}checked{% endif %}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>
          Transport medical
        </label>
      </div>

      <div id="options_medical" class="options-medical" style="display: {% if valeurs.get('type_course') == 'medical' %}block{% else %}none{% endif %};">
        <label style="display:flex; align-items:center; gap:8px; font-weight:400; font-size:14px; color:#777; margin:10px 0 0;">
          <input type="checkbox" id="accompagnant" name="accompagnant" value="oui" style="width:auto;"
                 {% if valeurs.get('accompagnant') %}checked{% endif %}>
          J'aurai un accompagnant avec moi
        </label>
        <label style="display:flex; align-items:center; gap:8px; font-weight:400; font-size:14px; color:#777; margin:8px 0 0;">
          <input type="checkbox" id="bto_retour" name="bto_retour" value="oui" style="width:auto;"
                 {% if valeurs.get('bto_retour') %}checked{% endif %}>
          Je donnerai mon bon de transport au retour
        </label>
      </div>
      {% else %}
      <div class="choix-client">
        <label for="type_prive">
          <svg class="icone-transport" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 11l1.5-4.5A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.5L19 11"/><rect x="3" y="11" width="18" height="6" rx="2"/><circle cx="7.5" cy="17.5" r="1.5"/><circle cx="16.5" cy="17.5" r="1.5"/></svg>
          <span class="choix-texte">Course privée</span>
          <span class="choix-coche">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6 9 17l-5-5"/></svg>
          </span>
          <input type="radio" id="type_prive" name="type_course" value="prive"
                 {% if valeurs.get('type_course', 'prive') == 'prive' %}checked{% endif %}>
        </label>
        <label for="type_medical">
          <svg class="icone-transport" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>
          <span class="choix-texte">Transport médical</span>
          <span class="choix-coche">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6 9 17l-5-5"/></svg>
          </span>
          <input type="radio" id="type_medical" name="type_course" value="medical"
                 {% if valeurs.get('type_course') == 'medical' %}checked{% endif %}>
        </label>
      </div>

      <div id="options_medical" class="options-medical" style="display: {% if valeurs.get('type_course') == 'medical' %}block{% else %}none{% endif %};">
        <label class="switch-option" for="accompagnant">
          <input type="checkbox" id="accompagnant" name="accompagnant" value="oui"
                 {% if valeurs.get('accompagnant') %}checked{% endif %}>
          <span class="switch-track"><span class="switch-thumb"></span></span>
          <svg class="icone-switch" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
          Je voyage avec un accompagnant
        </label>
        <label class="switch-option" for="bto_retour">
          <input type="checkbox" id="bto_retour" name="bto_retour" value="oui"
                 {% if valeurs.get('bto_retour') %}checked{% endif %}>
          <span class="switch-track"><span class="switch-thumb"></span></span>
          <svg class="icone-switch" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h3"/></svg>
          Je remettrai mon bon de transport au retour
        </label>
      </div>
      {% endif %}

      <div class="adresses" style="margin-top: 8px;">
        <label for="prise_en_charge">Adresse de prise en charge</label>
        <div class="champ-icone">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>
          <input type="text" id="prise_en_charge" name="prise_en_charge"
                 placeholder="Ex : 12 av. de la Republique"
                 value="{{ valeurs.get('prise_en_charge', '') }}" required>
        </div>

        {% if role == 'secretaire' %}
        <label style="margin-top: 14px;">Etablissements favoris</label>
        <div class="raccourcis">
          {% for nom_etablissement in ['Les Sources B1', 'Pasteur', "L'Archet", 'Saint-Georges', 'Lenval', 'Antoine Lacassagne', 'Parc Imperial', 'Saint-Antoine', 'Santa Maria', 'Saint-Francois', 'Cimiez', 'Saint Jean', 'Tzanck'] %}
          <button type="button" onclick="document.getElementById('prise_en_charge').value = '{{ nom_etablissement }}'">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21h18M6 21V7l6-4 6 4v14M9 9h1M9 13h1M14 9h1M14 13h1M10 21v-4h4v4"/></svg>
            {{ nom_etablissement }}
          </button>
          {% endfor %}
        </div>
        {% endif %}

        <button type="button" class="bouton-inverser" id="bouton_inverser" aria-label="Inverser les adresses" style="top: 78px;">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v14M4 13l4 4 4-4"/><path d="M16 21V7M12 11l4-4 4 4"/></svg>
        </button>

        <label for="destination" id="destination_label_texte">{% if not mode_admin and role != 'secretaire' and valeurs.get('type_course') == 'medical' %}Établissement ou destination{% else %}Destination{% endif %}</label>
        <div class="champ-icone">
          <span id="destination_icone" style="display:inline-flex;">
            {% if not mode_admin and role != 'secretaire' and valeurs.get('type_course') == 'medical' %}
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><path d="M12 7v6M9 10h6"/></svg>
            {% else %}
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>
            {% endif %}
          </span>
          <input type="text" id="destination" name="destination"
                 placeholder="{% if not mode_admin and role != 'secretaire' and valeurs.get('type_course') == 'medical' %}Ex : Hopital Pasteur 2{% else %}Ex : Aeroport de Nice{% endif %}"
                 value="{{ valeurs.get('destination', '') }}" required>
        </div>
      </div>
    </div>
    </div>

    <div class="carte">
      <div class="section-titre">
        <div class="numero">3</div>
        <h2>Date et horaire</h2>
      </div>

      <div class="ligne-double-souple">
        <div>
          <label for="date">Date du trajet</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></svg>
            <input type="date" id="date" name="date" min="{{ date_min }}"
                   value="{{ valeurs.get('date', '') }}" required>
          </div>
        </div>
        <div id="emplacement_horaire_ligne">
          {% if role != 'secretaire' and valeurs.get('type_course', 'prive') == 'prive' %}
          <div id="bloc_heure_pc">
            <label for="heure_pc">Heure de prise en charge</label>
            <div class="champ-icone">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
              <input type="time" id="heure_pc" name="heure_pc" value="{{ valeurs.get('heure_pc', '') }}">
            </div>
          </div>
          {% else %}
          <div id="bloc_heure_rdv">
            <label for="heure_rdv">Heure de rendez-vous</label>
            <div class="champ-icone">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
              <input type="time" id="heure_rdv" name="heure_rdv" value="{{ valeurs.get('heure_rdv', '') }}">
            </div>
          </div>
          {% endif %}
        </div>
      </div>

      <div id="emplacement_horaire_pleine_largeur">
        {% if role != 'secretaire' and valeurs.get('type_course', 'prive') == 'prive' %}
        <div id="bloc_heure_rdv">
          <label for="heure_rdv">Heure de rendez-vous</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
            <input type="time" id="heure_rdv" name="heure_rdv" value="{{ valeurs.get('heure_rdv', '') }}">
          </div>
        </div>
        {% else %}
        <div id="bloc_heure_pc">
          <label for="heure_pc">Heure de prise en charge</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
            <input type="time" id="heure_pc" name="heure_pc" value="{{ valeurs.get('heure_pc', '') }}">
          </div>
        </div>
        {% endif %}
      </div>

      <label class="switch-option switch-option-bleu" for="heure_inconnue" style="margin-top:14px;">
        <input type="checkbox" id="heure_inconnue" name="heure_inconnue" value="oui"
               {% if valeurs.get('heure_inconnue') %}checked{% endif %}>
        <span class="switch-track"><span class="switch-thumb"></span></span>
        Calcul automatique de l'heure de prise en charge
      </label>
    </div>

    <button type="submit" class="envoyer">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/><path d="m9 16 2 2 4-4"/></svg>
      <span id="bouton_texte">{% if not mode_admin and role != 'secretaire' and valeurs.get('type_course') == 'medical' %}Réserver mon transport{% else %}Réserver la course{% endif %}</span>
    </button>
  </form>

  <div class="pied">
    {% if not mode_admin %}
    <div class="ligne">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      Vous recevrez une confirmation par SMS
    </div>
    {% endif %}
    <div class="ligne">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.81.3 1.6.54 2.37a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.71-1.11a2 2 0 0 1 2.11-.45c.77.24 1.56.42 2.37.54A2 2 0 0 1 22 16.92z"/></svg>
      Besoin d'aide ? <a href="tel:+33624836448">Appelez la centrale</a>
    </div>
  </div>
</div>

<script>
  const caseInconnue = document.getElementById('heure_inconnue');
  const champPC = document.getElementById('heure_pc');
  const champRDV = document.getElementById('heure_rdv');

  function majEtatsChamps() {
    const inconnue = caseInconnue.checked;
    champPC.disabled = inconnue;
    champPC.required = !inconnue;
    if (inconnue) { champPC.value = ''; }
    champRDV.required = inconnue;
  }
  caseInconnue.addEventListener('change', majEtatsChamps);
  majEtatsChamps();

  // Bouton pour inverser l'adresse de prise en charge et la destination.
  document.getElementById('bouton_inverser').addEventListener('click', function () {
    const pc = document.getElementById('prise_en_charge');
    const dest = document.getElementById('destination');
    const temp = pc.value;
    pc.value = dest.value;
    dest.value = temp;
  });

  // Affiche les options specifiques au transport medical (accompagnant,
  // bon de transport retour) uniquement quand ce type est selectionne.
  // Sur la page publique (client), synchronise aussi le sous-titre, le
  // badge, le libelle/icone du champ destination et le texte du bouton.
  const radioPrive = document.getElementById('type_prive');
  const radioMedical = document.getElementById('type_medical');
  const optionsMedical = document.getElementById('options_medical');
  if (radioPrive && radioMedical && optionsMedical) {
    const iconePin = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>';
    const iconeHopital = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><path d="M12 7v6M9 10h6"/></svg>';
    const iconeBadgePrive = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>';
    const iconeBadgeMedical = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M12 8v6M9 11h6"/></svg>';

    function majOptionsMedical() {
      const estMedical = radioMedical.checked;
      optionsMedical.style.display = estMedical ? 'block' : 'none';

      const sousTitre = document.getElementById('soustitre_dynamique');
      if (sousTitre) {
        sousTitre.textContent = estMedical
          ? 'Votre transport médical en toute sérénité'
          : 'Réservez votre course en quelques instants';
      }

      const badge = document.getElementById('badge_mode');
      const badgeTexte = document.getElementById('badge_mode_texte');
      const badgeIcone = document.getElementById('badge_mode_icone');
      if (badge && badgeTexte && badgeIcone) {
        badge.classList.toggle('badge-mode-medical', estMedical);
        badge.classList.toggle('badge-mode-prive', !estMedical);
        badgeTexte.textContent = estMedical ? 'Conventionné • Disponible 24h/24' : 'Disponible 24h/24';
        badgeIcone.innerHTML = estMedical ? iconeBadgeMedical : iconeBadgePrive;
      }

      const labelDest = document.getElementById('destination_label_texte');
      const iconeDest = document.getElementById('destination_icone');
      const champDest = document.getElementById('destination');
      if (labelDest && iconeDest && champDest) {
        labelDest.textContent = estMedical ? 'Établissement ou destination' : 'Destination';
        iconeDest.innerHTML = estMedical ? iconeHopital : iconePin;
        champDest.placeholder = estMedical ? 'Ex : Hopital Pasteur 2' : 'Ex : Aeroport de Nice';
      }

      const texteBouton = document.getElementById('bouton_texte');
      if (texteBouton) {
        texteBouton.textContent = estMedical ? 'Réserver mon transport' : 'Réserver la course';
      }

      const emplacementLigne = document.getElementById('emplacement_horaire_ligne');
      const emplacementPleine = document.getElementById('emplacement_horaire_pleine_largeur');
      const blocRdv = document.getElementById('bloc_heure_rdv');
      const blocPc = document.getElementById('bloc_heure_pc');
      if (emplacementLigne && emplacementPleine && blocRdv && blocPc) {
        if (estMedical) {
          emplacementLigne.appendChild(blocRdv);
          emplacementPleine.appendChild(blocPc);
        } else {
          emplacementLigne.appendChild(blocPc);
          emplacementPleine.appendChild(blocRdv);
        }
      }
    }
    radioPrive.addEventListener('change', majOptionsMedical);
    radioMedical.addEventListener('change', majOptionsMedical);
    majOptionsMedical();
  }

  // Empeche les doubles reservations en cas de double-clic ou d'appui
  // rapide sur le bouton "Confirmer ma reservation".
  const formulaire = document.querySelector('form');
  const boutonEnvoi = document.querySelector('button.envoyer');
  formulaire.addEventListener('submit', function () {
    boutonEnvoi.disabled = true;
    boutonEnvoi.textContent = 'Envoi en cours...';
  });

  // Memorise le dernier nom de secretaire/infirmiere saisi sur cet appareil,
  // et affiche "Reservation effectuee par : NOM" avec un bouton Modifier
  // au lieu de faire retaper le champ a chaque fois.
  const champInfirmiere = document.getElementById('nom_infirmiere');
  if (champInfirmiere) {
    const blocAffichage = document.getElementById('bloc_infirmiere_affichage');
    const blocSaisie = document.getElementById('bloc_infirmiere_saisie');
    const nomAffiche = document.getElementById('nom_infirmiere_affiche');
    const boutonModifier = document.getElementById('bouton_modifier_infirmiere');
    const dernierNom = localStorage.getItem('dernier_nom_infirmiere');

    if (!champInfirmiere.value && dernierNom) {
      champInfirmiere.value = dernierNom;
      nomAffiche.textContent = dernierNom;
      blocAffichage.style.display = 'flex';
      blocSaisie.style.display = 'none';
    }

    boutonModifier.addEventListener('click', function () {
      blocAffichage.style.display = 'none';
      blocSaisie.style.display = 'block';
      champInfirmiere.focus();
      champInfirmiere.select();
    });

    formulaire.addEventListener('submit', function () {
      if (champInfirmiere.value) {
        localStorage.setItem('dernier_nom_infirmiere', champInfirmiere.value);
      }
    });
  }
</script>
</body>
</html>
"""

CONFIRMATION_RESERVATION_HTML = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reservation confirmee</title>
<style>
  body {
    margin: 0; padding: 24px 16px; background: #f4f5f7;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: #1a1a1a;
  }
  .carte {
    max-width: 480px; margin: 40px auto 0; background: #ffffff; border-radius: 16px;
    padding: 28px 22px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); text-align: center;
  }
  .coche {
    width: 56px; height: 56px; border-radius: 50%; background: #e7f6ec; color: #1e8e3e;
    display: flex; align-items: center; justify-content: center; font-size: 30px;
    margin: 0 auto 16px;
  }
  h1 { font-size: 20px; margin: 0 0 12px; }
  table { width: 100%; text-align: left; margin-top: 18px; font-size: 15px; }
  td { padding: 6px 0; border-bottom: 1px solid #eee; }
  td.libelle { color: #777; width: 40%; }
  .ref {
    display: inline-block; margin-top: 18px; padding: 10px 16px; background: #fff6e6;
    border: 1px solid #f6a300; border-radius: 10px; font-weight: 700; letter-spacing: 1px;
  }
  a.retour { display: inline-block; margin-top: 24px; color: #555; font-size: 14px; }
</style>
</head>
<body>
  <div class="carte">
    <div class="coche">&#10003;</div>
    <h1>Votre taxi est reserve</h1>
    <table>
      <tr><td class="libelle">Nom</td><td>{{ donnees['nom'] }}</td></tr>
      <tr><td class="libelle">Prise en charge</td><td>{{ donnees['prise_en_charge'] }}</td></tr>
      <tr><td class="libelle">Destination</td><td>{{ donnees['destination'] }}</td></tr>
      <tr><td class="libelle">Heure de passage</td><td>{{ donnees['heure'] }}</td></tr>
      {% if donnees.get('heure_rdv') %}
      <tr><td class="libelle">Rendez-vous</td><td>{{ donnees['heure_rdv'] }}</td></tr>
      {% endif %}
    </table>
    <div class="ref">Reference : {{ reference }}</div>
    {% if mode_admin %}
    <p style="font-size:13px;color:#0d2a52;margin-top:14px;font-weight:600;">
      Ajoutee a l'agenda -- {% if role == 'secretaire' %}mode partenaire{% else %}mode admin{% endif %}, aucun SMS envoye au client.
    </p>
    {% else %}
    <p style="font-size:13px;color:#a30000;margin-top:14px;font-weight:600;">
      Conservez cette reference : elle vous sera demandee pour annuler ou
      modifier votre reservation (par SMS ou par telephone).
    </p>
    <p style="font-size:13px;color:#777;margin-top:10px;">
      Un SMS de confirmation avec cette reference vient de vous etre envoye.
      Un chauffeur vous contactera peu avant son arrivee.
    </p>
    {% endif %}
    <a class="retour" href="/reserver{% if mode_admin %}?admin={{ admin_code }}{% endif %}">Faire une nouvelle reservation</a>
  </div>
</body>
</html>
"""


@app.route("/logo-horizontal.png", methods=["GET"])
def logo_horizontal():
    """Sert le logo d'en-tete depuis un fichier image separe (et non en
    base64 dans ce fichier .py) -- evite les problemes de troncature deja
    rencontres avec ce fichier lors d'une edition en ligne sur GitHub."""
    return send_from_directory(app.root_path, "logo-horizontal-taxis.png", mimetype="image/png")


@app.route("/icon-<int:taille>.png", methods=["GET"])
def icone_ecran_accueil(taille):
    """Sert les icones carrees (favicon, iOS, Android/manifest). Les
    fichiers icon-taxis-<taille>.png doivent etre presents a la racine
    du depot, a cote de ce script."""
    if taille not in (32, 96, 180, 192, 512):
        return "Not found", 404
    return send_from_directory(app.root_path, f"icon-taxis-{taille}.png", mimetype="image/png")


@app.route("/icon-<int:taille>-maskable.png", methods=["GET"])
def icone_ecran_accueil_maskable(taille):
    """Variante avec marge de securite pour les icones adaptatives
    Android ('purpose: maskable')."""
    if taille not in (192, 512):
        return "Not found", 404
    return send_from_directory(app.root_path, f"icon-taxis-{taille}-maskable.png", mimetype="image/png")


@app.route("/manifest.json", methods=["GET"])
def manifest_web_app():
    """Manifest PWA : permet d'installer une icone nette en plein ecran.
    Le code d'acces admin/secretaire est embarque dans start_url (recupere
    depuis le lien du manifest lui-meme, cf. balise <link rel="manifest">
    dans le HTML) pour que l'icone ajoutee a l'ecran d'accueil rouvre
    directement le formulaire deja deverrouille."""
    code = request.args.get("admin", "")
    start_url = f"/reserver?admin={code}" if code else "/reserver"
    manifest = {
        "name": "Centrale des Taxis Niçois",
        "short_name": "Taxis Niçois",
        "start_url": start_url,
        "scope": "/",
        "display": "browser",
        "background_color": "#f4f5f7",
        "theme_color": "#0d2a52",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return json.dumps(manifest, ensure_ascii=False), 200, {"Content-Type": "application/manifest+json"}


@app.route("/", methods=["GET"])
def racine():
    code = request.args.get("admin", "")
    destination = f"/reserver?admin={code}" if code else "/reserver"
    return redirect(destination)


@app.route("/reserver", methods=["GET"])
def page_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    code_saisi = request.args.get("admin")
    role = determiner_role(code_saisi)
    return render_template_string(
        FORMULAIRE_RESERVATION_HTML, erreur=None, date_min=date_min, valeurs={},
        mode_admin=(role is not None), role=role,
        admin_code=code_saisi if role else "",
    )


@app.route("/reserver", methods=["POST"])
def valider_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    valeurs = request.form.to_dict()
    code_saisi = request.form.get("admin_code")
    role = determiner_role(code_saisi)
    mode_admin = role is not None

    def page_erreur(message: str):
        return render_template_string(
            FORMULAIRE_RESERVATION_HTML, erreur=message, date_min=date_min, valeurs=valeurs,
            mode_admin=mode_admin, role=role,
            admin_code=code_saisi if role else "",
        )

    prenom = (request.form.get("prenom") or "").strip()
    nom = (request.form.get("nom") or "").strip()
    patient_nom_complet = (request.form.get("patient_nom_complet") or "").strip()
    telephone_saisi = (request.form.get("telephone") or "").strip()
    type_course = "medical" if role == "secretaire" else (request.form.get("type_course") or "prive")
    accompagnant = request.form.get("accompagnant") == "oui"
    bto_retour = request.form.get("bto_retour") == "oui"
    nom_infirmiere = (request.form.get("nom_infirmiere") or "").strip()
    prise_en_charge = (request.form.get("prise_en_charge") or "").strip()
    destination = (request.form.get("destination") or "").strip()
    date_str = (request.form.get("date") or "").strip()
    heure_rdv_saisie = (request.form.get("heure_rdv") or "").strip()
    heure_pc_saisie = (request.form.get("heure_pc") or "").strip()
    heure_inconnue = request.form.get("heure_inconnue") == "oui"

    if role == "secretaire":
        if not all([patient_nom_complet, telephone_saisi, prise_en_charge, destination, date_str]):
            return page_erreur("Merci de remplir tous les champs du formulaire.")
    elif not all([nom, telephone_saisi, prise_en_charge, destination, date_str]):
        return page_erreur("Merci de remplir tous les champs du formulaire.")

    if role == "secretaire" and not nom_infirmiere:
        return page_erreur("Merci d'indiquer le nom de la secretaire ou de l'infirmiere.")

    # Nom complet utilise partout ensuite (agenda, email, SMS). En mode
    # secretaire, un seul champ "nom et prenom du patient" est saisi ; sinon
    # on garde prenom + nom separes comme avant.
    if role == "secretaire":
        nom_complet = patient_nom_complet
        nom_pour_agenda = patient_nom_complet
    else:
        nom_complet = f"{prenom} {nom}".strip()
        # Format specifique pour l'agenda : NOM avant Prenom.
        nom_pour_agenda = f"{nom} {prenom}".strip()

    telephone = normaliser_numero_francais(telephone_saisi)

    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        return page_erreur("La date saisie n'est pas valide.")

    donnees = {
        "type": "medical" if type_course == "medical" else "prive",
        "nom": nom_complet,
        "nom_agenda": nom_pour_agenda,
        "mode_admin": mode_admin,
        "role": role,
        "nom_infirmiere": nom_infirmiere or None,
        "accompagnant": accompagnant,
        "bto_retour": bto_retour,
        "telephone": telephone,
        "prise_en_charge": prise_en_charge,
        "destination": destination,
        "heure": None,
        "heure_rdv": None,
        "heure_iso": None,
    }

    # L'heure de rendez-vous est toujours facultative -- si elle est fournie,
    # on la garde pour affichage/agenda, meme quand l'heure de prise en
    # charge est aussi connue directement.
    if heure_rdv_saisie:
        heure_minute_rdv = parser_heure_texte(heure_rdv_saisie.replace(":", "h"))
        if not heure_minute_rdv:
            return page_erreur("L'heure de rendez-vous saisie n'est pas valide.")
        rdv_h, rdv_m = heure_minute_rdv
        donnees["heure_rdv"] = f"{rdv_h:02d}h{rdv_m:02d}"

    heure_estimee = False

    if heure_inconnue:
        if not heure_rdv_saisie:
            return page_erreur(
                "Merci d'indiquer l'heure de rendez-vous pour que la centrale "
                "puisse calculer automatiquement l'heure de prise en charge."
            )
        duree_trajet = estimer_duree_trajet(
            completer_adresse_avec_ville(resoudre_adresse_medicale(prise_en_charge)),
            completer_adresse_avec_ville(resoudre_adresse_medicale(destination)),
        )
        if duree_trajet is None:
            return page_erreur(
                "Impossible d'estimer automatiquement l'heure de prise en charge pour "
                "ce trajet. Merci de renseigner directement l'heure de prise en "
                "charge, ou d'appeler la centrale."
            )
        rdv_h, rdv_m = parser_heure_texte(heure_rdv_saisie.replace(":", "h"))
        date_rdv = datetime.fromisoformat(date_str).replace(
            hour=rdv_h, minute=rdv_m, tzinfo=FUSEAU_HORAIRE
        )
        marge_securite_minutes = 15 if duree_trajet < 30 else 30
        heure_pc_dt = date_rdv - timedelta(minutes=duree_trajet + marge_securite_minutes)
        minutes_totales = heure_pc_dt.hour * 60 + heure_pc_dt.minute
        minutes_arrondies = round(minutes_totales / 5) * 5
        heure_pc_dt = heure_pc_dt.replace(hour=0, minute=0) + timedelta(minutes=minutes_arrondies)
        donnees["heure_iso"] = heure_pc_dt.replace(tzinfo=None).isoformat()
        donnees["heure"] = heure_pc_dt.strftime("%Hh%M")
        heure_estimee = True
    else:
        if not heure_pc_saisie:
            return page_erreur(
                "Merci d'indiquer l'heure de prise en charge, ou de cocher la "
                "case si vous ne la connaissez pas."
            )
        heure_minute_pc = parser_heure_texte(heure_pc_saisie.replace(":", "h"))
        if not heure_minute_pc:
            return page_erreur("L'heure de prise en charge saisie n'est pas valide.")
        pc_h, pc_m = heure_minute_pc
        donnees["heure"] = f"{pc_h:02d}h{pc_m:02d}"
        pc_dt = datetime.fromisoformat(date_str).replace(hour=pc_h, minute=pc_m)
        donnees["heure_iso"] = pc_dt.isoformat()

    reservations_en_cours = rechercher_evenements(telephone, seulement_futur=True)
    if len(reservations_en_cours) >= MAX_RESERVATIONS_ACTIVES:
        return page_erreur(
            f"Vous avez deja {MAX_RESERVATIONS_ACTIVES} reservations en cours avec ce numero. "
            "Merci d'appeler la centrale pour en annuler une avant d'en ajouter une nouvelle."
        )

    # Protection anti-doublon : si un evenement pour ce numero existe deja
    # avec exactement la meme adresse de prise en charge, destination et
    # heure de depart, c'est tres probablement un double-clic / une double
    # soumission du formulaire -> on renvoie la confirmation de la
    # reservation existante au lieu d'en creer une deuxieme.
    for evenement in reservations_en_cours:
        debut_existant = evenement.get("start", {}).get("dateTime", "")
        description_existante = evenement.get("description", "")
        if (
            debut_existant.startswith(donnees["heure_iso"])
            and donnees["prise_en_charge"].upper() in description_existante.upper()
            and donnees["destination"].upper() in description_existante.upper()
        ):
            reference_existante = extraire_reference_de_description(description_existante)
            log.info(
                "Doublon detecte pour %s (ref existante %s), pas de nouvelle creation",
                telephone, reference_existante,
            )
            return render_template_string(
                CONFIRMATION_RESERVATION_HTML, donnees=donnees, reference=reference_existante,
                mode_admin=mode_admin, role=role, admin_code=code_saisi if role else "",
            )

    reference = generer_reference()
    succes, detail, event_id = creer_evenement_agenda(donnees, reference)
    if not succes:
        log.error("Echec creation reservation web : %s", detail)
        return page_erreur(
            "Une erreur technique empeche la validation de votre reservation en ligne. "
            "Merci d'appeler directement la centrale pour reserver votre taxi."
        )

    if mode_admin:
        if role == "secretaire":
            # Mode partenaire/secretaire : pas de SMS au client, mais on
            # notifie quand meme Tony par email pour qu'il soit au courant.
            envoyer_email_confirmation(donnees, reference)
        log.info(
            "Reservation %s creee : %s (ref %s, tel %s, event %s) -- pas de SMS envoye",
            (role or "admin").upper(), nom_complet, reference, telephone, event_id,
        )
    else:
        envoyer_email_confirmation(donnees, reference)
        texte_sms = construire_sms_confirmation(donnees, reference, heure_estimee)
        envoyer_sms(telephone, texte_sms)
        log.info(
            "Reservation web creee : %s (ref %s, tel %s, event %s)",
            nom_complet, reference, telephone, event_id,
        )

    return render_template_string(
        CONFIRMATION_RESERVATION_HTML, donnees=donnees, reference=reference, mode_admin=mode_admin,
        role=role, admin_code=code_saisi if role else "",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
