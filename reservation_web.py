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

# Photo de l'agent affichee en haut de la page de reservation (encodee en
# base64 pour rester dans un seul fichier autonome, sans dependance a un
# hebergement d'image externe).
PHOTO_AGENT_B64 = "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"

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
  .entete { text-align: center; margin-bottom: 20px; position: relative; padding-top: 4px; }
  .entete svg { color: var(--navy); margin-bottom: 6px; }
  .entete .photo-agent {
    position: absolute; top: 0; left: 0; width: 56px; height: 56px;
    border-radius: 50%; object-fit: cover; box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    border: 2px solid #fff;
  }
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
    <img class="photo-agent" src="data:image/jpeg;base64,{{ photo_agent }}" alt="Votre interlocutrice">
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
        FORMULAIRE_RESERVATION_HTML, erreur=None, date_min=date_min, valeurs={},
        photo_agent=PHOTO_AGENT_B64,
    )


@app.route("/reserver", methods=["POST"])
def valider_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    valeurs = request.form.to_dict()

    def page_erreur(message: str):
        return render_template_string(
            FORMULAIRE_RESERVATION_HTML, erreur=message, date_min=date_min, valeurs=valeurs,
            photo_agent=PHOTO_AGENT_B64,
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
