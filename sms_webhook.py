"""
sms_webhook.py

Etape 1 : reception d'un SMS + reponse automatique fixe.
Pas d'IA, pas de Google Agenda pour l'instant - juste pour valider
que le principe SMS -> webhook -> reponse fonctionne.

Fonctionne avec l'app "SMS Gateway for Android" (docs.sms-gate.app).

Variables d'environnement necessaires (a mettre sur Railway) :
  SMS_GATEWAY_SIGNING_KEY   -> cle de signature webhook (app > Parametres > Webhooks)
  SMS_GATEWAY_USERNAME      -> identifiant Cloud/Local de l'app
  SMS_GATEWAY_PASSWORD      -> mot de passe Cloud/Local de l'app
  SMS_GATEWAY_MODE          -> "cloud" ou "local" (defaut: cloud)
  SMS_GATEWAY_LOCAL_URL     -> uniquement si mode local (ex: https://192.168.1.10:8080)
  REPONSE_FIXE              -> texte de la reponse automatique (optionnel, valeur par defaut ci-dessous)
"""

import hashlib
import hmac
import logging
import os

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

REPONSE_FIXE = os.environ.get(
    "REPONSE_FIXE",
    "Bien recu, votre demande de reservation a ete notee. Un chauffeur vous recontactera pour confirmer.",
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

    if expediteur:
        envoyer_sms(expediteur, REPONSE_FIXE)

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
    Page a visiter une seule fois dans le navigateur : elle demande a
    SMS Gateway (api.sms-gate.app) d'envoyer les futurs SMS recus vers
    notre propre URL /webhook/sms. Remplace le besoin de passer par
    un outil externe comme ReqBin.
    """
    url_service = request.host_url.rstrip("/")
    url_webhook = f"{url_service}/webhook/sms"

    try:
        reponse = requests.post(
            "https://api.sms-gate.app/3rdparty/v1/webhooks",
            json={"url": url_webhook, "event": "sms:received"},
            auth=(GATEWAY_USERNAME, GATEWAY_PASSWORD),
            timeout=15,
        )
    except requests.RequestException as e:
        return f"Erreur de connexion a SMS Gateway : {e}", 500

    if reponse.status_code >= 300:
        return (
            f"Echec (statut {reponse.status_code}) : {reponse.text}<br><br>"
            "Verifie SMS_GATEWAY_USERNAME et SMS_GATEWAY_PASSWORD sur Railway.",
            400,
        )

    return (
        "Webhook enregistre avec succes !<br><br>"
        f"Reponse du serveur : {reponse.text}<br><br>"
        "Tu peux maintenant envoyer un SMS de test au numero du telephone dedie."
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
