import os, json, logging, base64
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

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

def fecha_hoy():
    return datetime.now().strftime("%Y-%m-%d")

def hora_ahora():
    return datetime.now().strftime("%H:%M")

def extraer_datos(image_data):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    img64 = base64.standard_b64encode(image_data).decode("utf-8")
    msg = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img64}},
                {"type": "text", "text": 'Extraé del comprobante: nombre, apellido y monto. Respondé SOLO JSON sin texto extra: {"nombre": "...", "apellido": "...", "monto": 1234.0}. Si no encontras un dato usa null. Monto solo numeros.'}
            ]
        }]
    )
    texto = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(texto)

async def handle_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        foto = update.message.photo[-1]
        archivo = await context.bot.get_file(foto.file_id)
        imagen = bytes(await archivo.download_as_bytearray())
        await update.message.reply_text("📷 Procesando comprobante...")
        datos_comp = extraer_datos(imagen)
        nombre = (datos_comp.get("nombre") or "")
        apellido = (datos_comp.get("apellido") or "")
        monto = datos_comp.get("monto") or 0
        nombre_completo = f"{nombre} {apellido}".strip() or "Desconocido"
        hoy = fecha_hoy()
        datos = cargar_datos()
        if hoy not in datos:
            datos[hoy] = []
        datos[hoy].append({"nombre": nombre_completo, "monto": monto, "hora": hora_ahora()})
        guardar_datos(datos)
        total = sum(c["monto"] for c in datos[hoy])
        cant = len(datos[hoy])
        texto = f"✅ *{nombre_completo}* — ${monto:,.0f}\n📊 *Total hoy:* ${total:,.0f} ({c
