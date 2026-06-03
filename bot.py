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
SALDO_FILE = "saldo.json"

NOMBRE_TITULAR = "javier requena"

def cargar_datos():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def guardar_datos(datos):
    with open(DATA_FILE, "w") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

def cargar_saldo():
    if os.path.exists(SALDO_FILE):
        with open(SALDO_FILE, "r") as f:
            return json.load(f).get("saldo", 0)
    return 0

def guardar_saldo(saldo):
    with open(SALDO_FILE, "w") as f:
        json.dump({"saldo": saldo}, f)

def limpiar_monto(monto_raw):
    if monto_raw is None:
        return 0
    if isinstance(monto_raw, (int, float)):
        return float(monto_raw)
    monto_str = str(monto_raw).replace("$", "").replace(" ", "")
    monto_str = monto_str.replace(".", "").replace(",", ".")
    try:
        return float(monto_str)
    except Exception:
        return 0

def nombre_es_titular(nombre):
    if not nombre:
        return False
    return NOMBRE_TITULAR in nombre.lower()

def formatear_pesos(monto):
    return "$" + "{:,.0f}".format(monto).replace(",", ".")

def es_duplicado(datos, nombre_envia, monto, nro_comprobante):
    if not nro_comprobante:
        return False, None
    for fecha, lista in datos.items():
        for c in lista:
            if (
                c.get("nro_comprobante") and
                str(c.get("nro_comprobante")).strip() == str(nro_comprobante).strip() and
                c.get("envia", "").lower() == nombre_envia.lower() and
                abs(limpiar_monto(c.get("monto_original", c.get("monto", 0))) - monto) < 1
            ):
                return True, fecha + " " + c.get("hora", "")
    return False, None

def extraer_datos_imagen(image_data):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    img64 = base64.standard_b64encode(image_data).decode("utf-8")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img64}},
            {"type": "text", "text": "Analiza este comprobante de transferencia bancaria. Extrae todos estos datos. Responde SOLO JSON sin texto extra: {\"nombre_envia\": \"...\", \"apellido_envia\": \"...\", \"nombre_recibe\": \"...\", \"apellido_recibe\": \"...\", \"monto\": 1234.0, \"nro_comprobante\": \"...\"}. El nro_comprobante es el numero de operacion, codigo de transaccion o identificador unico del comprobante. Si no encuentras un dato usa null. El monto debe ser solo numeros sin puntos de miles ni comas."}
        ]}]
    )
    texto = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(texto)

def extraer_datos_pdf(pdf_data):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pdf64 = base64.standard_b64encode(pdf_data).decode("utf-8")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf64}},
            {"type": "text", "text": "Analiza este comprobante de transferencia bancaria. Extrae todos estos datos. Responde SOLO JSON sin texto extra: {\"nombre_envia\": \"...\", \"apellido_envia\": \"...\", \"nombre_recibe\": \"...\", \"apellido_recibe\": \"...\", \"monto\": 1234.0, \"nro_comprobante\": \"...\"}. El nro_comprobante es el numero de operacion, codigo de transaccion o identificador unico del comprobante. Si no encuentras un dato usa null. El monto debe ser solo numeros sin puntos de miles ni comas."}
        ]}]
    )
    texto = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(texto)

async def procesar_y_guardar(update, datos_comp):
    nombre_envia = ((datos_comp.get("nombre_envia") or "") + " " + (datos_comp.get("apellido_envia") or "")).strip() or "Desconocido"
    nombre_recibe = ((datos_comp.get("nombre_recibe") or "") + " " + (datos_comp.get("apellido_recibe") or "")).strip() or "Desconocido"
    monto_original = limpiar_monto(datos_comp.get("monto"))
    nro_comprobante = datos_comp.get("nro_comprobante") or None

    envia_titular = nombre_es_titular(nombre_envia)
    recibe_titular = nombre_es_titular(nombre_recibe)

    datos = cargar_datos()
    hoy = datetime.now().strftime("%Y-%m-%d")
    if hoy not in datos:
        datos[hoy] = []

    duplicado, fecha_original = es_duplicado(datos, nombre_envia, monto_original, nro_comprobante)
    if duplicado:
        saldo = cargar_saldo()
        msg = (
            "DUPLICADO - Esta transferencia ya fue registrada\n"
            "Envia: " + nombre_envia + "\n"
            "Monto: " + formatear_pesos(monto_original) + "\n"
            "Nro comprobante: " + str(nro_comprobante) + "\n"
            "Registrada el: " + fecha_original + "\n"
            "Saldo sin cambios: " + formatear_pesos(saldo)
        )
        await update.message.reply_text(msg)
        return

    saldo = cargar_saldo()

    if envia_titular and not recibe_titular:
        saldo = saldo - monto_original
        guardar_saldo(saldo)
        datos[hoy].append({
            "tipo": "egreso",
            "envia": nombre_envia,
            "recibe": nombre_recibe,
            "monto": monto_original,
            "nro_comprobante": nro_comprobante,
            "hora": datetime.now().strftime("%H:%M")
        })
        guardar_datos(datos)
        msg = (
            "EGRESO - " + nombre_envia + " envia a " + nombre_recibe + "\n"
            "Monto: " + formatear_pesos(monto_original) + "\n"
            "Nro comprobante: " + str(nro_comprobante or "no encontrado") + "\n"
            "Saldo actual: " + formatear_pesos(saldo)
        )
        await update.message.reply_text(msg)

    elif recibe_titular:
        comision = monto_original * 0.01
        monto_neto = monto_original - comision
        saldo = saldo + monto_neto
        guardar_saldo(saldo)
        datos[hoy].append({
            "tipo": "ingreso",
            "envia": nombre_envia,
            "recibe": nombre_recibe,
            "monto_original": monto_original,
            "monto_neto": monto_neto,
            "comision": comision,
            "nro_comprobante": nro_comprobante,
            "hora": datetime.now().strftime("%H:%M")
        })
        guardar_datos(datos)
        msg = (
            "INGRESO - " + nombre_envia + " envia a " + nombre_recibe + "\n"
            "Monto recibido: " + formatear_pesos(monto_original) + "\n"
            "Restamos 1%: -" + formatear_pesos(comision) + "\n"
            "Monto neto: " + formatear_pesos(monto_neto) + "\n"
            "Nro comprobante: " + str(nro_comprobante or "no encontrado") + "\n"
            "Saldo actual: " + formatear_pesos(saldo)
        )
        await update.message.reply_text(msg)

    else:
        msg = (
            "IGNORADO - Transferencia no corresponde a Javier Requena\n"
            "Envia: " + nombre_envia + "\n"
            "Recibe: " + nombre_recibe + "\n"
            "Saldo sin cambios: " + formatear_pesos(saldo)
        )
        await update.message.reply_text(msg)

async def handle_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        foto = update.message.photo[-1]
        archivo = await context.bot.get_file(foto.file_id)
        imagen = bytes(await archivo.download_as_bytearray())
        await update.message.reply_text("Procesando...")
        datos_comp = extraer_datos_imagen(imagen)
        await procesar_y_guardar(update, datos_comp)
    except Exception as e:
        logger.error(str(e))
        await update.message.reply_text("Error: " + str(e))

async def handle_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        archivo = await context.bot.get_file(doc.file_id)
        datos_archivo = bytes(await archivo.download_as_bytearray())
        await update.message.reply_text("Procesando...")
        if doc.mime_type == "application/pdf":
            datos_comp = extraer_datos_pdf(datos_archivo)
        else:
            datos_comp = extraer_datos_imagen(datos_archivo)
        await procesar_y_guardar(update, datos_comp)
    except Exception as e:
        logger.error(str(e))
        await update.message.reply_text("Error: " + str(e))

async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.now().strftime("%Y-%m-%d")
    datos = cargar_datos()
    saldo = cargar_saldo()
    if hoy not in datos or not datos[hoy]:
        await update.message.reply_text("No hay movimientos hoy.\nSaldo actual: " + formatear_pesos(saldo))
        return
    lista = datos[hoy]
    lineas = ["Resumen " + hoy + "\n"]
    for i, c in enumerate(lista, 1):
        if c["tipo"] == "ingreso":
            lineas.append(str(i) + ". INGRESO de " + c["envia"] + " - Neto: " + formatear_pesos(c["monto_neto"]) + " (" + c["hora"] + ")")
        else:
            lineas.append(str(i) + ". EGRESO a " + c["recibe"] + " - " + formatear_pesos(c["monto"]) + " (" + c["hora"] + ")")
    lineas.append("\nSaldo actual: " + formatear_pesos(saldo))
    await update.message.reply_text("\n".join(lineas))

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = cargar_datos()
    saldo = cargar_saldo()
    if not datos:
        await update.message.reply_text("No hay historial.")
        return
    lineas = ["Historial ultimos 7 dias\n"]
    for fecha in sorted(datos.keys(), reverse=True)[:7]:
        lista = datos[fecha]
        ingresos = sum(c.get("monto_neto", 0) for c in lista if c["tipo"] == "ingreso")
        egresos = sum(c.get("monto", 0) for c in lista if c["tipo"] == "egreso")
        lineas.append(fecha + ": +" + formatear_pesos(ingresos) + " / -" + formatear_pesos(egresos))
    lineas.append("\nSaldo actual: " + formatear_pesos(saldo))
    await update.message.reply_text("\n".join(lineas))

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = cargar_saldo()
    await update.message.reply_text("Saldo actual: " + formatear_pesos(saldo))

async def cmd_resetear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guardar_saldo(0)
    await update.message.reply_text("Saldo reseteado a $0")

async def cmd_setear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        monto = limpiar_monto(context.args[0] if context.args else None)
        guardar_saldo(monto)
        await update.message.reply_text("Saldo establecido en: " + formatear_pesos(monto))
    except Exception:
        await update.message.reply_text("Uso correcto: /setear 500000")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = cargar_saldo()
    await update.message.reply_text(
        "Bot de comprobantes activo!\n\n"
        "Manda una foto o PDF de un comprobante y lo proceso automaticamente.\n\n"
        "Comandos:\n"
        "/saldo - ver saldo actual\n"
        "/resumen - movimientos de hoy\n"
        "/historial - ultimos 7 dias\n"
        "/setear 500000 - establecer saldo inicial\n"
        "/resetear - poner saldo en 0\n\n"
        "Saldo actual: " + formatear_pesos(saldo)
    )

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("resetear", cmd_resetear))
    app.add_handler(CommandHandler("setear", cmd_setear))
    app.add_handler(MessageHandler(filters.PHOTO, handle_foto))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_documento))
    logger.info("Bot iniciado!")
    app.run_polling(drop_pending_updates=True)
