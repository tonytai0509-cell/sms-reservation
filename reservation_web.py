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
    nom_pour_agenda = donnees.get("nom_agenda") or donnees["nom"]

    titre = (
        f"PC {heure_aff} M. {nom_pour_agenda} | "
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
  .entete {
    display: flex; align-items: center; gap: 14px; margin-bottom: 20px; padding-top: 4px;
  }
  .entete .photo-agent {
    width: 84px; height: 84px; flex-shrink: 0;
    border-radius: 50%; object-fit: cover; box-shadow: 0 2px 8px rgba(0,0,0,0.18);
    border: 2px solid #fff;
  }
  .entete-texte { flex: 1; text-align: center; }
  .entete-texte svg { color: var(--navy); margin-bottom: 6px; }
  .entete h1 {
    font-size: 18px; margin: 0 0 4px; color: var(--navy); font-weight: 800; line-height: 1.2;
  }
  .entete p { margin: 0; color: #667; font-size: 13px; }

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
    <div class="entete-texte">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M5 11l1.5-4.5A2 2 0 0 1 8.4 5h7.2a2 2 0 0 1 1.9 1.5L19 11"/>
        <rect x="3" y="11" width="18" height="6" rx="2"/>
        <circle cx="7.5" cy="17.5" r="1.5"/><circle cx="16.5" cy="17.5" r="1.5"/>
      </svg>
      <h1>Centrale des Taxis Nicois</h1>
      <p>Reservez votre course en quelques instants</p>
    </div>
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
