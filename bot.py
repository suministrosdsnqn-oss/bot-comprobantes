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

def extraer_datos(image_data):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    img64 = base64.standard_b64encode(image_data).decode("utf-8")
    msg = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img64}},
            {"type": "text", "text": "Extrae nombre, apellido y monto del comprobante. Responde SOLO JSON sin texto extra: {\"nombre\": \"...\", \"apellido\": \"...\", \"monto\": 1234.0}. Si no encuentras un dato usa null. Monto solo numeros."}
        ]}]
    )
    texto = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(texto)

async def handle_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        foto = update.message.photo[-1]
        archivo = await context.bot.get_file(foto.file_id)
        imagen = bytes(await archivo.download_as_bytearray())
        await update.message.reply_text("Procesando comprobante...")
        d = extraer_datos(imagen)
        nombre = ((d.get("nombre") or "") + " " + (d.get("apellido") or "")).strip() or "Desconocido"
        monto = d.get("monto") or 0
        hoy = datetime.now().strftime("%Y-%m-%d")
        datos = cargar_datos()
        if hoy not in datos:
            datos[hoy] = []
        datos[hoy].append({"nombre": nombre, "monto": monto, "hora": datetime.now().strftime("%H:%M")})
        guardar_datos(datos)
        total = sum(c["monto"] for c in datos[hoy])
        cant = len(datos[hoy])
        msg = nombre + " - $" + str(int(monto)) + "\nTotal hoy: $" + str(int(total)) + " (" + str(cant) + " comprobantes)\n\nEscribe /resumen para ver el detalle."
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(str(e))
        await update.message.reply_text("No pude leer el comprobante. Intenta con una imagen mas clara.")

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.now().strftime("%Y-%m-%d")
    datos = cargar_datos()
    if hoy not in datos or not datos[hoy]:
        await update.message.reply_text("No hay comprobantes hoy.")
        return
    lista = datos[hoy]
    total = sum(c["monto"] for c in lista)
    lineas = ["Resumen " + hoy + "\n"]
    for i, c in enumerate(lista, 1):
        lineas.append(str(i) + ". " + c["nombre"] + " - $" + str(int(c["monto"])) + " (" + c["hora"] + ")")
    lineas.append("\nTotal: $" + str(int(total)))
    lineas.append("Cantidad: " + str(len(lista)))
    await update.message.reply_text("\n".join(lineas))

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = cargar_datos()
    if not datos:
        await update.message.reply_text("No hay historial.")
        return
    lineas = ["Historial ultimos 7 dias\n"]
    for fecha in sorted(datos.keys(), reverse=True)[:7]:
        lista = datos[fecha]
        total = sum(c["monto"] for c in lista)
        lineas.append(fecha + ": $" + str(int(total)) + " (" + str(len(lista)) + " comprobantes)")
    await update.message.reply_text("\n".join(lineas))

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_foto))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("historial", cmd_historial))
    logger.info("Bot iniciado!")
    app.run_polling(drop_pending_updates=True)
