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

PROMPT_SYSTEME = """Tu geres une conversation SMS avec un client qui reserve un taxi.
Tu recois : les informations DEJA CONNUES sur sa reservation (peuvent etre
vides), et le NOUVEAU message qu'il vient d'envoyer.

Ta tache : renvoyer la version A JOUR complete des informations, en
combinant intelligemment les infos deja connues avec ce que le nouveau
message apporte.

Champs :
- type : "prive" ou "medical". Medical si mention d'un hopital, clinique, CHU,
  service medical (ex: "neurologie", "dialyse"), medecin, etc. Sinon prive.
- nom : nom de famille du client. Ne provient JAMAIS d'un mot comme "avenue",
  "rue", "boulevard", "chemin", "allee", "impasse" -- ce sont des adresses,
  pas des noms. Un nom apparait typiquement apres "je suis", "M.", "Mme",
  "pour M./Mme", ou en signature. Si le nouveau message ne contient pas de
  nom clairement identifiable, garde le nom deja connu (ne le remplace pas
  par un mot d'adresse).
- telephone : numero de contact different de l'expediteur, si mentionne.
- prise_en_charge : adresse ou lieu de depart complet (numero + nom de rue).
- destination : adresse ou lieu d'arrivee.
- heure : heure precise de prise en charge (ex: "demain 8h30", "20h"). Une
  date seule sans heure chiffree (ex: juste "demain") NE COMPTE PAS comme
  une heure valide.
- est_question : true si le nouveau message est une question ou une
  remarque du client (ex: "a quelle heure venez-vous ?", "c'est confirme ?",
  "merci") qui n'apporte AUCUNE nouvelle info de reservation exploitable
  (au dela de repeter une info deja connue) ; false s'il apporte une info
  nouvelle ou modifiee.

Regles generales :
- Un champ deja connu ne doit JAMAIS etre efface ou remplace par une valeur
  moins precise ou hors-sujet. Il n'est remplace que si le nouveau message
  apporte clairement une correction ou precision sur ce champ precis.
- Les champs jamais renseignes restent a null.

Reponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou apres,
et SANS balises markdown (pas de ```json, pas de backticks du tout).
Ta reponse doit commencer directement par { et finir par }, au format exact :
{"type": "...", "nom": ..., "telephone": ..., "prise_en_charge": ..., "destination": ..., "heure": ..., "est_question": true/false}
"""

CHAMPS_OBLIGATOIRES = {
    "nom": "votre nom",
    "prise_en_charge": "l'adresse de prise en charge",
    "destination": "la destination",
    "heure": "l'heure de prise en charge",
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


def sauvegarder_entree(numero: str, donnees: dict, complete: bool) -> None:
    MEMOIRE_RESERVATIONS[numero] = {
        "donnees": donnees,
        "derniere_maj": time.time(),
        "complete": complete,
    }


def extraire_reservation(message: str, connu: dict | None = None) -> dict | None:
    """Appelle Claude Haiku pour extraire/mettre a jour les infos de
    reservation, en lui donnant le contexte deja connu (infos des messages
    precedents de ce numero) pour qu'elle fasse la fusion intelligemment
    plutot que de repartir de zero a chaque SMS."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY non configuree - extraction IA impossible")
        return None

    contenu_utilisateur = (
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

            if est_question and entree_existante:
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
