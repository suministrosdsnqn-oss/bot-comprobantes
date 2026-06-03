import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import base64
import httpx

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
        
        nombre = datos_comprobante.get("nombre") or ""
        apellido = datos_comprobante.get("apellido") or ""
        monto = datos_comprobante.get("monto") or 0
        
        nombre_completo = f"{nombre} {apellido}".strip()
        if not nombre_completo:
            nombre_completo = "Desconocido"
        
        fecha_hoy = obtener_fecha_hoy()
        datos = cargar_datos()
        
        if fecha_hoy not in datos:
            datos[fecha_hoy] = []
        
        datos[fecha_hoy].append({
            "nombre": nombre_completo,
            "monto": monto,
            "hora": datetime.now().strftime("%H:%M")
        })
        
        guardar_datos(datos)
        
        total_hoy = sum(c["monto"] for c in datos[fecha_hoy])
        cantidad_hoy = len(datos[fecha_hoy])
        
        respuesta = (
            f"✅ *{nombre_completo}* — ${monto:,.0f}\n"
            f"📊 *Total hoy:* ${total_hoy:,.0f} ({cantidad_hoy} comprobante{'s' if cantidad_hoy > 1 else ''})\n\n"
            f"Escribí /resumen para ver el detalle completo de hoy."
        )
        
        await update.message.reply_text(respuesta, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ No pude leer el comprobante. Asegurate que la imagen sea clara.")

async def comando_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha_hoy = obtener_fecha_hoy()
    datos = cargar_datos()
    
    if fecha_hoy not in datos or not datos[fecha_hoy]:
        await update.message.reply_text("📭 No hay comprobantes registrados hoy.")
        return
    
    comprobantes_hoy = datos[fecha_hoy]
    total = sum(c["monto"] for c in comprobantes_hoy)
    
    lineas = [f"📋 *Resumen del {fecha_hoy}*\n"]
    for i, c in enumerate(comprobantes_hoy, 1):
        lineas.append(f"{i}. {c['nombre']} — ${c['monto']:,.0f} ({c['hora']})")
    
    lineas.append(f"\n💰 *Total: ${total:,.0f}*")
    lineas.append(f"📦 *Comprobantes: {len(comprobantes_hoy)}*")
    
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def comando_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = cargar_datos()
    
    if not datos:
        await update.message.reply_text("📭 No hay historial registrado.")
        return
    
    lineas = ["📅 *Historial por día*\n"]
    for fecha in sorted(datos.keys(), reverse=True)[:7]:
        comprobantes = datos[fecha]
        total = sum(c["monto"] for c in comprobantes)
        lineas.append(f"• {fecha}: ${total:,.0f} ({len(comprobantes)} comprobantes)")
    
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_foto))
    app.add_handler(CommandHandler("resumen", comando_resumen))
    app.add_handler(CommandHandler("historial", comando_historial))
    logger.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
