import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATA_FILE = "comprobantes.json"

def cargar_datos():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def guardar_datos(datos):
    with open(DATA_FILE, "w") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

def obtener_fecha_hoy():
    return datetime.now().strftime("%Y-%m-%d")

async def procesar_comprobante(image_data: bytes) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_base64 = base64.standard_b64encode(image_data).decode("utf-8")
    message = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64,
                        },
                    },
                    {
                        "type": "text",
                        "text": """Analizá este comprobante de pago y extraé la siguiente información.
Respondé ÚNICAMENTE en formato JSON sin ningún texto adicional, así:
{
  "nombre": "nombre de la persona",
  "apellido": "apellido de la persona",
  "monto": 12345.00
}
Si no encontrás algún dato, usá null. El monto debe ser un número sin símbolos ni puntos de miles."""
                    }
                ],
            }
        ],
    )
    texto = message.content[0].text.strip()
    texto = texto.replace("```json", "").replace("```", "").strip()
    return json.loads(texto)

async def handle_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        foto = update.message.photo[-1]
        file = await context.bot.get_file(foto.file_id)
        image_data = await file.download_as_bytearray()
        await update.message.reply_text("📷 Procesando comprobante...")
        datos_comprobante = await procesar_comprobante(bytes(image_data))
