"""
sms_webhook.py

Etape 2 : reception d'un SMS + extraction IA (Claude Haiku) des infos de
reservation (type prive/medical, nom, telephone, prise en charge,
destination, heure) + reponse automatique adaptee.
Pas encore de Google Agenda - ca sera la prochaine etape.

Fonctionne avec l'app "SMS Gateway for Android" (docs.sms-gate.app).

Variables d'environnement necessaires (a mettre sur Railway) :
  SMS_GATEWAY_SIGNING_KEY   -> cle de signature webhook (app > Parametres > Webhooks)
  SMS_GATEWAY_USERNAME      -> identifiant Cloud/Local de l'app
  SMS_GATEWAY_PASSWORD      -> mot de passe Cloud/Local de l'app
  SMS_GATEWAY_MODE          -> "cloud" ou "local" (defaut: cloud)
  SMS_GATEWAY_LOCAL_URL     -> uniquement si mode local (ex: https://192.168.1.10:8080)
  ANTHROPIC_API_KEY         -> cle API Anthropic (console.anthropic.com), pour l'extraction IA
"""

import hashlib
import hmac
import json
import logging
import os
import time

import requests
from flask import Flask, request, jsonify

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

PROMPT_SYSTEME = """Tu extrais les informations d'une reservation de taxi a partir d'un SMS en francais.

Champs a extraire :
- type : "prive" ou "medical". Medical si mention d'un hopital, clinique, CHU,
  service medical (ex: "neurologie", "dialyse"), medecin, etc. Sinon prive.
- nom : nom du client mentionne dans le message (ou null si absent)
- telephone : numero de contact mentionne dans le message (ou null si absent,
  on utilisera alors le numero qui a envoye le SMS)
- prise_en_charge : adresse ou lieu de depart (ou null si absent)
- destination : adresse ou lieu d'arrivee (ou null si absent)
- heure : heure precise de prise en charge (ex: "demain 8h30", "20h",
  "aujourd'hui 14h"). IMPORTANT : une date seule sans heure precise
  (ex: juste "demain" ou juste "aujourd'hui") NE COMPTE PAS comme une heure
  valide -> mets null dans ce cas, il faut une heure chiffree.

Reponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou apres,
et SANS balises markdown (pas de ```json, pas de backticks du tout).
Ta reponse doit commencer directement par { et finir par }, au format exact :
{"type": "...", "nom": ..., "telephone": ..., "prise_en_charge": ..., "destination": ..., "heure": ...}
"""

CHAMPS_OBLIGATOIRES = {
    "nom": "votre nom",
    "prise_en_charge": "l'adresse de prise en charge",
    "destination": "la destination",
    "heure": "l'heure de prise en charge",
}

# Champs "factuels" utilises pour savoir si un SMS apporte une nouvelle info
# ou non (le champ "type" est exclu car l'IA lui donne toujours une valeur,
# meme quand le message ne parle pas du tout d'une reservation).
CHAMPS_FACTUELS = ["nom", "telephone", "prise_en_charge", "destination", "heure"]

# Memoire des reservations par numero de telephone, qu'elles soient
# completes ou non. Permet de repondre correctement a un message de suivi
# ("a quelle heure ?") sans tout redemander, et de fusionner les infos
# entre plusieurs SMS d'une meme demande.
# Format : { "+336...": {"donnees": {...}, "derniere_maj": ts, "complete": bool} }
# Simple dictionnaire en RAM : suffisant pour ce volume, se vide si le
# service redemarre (acceptable, cas rare).
MEMOIRE_RESERVATIONS: dict[str, dict] = {}
DUREE_EXPIRATION_SECONDES = 60 * 60  # 1 heure


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


def fusionner(anciennes: dict, nouvelles: dict) -> dict:
    """Combine les infos deja connues avec les nouvelles infos extraites du
    dernier SMS. Les nouvelles valeurs (non nulles) remplacent les
    anciennes ; les champs absents du nouveau message gardent leur ancienne
    valeur."""
    fusion = dict(anciennes)
    for champ, valeur in nouvelles.items():
        if valeur:
            fusion[champ] = valeur
    return fusion


def possede_info_reservation(donnees: dict) -> bool:
    """Indique si l'extraction IA a trouve au moins une info de reservation
    (nom, telephone, adresse, destination ou heure) dans le message, par
    opposition a un message de suivi (question, remerciement, etc.) qui ne
    contient aucune de ces infos."""
    return any(donnees.get(champ) for champ in CHAMPS_FACTUELS)


def sauvegarder_entree(numero: str, donnees: dict, complete: bool) -> None:
    MEMOIRE_RESERVATIONS[numero] = {
        "donnees": donnees,
        "derniere_maj": time.time(),
        "complete": complete,
    }


def extraire_reservation(message: str) -> dict | None:
    """Appelle Claude Haiku pour extraire les infos de reservation du SMS."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY non configuree - extraction IA impossible")
        return None
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
                "messages": [{"role": "user", "content": message}],
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


def construire_reponse(donnees: dict) -> str:
    """Construit le SMS de reponse selon que la reservation est complete ou non."""
    manquants = [
        libelle
        for champ, libelle in CHAMPS_OBLIGATOIRES.items()
        if not donnees.get(champ)
    ]

    if manquants:
        return "Merci de preciser : " + ", ".join(manquants) + "."

    type_course = "medical" if donnees.get("type") == "medical" else "prive"
    nom = donnees["nom"]
    heure = donnees["heure"]
    depart = donnees["prise_en_charge"]
    destination = donnees["destination"]

    if type_course == "medical":
        return (
            f"C'est note pour {nom} : {heure}, depart {depart}, "
            f"direction {destination}. Un chauffeur vous contactera."
        )
    return (
        f"C'est note pour {nom} : {heure}, depart {depart}, "
        f"direction {destination}. Un chauffeur vous contactera."
    )


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
        donnees_extraites = extraire_reservation(message)
        if donnees_extraites is None:
            # Echec de l'IA (cle manquante, erreur reseau, etc.) -> on previent
            # quand meme le client au lieu de rester silencieux.
            texte_reponse = (
                "Bien recu, votre demande de reservation a ete notee. "
                "Un chauffeur vous recontactera pour confirmer."
            )
        else:
            log.info("Extraction IA (ce message) : %s", donnees_extraites)
            entree_existante = recuperer_entree(expediteur)
            donnees_existantes = entree_existante["donnees"] if entree_existante else {}

            if not possede_info_reservation(donnees_extraites) and entree_existante:
                # Le SMS ne contient aucune nouvelle info de reservation
                # (ex: "a quelle heure ?", "merci", "c'est confirme ?") et on
                # a deja une reservation en memoire pour ce numero -> on la
                # rappelle plutot que de redemander toutes les infos.
                log.info("Message de suivi detecte pour %s, rappel de la reservation existante", expediteur)
                texte_reponse = construire_reponse(donnees_existantes)
                # On prolonge la duree de vie de la memoire puisque le client
                # est toujours en train d'echanger sur cette reservation.
                sauvegarder_entree(expediteur, donnees_existantes, entree_existante["complete"])
            else:
                donnees_completes = fusionner(donnees_existantes, donnees_extraites)
                log.info("Donnees cumulees pour %s : %s", expediteur, donnees_completes)

                texte_reponse = construire_reponse(donnees_completes)

                champs_manquants = [
                    c for c in CHAMPS_OBLIGATOIRES if not donnees_completes.get(c)
                ]
                sauvegarder_entree(expediteur, donnees_completes, complete=not champs_manquants)

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
