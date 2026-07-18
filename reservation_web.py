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
from flask import Flask, request, render_template_string
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
    telephone = donnees.get("telephone") or "(non renseigne)"
    heure_aff = debut_dt.strftime("%Hh%M")
    heure_rdv_aff = donnees.get("heure_rdv") or heure_aff

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
        f"TEL : {telephone}\n"
        f"SOURCE : reservation en ligne"
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
  .entete { text-align: center; margin-bottom: 20px; }
  .entete svg { color: var(--navy); margin-bottom: 6px; }
  .entete h1 {
    font-size: 21px; margin: 0 0 4px; color: var(--navy); font-weight: 800;
  }
  .entete p { margin: 0; color: #667; font-size: 14px; }

  .carte {
    background: #ffffff; border-radius: 16px; padding: 20px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.06); margin-bottom: 14px;
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
  input:focus { outline: none; border-color: var(--navy); }

  .ligne-double { display: flex; gap: 10px; }
  .ligne-double > div { flex: 1; }

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
  }
  button.envoyer:active { background: var(--navy-dark); }
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
</style>
</head>
<body>
<div class="page">

  <div class="entete">
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M5 11l1.5-4.5A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.5L19 11"/>
      <rect x="3" y="11" width="18" height="6" rx="2"/>
      <circle cx="7.5" cy="17.5" r="1.5"/><circle cx="16.5" cy="17.5" r="1.5"/>
    </svg>
    <h1>Centrale des Taxis Nicois</h1>
    <p>Reservez votre course en quelques instants</p>
  </div>

  {% if erreur %}<div class="erreur">{{ erreur }}</div>{% endif %}

  <form method="POST" action="/reserver">

    <div class="carte">
      <div class="section-titre">
        <div class="numero">1</div>
        <h2>Vos coordonnees</h2>
      </div>

      <div class="ligne-double">
        <div>
          <label for="prenom">Prenom</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
            <input type="text" id="prenom" name="prenom" value="{{ valeurs.get('prenom', '') }}" required>
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

      <label for="telephone">Telephone</label>
      <div class="champ-icone">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.81.3 1.6.54 2.37a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.71-1.11a2 2 0 0 1 2.11-.45c.77.24 1.56.42 2.37.54A2 2 0 0 1 22 16.92z"/></svg>
        <input type="tel" id="telephone" name="telephone" placeholder="06 12 34 56 78"
               value="{{ valeurs.get('telephone', '') }}" required>
      </div>
    </div>

    <div class="carte">
      <div class="section-titre">
        <div class="numero">2</div>
        <h2>Votre trajet</h2>
      </div>

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

      <div class="adresses">
        <label for="prise_en_charge">Adresse de prise en charge</label>
        <div class="champ-icone">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>
          <input type="text" id="prise_en_charge" name="prise_en_charge"
                 placeholder="Ex : 12 avenue de la Republique, Nice"
                 value="{{ valeurs.get('prise_en_charge', '') }}" required>
        </div>

        <button type="button" class="bouton-inverser" id="bouton_inverser" aria-label="Inverser les adresses" style="top: 78px;">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v14M4 13l4 4 4-4"/><path d="M16 21V7M12 11l4-4 4 4"/></svg>
        </button>

        <label for="destination">Destination</label>
        <div class="champ-icone">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>
          <input type="text" id="destination" name="destination"
                 placeholder="Ex : Aeroport de Nice"
                 value="{{ valeurs.get('destination', '') }}" required>
        </div>
      </div>
    </div>

    <div class="carte">
      <div class="section-titre">
        <div class="numero">3</div>
        <h2>Date et horaire</h2>
      </div>

      <label for="date">Date du trajet</label>
      <div class="champ-icone">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></svg>
        <input type="date" id="date" name="date" min="{{ date_min }}"
               value="{{ valeurs.get('date', '') }}" required>
      </div>

      <label for="heure_rdv">Heure de rendez-vous</label>
      <div class="champ-icone">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
        <input type="time" id="heure_rdv" name="heure_rdv" value="{{ valeurs.get('heure_rdv', '') }}">
      </div>

      <label for="heure_pc">Heure de prise en charge</label>
      <div class="champ-icone">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
        <input type="time" id="heure_pc" name="heure_pc" value="{{ valeurs.get('heure_pc', '') }}">
      </div>

      <div class="case-auto">
        <input type="checkbox" id="heure_inconnue" name="heure_inconnue" value="oui"
               {% if valeurs.get('heure_inconnue') %}checked{% endif %}>
        <label for="heure_inconnue" style="margin:0; font-weight:400; color:#555;">
          Laissez la centrale calculer mon heure de prise en charge automatiquement selon le trajet.
        </label>
      </div>
    </div>

    <button type="submit" class="envoyer">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/><path d="m9 16 2 2 4-4"/></svg>
      Confirmer ma reservation
    </button>
  </form>

  <div class="pied">
    <div class="ligne">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      Vous recevrez une confirmation par SMS
    </div>
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

  // Empeche les doubles reservations en cas de double-clic ou d'appui
  // rapide sur le bouton "Confirmer ma reservation".
  const formulaire = document.querySelector('form');
  const boutonEnvoi = document.querySelector('button.envoyer');
  formulaire.addEventListener('submit', function () {
    boutonEnvoi.disabled = true;
    boutonEnvoi.textContent = 'Envoi en cours...';
  });
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
    <p style="font-size:13px;color:#a30000;margin-top:14px;font-weight:600;">
      Conservez cette reference : elle vous sera demandee pour annuler ou
      modifier votre reservation (par SMS ou par telephone).
    </p>
    <p style="font-size:13px;color:#777;margin-top:10px;">
      Un SMS de confirmation avec cette reference vient de vous etre envoye.
      Un chauffeur vous contactera peu avant son arrivee.
    </p>
    <a class="retour" href="/reserver">Faire une nouvelle reservation</a>
  </div>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def racine():
    return (
        "Page de reservation en ligne - operationnelle<br><br>"
        '<a href="/reserver">Acceder au formulaire de reservation</a>'
    ), 200


@app.route("/reserver", methods=["GET"])
def page_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    return render_template_string(
        FORMULAIRE_RESERVATION_HTML, erreur=None, date_min=date_min, valeurs={}
    )


@app.route("/reserver", methods=["POST"])
def valider_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    valeurs = request.form.to_dict()

    def page_erreur(message: str):
        return render_template_string(
            FORMULAIRE_RESERVATION_HTML, erreur=message, date_min=date_min, valeurs=valeurs
        )

    prenom = (request.form.get("prenom") or "").strip()
    nom = (request.form.get("nom") or "").strip()
    telephone_saisi = (request.form.get("telephone") or "").strip()
    type_course = request.form.get("type_course") or "prive"
    prise_en_charge = (request.form.get("prise_en_charge") or "").strip()
    destination = (request.form.get("destination") or "").strip()
    date_str = (request.form.get("date") or "").strip()
    heure_rdv_saisie = (request.form.get("heure_rdv") or "").strip()
    heure_pc_saisie = (request.form.get("heure_pc") or "").strip()
    heure_inconnue = request.form.get("heure_inconnue") == "oui"

    if not all([prenom, nom, telephone_saisi, prise_en_charge, destination, date_str]):
        return page_erreur("Merci de remplir tous les champs du formulaire.")

    # Nom complet utilise partout ensuite (agenda, email, SMS), pour garder
    # exactement la meme mise en forme qu'avant (un seul champ "nom") tout
    # en affichant le prenom en plus.
    nom_complet = f"{prenom} {nom}".strip()

    telephone = normaliser_numero_francais(telephone_saisi)

    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        return page_erreur("La date saisie n'est pas valide.")

    donnees = {
        "type": "medical" if type_course == "medical" else "prive",
        "nom": nom_complet,
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
                CONFIRMATION_RESERVATION_HTML, donnees=donnees, reference=reference_existante
            )

    reference = generer_reference()
    succes, detail, event_id = creer_evenement_agenda(donnees, reference)
    if not succes:
        log.error("Echec creation reservation web : %s", detail)
        return page_erreur(
            "Une erreur technique empeche la validation de votre reservation en ligne. "
            "Merci d'appeler directement la centrale pour reserver votre taxi."
        )

    envoyer_email_confirmation(donnees, reference)
    texte_sms = construire_sms_confirmation(donnees, reference, heure_estimee)
    envoyer_sms(telephone, texte_sms)

    log.info(
        "Reservation web creee : %s (ref %s, tel %s, event %s)",
        nom, reference, telephone, event_id,
    )

    return render_template_string(
        CONFIRMATION_RESERVATION_HTML, donnees=donnees, reference=reference
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
