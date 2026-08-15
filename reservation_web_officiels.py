"""
reservation_web_officiels.py

Page de reservation ADMIN uniquement pour "Les Taxis Officiels de Nice".
Contrairement a reservation_web.py (Centrale EasyTaxi), ce fichier n'a
PAS de vocation "client final" : c'est un outil interne pour rentrer une
course dans l'agenda plus vite qu'a la main, depuis un telephone ou un
ordinateur, protege par un code d'acces (admin ou secretaire).

CE QUE CE FICHIER NE FAIT PAS (volontairement) :
  - Pas d'envoi d'email de confirmation (pas de Resend)
  - Pas d'envoi de SMS de confirmation (pas de SMS Gateway)
  - Pas d'acces public sans code : sans ?admin=CODE valide, le formulaire
    refuse l'affichage/la soumission.

Design : identite visuelle "affiche niçoise vintage x application moderne"
(fond ivoire, rouge principal, aigle/ecusson en en-tete). Uniquement de
l'habillage visuel -- aucune logique metier n'est modifiee par rapport a
la version precedente de ce fichier.

Deploiement recommande : un service Railway INDEPENDANT (nouveau projet
ou nouveau service dans un projet existant), avec son PROPRE calendrier
Google et ses PROPRES variables d'environnement (aucune ne doit etre
partagee avec le bot SMS ni avec reservation_web.py) :
  GOOGLE_SERVICE_ACCOUNT_JSON   (compte de service Google, acces au calendrier)
  GOOGLE_CALENDAR_ID            (ID du calendrier "Les Taxis Officiels de Nice")
  GOOGLE_MAPS_API_KEY           (optionnel, seulement si calcul auto de l'heure de PEC)
  ADMIN_ACCESS_CODE             (code personnel, a definir sur Railway)
  SECRETAIRE_ACCESS_CODE        (code partage secretaires, a definir sur Railway)
  MAX_RESERVATIONS_ACTIVES      (optionnel, defaut 5)

Commande de lancement Railway pour ce service (Start Command) :
  python reservation_web_officiels.py
"""

import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, render_template_string, send_from_directory
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reservation_web_officiels")

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
FUSEAU_HORAIRE = ZoneInfo("Europe/Paris")

MAX_RESERVATIONS_ACTIVES = int(os.environ.get("MAX_RESERVATIONS_ACTIVES", "5"))

# Codes d'acces obligatoires. Sans variable definie sur Railway, la valeur
# est vide -> AUCUN acces n'est possible (comportement volontaire, pas de
# valeur par defaut devinable en dur ici).
ADMIN_ACCESS_CODE = os.environ.get("ADMIN_ACCESS_CODE", "")
SECRETAIRE_ACCESS_CODE = os.environ.get("SECRETAIRE_ACCESS_CODE", "")


def determiner_role(code_saisi: str | None) -> str | None:
    """Renvoie 'admin', 'secretaire' ou None selon le code fourni dans
    l'URL (?admin=...) ou le champ cache du formulaire. Si les variables
    d'env ne sont pas configurees, aucun code ne peut jamais matcher."""
    if not code_saisi:
        return None
    if ADMIN_ACCESS_CODE and code_saisi == ADMIN_ACCESS_CODE:
        return "admin"
    if SECRETAIRE_ACCESS_CODE and code_saisi == SECRETAIRE_ACCESS_CODE:
        return "secretaire"
    return None


# ---------------------------------------------------------------------------
# Aide a l'adressage (mêmes tables que reservation_web.py, dupliquees ici
# pour que ce fichier reste totalement independant)
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


def formater_prix(valeur: float) -> str:
    """Formate un montant sans decimales inutiles (35.0 -> '35', 35.5 -> '35.5')."""
    if valeur == int(valeur):
        return str(int(valeur))
    return f"{valeur:.2f}".rstrip("0").rstrip(".")


def extraire_reference_de_description(description: str) -> str:
    """Recupere le code de reference ecrit dans la description d'un
    evenement Google Agenda (ligne 'REF : XXXXXX')."""
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
# Google Agenda ("Les Taxis Officiels de Nice" -- calendrier dedie,
# distinct de celui de la Centrale des Taxis Niçois)
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
    """Cree l'evenement dans le calendrier "Les Taxis Officiels de Nice"."""
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

    prix_min = donnees.get("prix_min")
    prix_max = donnees.get("prix_max")
    prix_annonce = (
        f"{formater_prix(prix_min)}€-{formater_prix(prix_max)}€"
        if prix_min is not None and prix_max is not None
        else None
    )

    titre = (
        f"PC {heure_aff} M. {nom_pour_agenda} | "
        f"PC : {donnees['prise_en_charge']} | "
        f"DEST : {donnees['destination']} | "
        + (f"PRIX : {prix_annonce} | " if prix_annonce else "")
        + f"RDV : {heure_rdv_aff} {type_tag} | "
        f"TEL : {telephone} | REF : {reference}"
        + (f" [{donnees['nom_infirmiere']}]" if donnees.get("nom_infirmiere") else "")
        + (" [ACCOMPAGNANT]" if donnees.get("accompagnant") else "")
        + (" [BT AU RETOUR]" if donnees.get("bto_retour") else "")
    ).upper()
    role = donnees.get("role")
    source_label = "reservation prise par Tony (admin)" if role == "admin" else "reservation prise par une/un secretaire"

    description = (
        f"REF : {reference}\n"
        f"PC : {donnees['prise_en_charge']}\n"
        f"DEST : {donnees['destination']}\n"
        f"RDV : {heure_rdv_aff} {type_tag}\n"
        f"TEL : {telephone}\n"
        f"SOURCE : {source_label}"
        + (f"\nPRIX ANNONCE : {prix_annonce}" if prix_annonce else "")
        + (f"\nINFIRMIERE : {donnees['nom_infirmiere']}" if donnees.get("nom_infirmiere") else "")
        + ("\nACCOMPAGNANT : OUI" if donnees.get("accompagnant") else "")
        + ("\nBT : AU RETOUR UNIQUEMENT" if donnees.get("bto_retour") else "")
    ).upper()

    try:
        service = _construire_service_agenda()
        evenement = {
            "summary": titre,
            "description": description,
            "start": {"dateTime": debut_dt.isoformat(), "timeZone": "Europe/Paris"},
            "end": {"dateTime": fin_dt.isoformat(), "timeZone": "Europe/Paris"},
            # colorId different de celui de la Centrale des Taxis Niçois
            # (9, bleu myrtille) pour distinguer d'un coup d'oeil les
            # reservations de ce calendrier si jamais il est un jour
            # consulte cote a cote avec l'autre.
            "colorId": "9",
        }
        resultat = (
            service.events()
            .insert(calendarId=GOOGLE_CALENDAR_ID, body=evenement)
            .execute()
        )
        return True, resultat.get("htmlLink", "evenement cree"), resultat.get("id")
    except Exception as e:
        return False, str(e), None


# ---------------------------------------------------------------------------
# Pages web
# ---------------------------------------------------------------------------


FORMULAIRE_RESERVATION_HTML = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reserver un taxi - Les Taxis Officiels de Nice</title>
<link rel="manifest" href="/manifest.json?admin={{ admin_code }}">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<meta name="theme-color" content="#B32620">
<meta name="apple-mobile-web-app-title" content="Taxis Nice">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: light;
    --ivoire: #F3E6C8;
    --rouge: #B32620;
    --rouge-fonce: #8F1D18;
    --noir: #171717;
    --dore: #B89A5A;
    --champ-fond: #FFFDF8;
    --texte-secondaire: #686159;
    --bordure-chaude: #DCCBA6;
    --rouge-fond-leger: rgba(179, 38, 32, 0.07);
    --rouge-ombre: rgba(179, 38, 32, 0.28);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { max-width: 100%; overflow-x: hidden; }
  body {
    margin: 0; padding: 22px 16px 48px; background: var(--ivoire);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: var(--noir);
  }
  .page { max-width: 480px; margin: 0 auto; width: 100%; }
  .grille-haut { display: contents; }

  @media (min-width: 720px) {
    body { padding: 40px 24px 60px; }
    .page { max-width: 900px; }
    .grille-haut {
      display: grid; grid-template-columns: 340px 1fr; gap: 16px; align-items: start;
    }
    .grille-haut .carte { margin-bottom: 0; min-width: 0; }
    .raccourcis { flex-wrap: wrap; overflow-x: visible; }
  }

  /* ---------- En-tete ---------- */
  .entete { text-align: center; padding-top: 0; margin-bottom: 18px; }
  .entete .logo-aigle {
    width: 76px; height: 76px; object-fit: contain; margin: 0 auto 2px; display: block;
  }
  .entete h1 {
    position: relative; display: inline-block;
    font-family: 'Oswald', sans-serif; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.5px; margin: 0 0 14px;
    font-size: clamp(21px, 6.2vw, 28px); line-height: 1.15;
    color: var(--rouge);
    text-shadow: 0 0 1px rgba(179,38,32,0.5), 0 0 4px rgba(179,38,32,0.3), 0 0 9px rgba(179,38,32,0.18);
  }
  .entete h1::before {
    content: attr(data-text);
    position: absolute; inset: 0;
    text-transform: uppercase; letter-spacing: inherit;
    background: linear-gradient(
      100deg,
      transparent 0%, transparent 46%, #FFFDF8 50%, transparent 54%, transparent 100%
    );
    background-size: 320% 100%;
    background-position: 260% 0;
    -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; color: transparent;
    animation: neon-eclat 6.5s linear infinite;
    pointer-events: none;
  }
  @keyframes neon-eclat {
    0% { background-position: 260% 0; }
    100% { background-position: -140% 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .entete h1::before { animation: none; display: none; }
  }
  .badge-role {
    display: inline-flex; align-items: center; gap: 6px;
    border: 1.5px solid var(--rouge); color: var(--rouge); background: transparent;
    padding: 6px 16px; border-radius: 20px; font-size: 12px; font-weight: 700;
    letter-spacing: 0.6px; text-transform: uppercase;
  }

  /* ---------- Cartes ---------- */
  .carte {
    background: var(--champ-fond); border: 1.5px solid rgba(179, 38, 32, 0.35);
    border-radius: 18px; padding: 20px 18px 16px;
    box-shadow: 0 4px 16px rgba(23, 17, 17, 0.07); margin-bottom: 14px;
  }
  .section-titre { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .numero {
    width: 27px; height: 27px; border-radius: 50%; background: var(--rouge);
    color: #fff; font-family: 'Oswald', sans-serif; font-weight: 700; font-size: 14px;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .section-titre h2 {
    margin: 0; font-family: 'Oswald', sans-serif; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.4px; font-size: 16.5px; color: var(--rouge);
  }
  .badge-medical {
    display: inline-flex; align-items: center; gap: 5px; background: var(--rouge-fond-leger);
    color: var(--rouge); padding: 5px 12px; border-radius: 20px; font-size: 12.5px; font-weight: 700;
  }
  .infirmiere-affichage {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    margin-top: 16px; font-size: 13px; color: var(--noir); flex-wrap: nowrap;
  }
  .infirmiere-affichage span {
    display: inline-flex; align-items: center; gap: 6px; color: var(--texte-secondaire);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
  }
  .infirmiere-affichage span svg { flex-shrink: 0; color: var(--dore); }
  .infirmiere-affichage strong { color: var(--noir); }
  .infirmiere-affichage button {
    display: inline-flex; align-items: center; gap: 5px; background: none; border: none;
    color: var(--rouge); font-weight: 600; font-size: 13px; cursor: pointer; padding: 4px 6px;
    flex-shrink: 0; font-family: 'Inter', sans-serif;
  }

  /* ---------- Champs ---------- */
  label {
    display: block; font-weight: 600; margin: 14px 0 6px; font-size: 14px; color: var(--noir);
  }
  label:first-of-type { margin-top: 0; }

  .champ-icone { position: relative; }
  .champ-icone svg {
    position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
    color: var(--noir); pointer-events: none;
  }
  .champ-icone input { padding-left: 44px !important; }

  input[type=text], input[type=tel], input[type=date], input[type=time], input[type=number] {
    width: 100%; min-height: 56px; padding: 14px; font-size: 16px;
    border: 1.5px solid var(--bordure-chaude); border-radius: 13px; background: var(--champ-fond);
    font-family: 'Inter', sans-serif; color: var(--noir); -webkit-appearance: none;
  }
  input:focus {
    outline: none; border-color: var(--rouge);
    box-shadow: 0 0 0 3px var(--rouge-fond-leger);
  }
  input::placeholder { font-size: 14.5px; color: #A79F92; }

  .ligne-double { display: flex; gap: 10px; flex-wrap: wrap; }
  .ligne-double > div { flex: 1 1 130px; min-width: 130px; }

  /* ---------- Selecteur type de course ---------- */
  .choix { display: flex; gap: 10px; margin: 6px 0 4px; }
  .choix label {
    flex: 1; margin: 0; display: flex; align-items: center; justify-content: center; gap: 8px;
    text-align: center; padding: 14px 8px; min-height: 56px; border: 1.5px solid var(--bordure-chaude);
    border-radius: 13px; font-weight: 600; cursor: pointer; font-size: 14px; color: var(--noir);
    background: #ffffff; transition: border-color .15s ease, background-color .15s ease, color .15s ease;
  }
  .choix input { display: none; }
  .choix label svg { flex-shrink: 0; }
  label:has(#type_prive:checked) { border-color: var(--rouge); background: var(--rouge-fond-leger); color: var(--rouge); }
  label:has(#type_medical:checked) { border-color: var(--rouge); background: var(--rouge-fond-leger); color: var(--rouge); }

  .adresses { position: relative; }
  .bouton-inverser {
    position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
    width: 32px; height: 32px; border-radius: 50%; background: var(--champ-fond);
    border: 1.5px solid var(--rouge); display: flex; align-items: center;
    justify-content: center; cursor: pointer; z-index: 2; color: var(--rouge);
  }

  input[type=checkbox] { accent-color: var(--rouge); }

  /* ---------- Interrupteurs (accompagnant / bon de transport) ---------- */
  .toggle-ligne {
    display: flex; align-items: center; gap: 10px; margin: 12px 0 0;
    font-weight: 400; font-size: 14px; color: var(--noir); cursor: pointer;
  }
  .toggle-switch { position: relative; width: 44px; height: 26px; flex-shrink: 0; }
  .toggle-switch input {
    position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer;
  }
  .toggle-piste {
    position: absolute; inset: 0; background: #E4D4B0; border-radius: 999px;
    transition: background-color .18s ease;
  }
  .toggle-piste::before {
    content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px;
    background: #fff; border-radius: 50%; box-shadow: 0 1px 3px rgba(23,17,17,0.25);
    transition: transform .18s ease;
  }
  .toggle-switch input:checked + .toggle-piste { background: var(--rouge); }
  .toggle-switch input:checked + .toggle-piste::before { transform: translateX(18px); }
  .toggle-switch input:focus-visible + .toggle-piste { box-shadow: 0 0 0 3px var(--rouge-fond-leger); }
  .toggle-texte { color: var(--noir); }

  /* ---------- Bouton principal ---------- */
  button.envoyer {
    width: 100%; margin-top: 6px; padding: 18px; min-height: 60px; font-size: 16.5px;
    font-family: 'Oswald', sans-serif; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase;
    background: var(--rouge); color: #fff; border: none; border-radius: 14px; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    box-shadow: 0 8px 20px var(--rouge-ombre);
    transition: background-color .15s ease, transform .1s ease;
  }
  button.envoyer:active { background: var(--rouge-fonce); transform: scale(0.98); }
  button.envoyer:disabled { opacity: 0.7; }

  .pied { text-align: center; font-size: 12.5px; color: var(--texte-secondaire); margin-top: 16px; }

  .erreur {
    background: #FBEAE9; color: var(--rouge-fonce); border: 1px solid var(--rouge);
    padding: 12px 14px; border-radius: 12px; font-size: 14px; margin-bottom: 14px;
  }
  .raccourcis {
    display: flex; gap: 8px; overflow-x: auto; margin: 8px 0 4px; padding-bottom: 2px;
    -webkit-overflow-scrolling: touch;
  }
  .raccourcis::-webkit-scrollbar { height: 4px; }
  .raccourcis button {
    flex-shrink: 0; padding: 9px 14px; border-radius: 20px; border: 1.5px solid var(--bordure-chaude);
    background: #ffffff; font-size: 13px; font-weight: 600; color: var(--noir); cursor: pointer;
    white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; font-family: 'Inter', sans-serif;
  }
  .raccourcis button:active { background: var(--rouge-fond-leger); border-color: var(--rouge); color: var(--rouge); }
</style>
</head>
<body>
<div class="page">

  <div class="entete">
    <img class="logo-aigle" src="/logo.png" alt="Les Taxis Officiels de Nice">
    <h1 data-text="Les Taxis Officiels de Nice">Les Taxis Officiels de Nice</h1>
    <div class="badge-role">
      {% if role == 'secretaire' %}Mode secretaire{% else %}Mode admin{% endif %}
    </div>
  </div>

  {% if erreur %}<div class="erreur">{{ erreur }}</div>{% endif %}

  <form method="POST" action="/reserver">
    <input type="hidden" name="admin_code" value="{{ admin_code }}">

    <div class="grille-haut">
    <div class="carte">
      <div class="section-titre">
        <div class="numero">1</div>
        <h2>{% if role == 'secretaire' %}Patient{% else %}Client{% endif %}</h2>
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
          <label for="prenom">Prenom</label>
          <div class="champ-icone">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>
            <input type="text" id="prenom" name="prenom" value="{{ valeurs.get('prenom', '') }}">
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

      <label for="telephone">Telephone de contact</label>
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
        <h2>Trajet</h2>
        {% if role == 'secretaire' %}
        <span class="badge-medical">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>
          Transport medical
        </span>
        {% endif %}
      </div>

      {% if role == 'secretaire' %}
        <input type="hidden" name="type_course" value="medical">
      {% else %}
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
        <label class="toggle-ligne">
          <span class="toggle-switch">
            <input type="checkbox" id="accompagnant" name="accompagnant" value="oui"
                   {% if valeurs.get('accompagnant') %}checked{% endif %}>
            <span class="toggle-piste"></span>
          </span>
          <span class="toggle-texte">Accompagnant avec le patient</span>
        </label>
        <label class="toggle-ligne">
          <span class="toggle-switch">
            <input type="checkbox" id="bto_retour" name="bto_retour" value="oui"
                   {% if valeurs.get('bto_retour') %}checked{% endif %}>
            <span class="toggle-piste"></span>
          </span>
          <span class="toggle-texte">Bon de transport remis au retour</span>
        </label>
      </div>

      <div id="options_prive" class="options-prive" style="display: {% if valeurs.get('type_course', 'prive') == 'prive' %}block{% else %}none{% endif %};">
        <label style="margin-top: 10px;">Prix annoncé au client</label>
        <div class="ligne-double">
          <div>
            <input type="number" id="prix_min" name="prix_min" placeholder="Mini (€)" min="0" step="1"
                   value="{{ valeurs.get('prix_min', '') }}">
          </div>
          <div>
            <input type="number" id="prix_max" name="prix_max" placeholder="Maxi (€)" min="0" step="1"
                   value="{{ valeurs.get('prix_max', '') }}">
          </div>
        </div>
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

        <label for="destination">Destination</label>
        <div class="champ-icone">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>
          <input type="text" id="destination" name="destination"
                 placeholder="Ex : Aeroport de Nice"
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
        <input type="time" id="heure_pc" name="heure_pc" value="{{ valeurs.get('heure_pc', '') }}" required>
      </div>
    </div>

    <button type="submit" class="envoyer">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/><path d="m9 16 2 2 4-4"/></svg>
      Ajouter a l'agenda
    </button>
  </form>
</div>

<script>
  document.getElementById('bouton_inverser').addEventListener('click', function () {
    const pc = document.getElementById('prise_en_charge');
    const dest = document.getElementById('destination');
    const temp = pc.value;
    pc.value = dest.value;
    dest.value = temp;
  });

  const radioPrive = document.getElementById('type_prive');
  const radioMedical = document.getElementById('type_medical');
  const optionsMedical = document.getElementById('options_medical');
  const optionsPrive = document.getElementById('options_prive');
  if (radioPrive && radioMedical) {
    function majOptionsMedical() {
      if (optionsMedical) { optionsMedical.style.display = radioMedical.checked ? 'block' : 'none'; }
      if (optionsPrive) { optionsPrive.style.display = radioPrive.checked ? 'block' : 'none'; }
    }
    radioPrive.addEventListener('change', majOptionsMedical);
    radioMedical.addEventListener('change', majOptionsMedical);
  }

  const formulaire = document.querySelector('form');
  const boutonEnvoi = document.querySelector('button.envoyer');
  formulaire.addEventListener('submit', function () {
    boutonEnvoi.disabled = true;
    boutonEnvoi.textContent = 'Envoi en cours...';
  });

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
<title>Reservation ajoutee</title>
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --ivoire: #F3E6C8; --rouge: #B32620; --rouge-fonce: #8F1D18; --noir: #171717;
    --dore: #B89A5A; --champ-fond: #FFFDF8; --texte-secondaire: #686159;
  }
  * { box-sizing: border-box; }
  html, body { max-width: 100%; overflow-x: hidden; }
  body {
    margin: 0; padding: 24px 16px; background: var(--ivoire);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: var(--noir);
  }
  .carte {
    max-width: 480px; margin: 40px auto 0; background: var(--champ-fond); border-radius: 18px;
    border: 1.5px solid rgba(179, 38, 32, 0.35);
    padding: 30px 22px; box-shadow: 0 4px 16px rgba(23,17,17,0.08); text-align: center;
  }
  .coche {
    width: 58px; height: 58px; border-radius: 50%; background: rgba(179, 38, 32, 0.09); color: var(--rouge);
    display: flex; align-items: center; justify-content: center; font-size: 30px;
    margin: 0 auto 16px; border: 1.5px solid var(--rouge);
  }
  h1 {
    font-family: 'Oswald', sans-serif; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.4px; color: var(--rouge); font-size: 19px; margin: 0 0 12px;
  }
  table { width: 100%; text-align: left; margin-top: 18px; font-size: 15px; }
  td { padding: 7px 0; border-bottom: 1px solid var(--ivoire); }
  td.libelle { color: var(--texte-secondaire); width: 40%; }
  .ref {
    display: inline-block; margin-top: 18px; padding: 10px 18px; background: rgba(184, 154, 90, 0.14);
    border: 1px solid var(--dore); border-radius: 10px; font-weight: 700; letter-spacing: 1px;
    color: var(--noir); font-family: 'Oswald', sans-serif;
  }
  a.retour {
    display: inline-block; margin-top: 24px; color: var(--rouge); font-size: 14px; font-weight: 600;
    text-decoration: none;
  }
</style>
</head>
<body>
  <div class="carte">
    <div class="coche">&#10003;</div>
    <h1>Course ajoutee a l'agenda</h1>
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
    <p style="font-size:13px;color:var(--texte-secondaire);margin-top:14px;font-weight:600;">
      Aucune notification envoyee au client (pas de SMS/email sur cet outil).
    </p>
    <a class="retour" href="/reserver?admin={{ admin_code }}">Ajouter une autre course</a>
  </div>
</body>
</html>
"""

ACCES_REFUSE_HTML = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Acces refuse</title>
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root { --ivoire: #F3E6C8; --rouge: #B32620; --rouge-fonce: #8F1D18; --texte-secondaire: #686159; }
  body {
    margin: 0; padding: 40px 16px; background: var(--ivoire);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    color: #171717; text-align: center;
  }
  .carte {
    max-width: 420px; margin: 0 auto; background: #FFFDF8; border-radius: 18px;
    border: 1.5px solid rgba(179, 38, 32, 0.35);
    padding: 28px 22px; box-shadow: 0 4px 16px rgba(23,17,17,0.08);
  }
  h1 { font-family: 'Oswald', sans-serif; font-weight: 700; text-transform: uppercase; font-size: 18px; color: var(--rouge-fonce); }
  p { color: var(--texte-secondaire); font-size: 14px; }
</style>
</head>
<body>
  <div class="carte">
    <h1>Acces refuse</h1>
    <p>Cette page necessite un code d'acces valide (?admin=CODE dans le lien).
    Cet outil est reserve a un usage interne.</p>
  </div>
</body>
</html>
"""


@app.route("/logo.png", methods=["GET"])
def logo_aigle():
    """Sert le logo depuis un fichier image separe (et non plus en base64
    dans ce fichier .py) -- evite les problemes de troncature rencontres
    lors de l'upload mobile d'un fichier texte contenant une ligne enorme."""
    return send_from_directory(app.root_path, "logo_aigle.png", mimetype="image/png")


@app.route("/icon-<int:taille>.png", methods=["GET"])
def icone_ecran_accueil(taille):
    """Sert les icones carrees (fond ivoire + aigle centre) utilisees pour
    l'icone d'ecran d'accueil (favicon, iOS, Android/manifest). Les
    fichiers icon-96.png / icon-180.png / icon-192.png / icon-512.png
    doivent etre presents a la racine du depot, a cote de ce script."""
    if taille not in (96, 180, 192, 512):
        return "Not found", 404
    return send_from_directory(app.root_path, f"icon-{taille}.png", mimetype="image/png")


@app.route("/manifest.json", methods=["GET"])
def manifest_web_app():
    """Manifest PWA : permet a Android (Chrome) d'installer un icone net en
    plein ecran. Le code d'acces admin/secretaire est embarque dans
    start_url (recupere depuis le lien du manifest lui-meme, cf. balise
    <link rel="manifest"> dans le HTML) pour que l'icone ajoutee a l'ecran
    d'accueil rouvre directement le formulaire deja deverrouille."""
    code = request.args.get("admin", "")
    start_url = f"/reserver?admin={code}" if code else "/reserver"
    manifest = {
        "name": "Les Taxis Officiels de Nice",
        "short_name": "Taxis Nice",
        "start_url": start_url,
        "scope": "/",
        "display": "browser",
        "background_color": "#F3E6C8",
        "theme_color": "#B32620",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return json.dumps(manifest), 200, {"Content-Type": "application/manifest+json"}


@app.route("/", methods=["GET"])
def racine():
    return (
        "Les Taxis Officiels de Nice -- outil de saisie agenda operationnel"
    ), 200


@app.route("/reserver", methods=["GET"])
def page_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    code_saisi = request.args.get("admin")
    role = determiner_role(code_saisi)
    if role is None:
        return render_template_string(ACCES_REFUSE_HTML), 403
    return render_template_string(
        FORMULAIRE_RESERVATION_HTML, erreur=None, date_min=date_min, valeurs={},
        role=role, admin_code=code_saisi,
    )


@app.route("/reserver", methods=["POST"])
def valider_reservation():
    date_min = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    valeurs = request.form.to_dict()
    code_saisi = request.form.get("admin_code")
    role = determiner_role(code_saisi)
    if role is None:
        return render_template_string(ACCES_REFUSE_HTML), 403

    def page_erreur(message: str):
        return render_template_string(
            FORMULAIRE_RESERVATION_HTML, erreur=message, date_min=date_min, valeurs=valeurs,
            role=role, admin_code=code_saisi,
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
    prix_min_saisi = (request.form.get("prix_min") or "").strip()
    prix_max_saisi = (request.form.get("prix_max") or "").strip()

    if role == "secretaire":
        if not all([patient_nom_complet, telephone_saisi, prise_en_charge, destination, date_str]):
            return page_erreur("Merci de remplir tous les champs du formulaire.")
        if not nom_infirmiere:
            return page_erreur("Merci d'indiquer le nom de la secretaire ou de l'infirmiere.")
    elif not all([nom, telephone_saisi, prise_en_charge, destination, date_str]):
        return page_erreur("Merci de remplir tous les champs du formulaire.")

    prix_min = None
    prix_max = None
    if type_course != "medical" and (prix_min_saisi or prix_max_saisi):
        if not (prix_min_saisi and prix_max_saisi):
            return page_erreur("Merci d'indiquer le prix mini ET le prix maxi, ou de laisser les deux vides.")
        try:
            prix_min = float(prix_min_saisi.replace(",", "."))
            prix_max = float(prix_max_saisi.replace(",", "."))
        except ValueError:
            return page_erreur("Le prix annonce saisi n'est pas valide.")
        if prix_min > prix_max:
            return page_erreur("Le prix mini ne peut pas etre superieur au prix maxi.")

    if role == "secretaire":
        nom_complet = patient_nom_complet
        nom_pour_agenda = patient_nom_complet
    else:
        nom_complet = f"{prenom} {nom}".strip()
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
        "role": role,
        "nom_infirmiere": nom_infirmiere or None,
        "accompagnant": accompagnant,
        "bto_retour": bto_retour,
        "telephone": telephone,
        "prise_en_charge": prise_en_charge,
        "destination": destination,
        "prix_min": prix_min,
        "prix_max": prix_max,
        "heure": None,
        "heure_rdv": None,
        "heure_iso": None,
    }

    if heure_rdv_saisie:
        heure_minute_rdv = parser_heure_texte(heure_rdv_saisie.replace(":", "h"))
        if not heure_minute_rdv:
            return page_erreur("L'heure de rendez-vous saisie n'est pas valide.")
        rdv_h, rdv_m = heure_minute_rdv
        donnees["heure_rdv"] = f"{rdv_h:02d}h{rdv_m:02d}"

    if heure_inconnue:
        if not heure_rdv_saisie:
            return page_erreur(
                "Merci d'indiquer l'heure de rendez-vous pour calculer "
                "automatiquement l'heure de prise en charge."
            )
        duree_trajet = estimer_duree_trajet(
            completer_adresse_avec_ville(resoudre_adresse_medicale(prise_en_charge)),
            completer_adresse_avec_ville(resoudre_adresse_medicale(destination)),
        )
        if duree_trajet is None:
            return page_erreur(
                "Impossible d'estimer automatiquement l'heure de prise en charge "
                "pour ce trajet. Merci de renseigner directement l'heure de prise "
                "en charge."
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
            f"Il y a deja {MAX_RESERVATIONS_ACTIVES} reservations en cours pour ce "
            "numero. Annulez-en une avant d'en ajouter une nouvelle."
        )

    # Protection anti-doublon : meme logique que reservation_web.py, pour
    # eviter de creer deux fois le meme evenement en cas de double-clic.
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
                admin_code=code_saisi,
            )

    reference = generer_reference()
    succes, detail, event_id = creer_evenement_agenda(donnees, reference)
    if not succes:
        log.error("Echec creation reservation (Officiels de Nice) : %s", detail)
        return page_erreur(
            "Une erreur technique empeche l'ajout de cette course a l'agenda. "
            "Reessayez ou ajoutez-la manuellement."
        )

    log.info(
        "Reservation %s creee (Officiels de Nice) : %s (ref %s, tel %s, event %s)",
        role.upper(), nom_complet, reference, telephone, event_id,
    )

    return render_template_string(
        CONFIRMATION_RESERVATION_HTML, donnees=donnees, reference=reference, admin_code=code_saisi,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
