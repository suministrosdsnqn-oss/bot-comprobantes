import os, json, logging, urllib.request
from flask import Flask, request, jsonify
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TC_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
AUGUSTO_PHONE = "5491151657337"

def es_cotizacion(mensaje):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": "Este mensaje contiene cotizaciones de divisas, precios de dolar, euro, reales, usdt o similares? Responde solo SI o NO.\n\nMensaje: " + mensaje}]
    )
    return "SI" in response.content[0].text.strip().upper()

def enviar_telegram(mensaje):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHANNEL_ID, "text": mensaje}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"ok": True})

        from_me = data.get("fromMe", False)
        if from_me:
            return jsonify({"ok": True})

        phone = data.get("phone", "")
        phone_limpio = phone.replace("@c.us", "").replace("@s.whatsapp.net", "").strip()
        if AUGUSTO_PHONE not in phone_limpio:
            return jsonify({"ok": True})

        text_data = data.get("text", {})
        if isinstance(text_data, dict):
            texto = text_data.get("message", "")
        else:
            texto = str(text_data) if text_data else ""

        if not texto or len(texto.strip()) < 3:
            return jsonify({"ok": True})

        logger.info("Mensaje de Augusto: " + texto[:50])

        if es_cotizacion(texto):
            enviar_telegram(texto)
            logger.info("Cotizacion reenviada al canal")
        else:
            logger.info("Mensaje ignorado - no es cotizacion")

        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Error: " + str(e))
        return jsonify({"ok": True})

@app.route("/", methods=["GET"])
def health():
    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
