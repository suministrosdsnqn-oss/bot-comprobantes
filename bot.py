import os, json, logging, base64, urllib.parse
from datetime import datetime
import pg8000
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

PALABRAS_TITULAR = ["javier", "requena", "osiecki", "reguena"]
CVU_COPTER = "0000053600000016266791"
CVU_FIWIND = "0000267900000000287683"
COMISION_COPTER = 0.01
COMISION_FIWIND = 0.05

def parse_db_url():
    url = urllib.parse.urlparse(DATABASE_URL)
    return {"host": url.hostname, "port": url.port or 5432, "database": url.path.lstrip("/"), "user": url.username, "password": url.password}

def get_conn():
    p = parse_db_url()
    return pg8000.connect(host=p["host"], port=p["port"], database=p["database"], user=p["user"], password=p["password"])

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS saldo (id INTEGER PRIMARY KEY, monto FLOAT DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS comprobantes (id SERIAL PRIMARY KEY, fecha TEXT, hora TEXT, tipo TEXT, envia TEXT, recibe TEXT, cuenta TEXT, monto_original FLOAT, monto_neto FLOAT, comision FLOAT, monto_egreso FLOAT, nro_comprobante TEXT)")
    cur.execute("INSERT INTO saldo (id, monto) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    conn.commit()
    cur.close()
    conn.close()

def cargar_saldo():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT monto FROM saldo WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0

def guardar_saldo(monto):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE saldo SET monto = %s WHERE id = 1", (monto,))
    conn.commit()
    cur.close()
    conn.close()

def guardar_comprobante(tipo, envia, recibe, cuenta, monto_original, monto_neto, comision, monto_egreso, nro_comprobante):
    conn = get_conn()
    cur = conn.cursor()
    hoy = datetime.now().strftime("%Y-%m-%d")
    hora = datetime.now().strftime("%H:%M")
    cur.execute("INSERT INTO comprobantes (fecha, hora, tipo, envia, recibe, cuenta, monto_original, monto_neto, comision, monto_egreso, nro_comprobante) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (hoy, hora, tipo, envia, recibe, cuenta, monto_original, monto_neto, comision, monto_egreso, nro_comprobante))
    conn.commit()
    cur.close()
    conn.close()

def es_duplicado(nombre_envia, monto, nro_comprobante):
    conn = get_conn()
    cur = conn.cursor()
    if nro_comprobante:
        cur.execute("SELECT fecha, hora FROM comprobantes WHERE nro_comprobante = %s AND ABS((monto_original + monto_egreso) - %s) < 1",
            (str(nro_comprobante), monto))
    else:
        hoy = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT fecha, hora FROM comprobantes WHERE LOWER(envia) = LOWER(%s) AND ABS((monto_original + monto_egreso) - %s) < 1 AND fecha = %s",
            (nombre_envia, monto, hoy))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return True, row[0] + " " + row[1]
    return False, None

def get_comprobantes_hoy():
    hoy = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT tipo, envia, recibe, monto_neto, monto_egreso, hora FROM comprobantes WHERE fecha = %s ORDER BY hora", (hoy,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_historial():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT fecha, SUM(CASE WHEN tipo = 'ingreso' THEN monto_neto ELSE 0 END), SUM(CASE WHEN tipo = 'egreso' THEN monto_egreso ELSE 0 END) FROM comprobantes WHERE fecha >= CURRENT_DATE - INTERVAL '7 days' GROUP BY fecha ORDER BY fecha DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

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
    nombre_lower = nombre.lower()
    coincidencias = sum(1 for palabra in PALABRAS_TITULAR if palabra in nombre_lower)
    return coincidencias >= 2

def detectar_cuenta_destino(cvu):
    if not cvu:
        return None, 0
    cvu_limpio = str(cvu).replace(" ", "").replace("-", "")
    if CVU_COPTER in cvu_limpio or cvu_limpio in CVU_COPTER:
        return "Copter", COMISION_COPTER
    if CVU_FIWIND in cvu_limpio or cvu_limpio in CVU_FIWIND:
        return "Fiwind", COMISION_FIWIND
    return None, 0

def formatear_pesos(monto):
    return "$" + "{:,.0f}".format(monto).replace(",", ".")

def extraer_datos_imagen(image_data):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    img64 = base64.standard_b64encode(image_data).decode("utf-8")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img64}},
            {"type": "text", "text": "Analiza este comprobante de transferencia bancaria. Responde SOLO JSON sin texto extra: {\"nombre_envia\": \"...\", \"apellido_envia\": \"...\", \"nombre_recibe\": \"...\", \"apellido_recibe\": \"...\", \"monto\": 1234.0, \"nro_comprobante\": \"...\", \"cvu_destino\": \"...\"}. Si no encuentras un dato usa null. El monto debe ser solo numeros sin puntos de miles ni comas."}
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
            {"type": "text", "text": "Analiza este comprobante de transferencia bancaria. Responde SOLO JSON sin texto extra: {\"nombre_envia\": \"...\", \"apellido_envia\": \"...\", \"nombre_recibe\": \"...\", \"apellido_recibe\": \"...\", \"monto\": 1234.0, \"nro_comprobante\": \"...\", \"cvu_destino\": \"...\"}. Si no encuentras un dato usa null. El monto debe ser solo numeros sin puntos de miles ni comas."}
        ]}]
    )
    texto = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(texto)

async def procesar_y_guardar(update, datos_comp):
    nombre_envia = ((datos_comp.get("nombre_envia") or "") + " " + (datos_comp.get("apellido_envia") or "")).strip() or "Desconocido"
    nombre_recibe = ((datos_comp.get("nombre_recibe") or "") + " " + (datos_comp.get("apellido_recibe") or "")).strip() or "Desconocido"
    monto_original = limpiar_monto(datos_comp.get("monto"))
    nro_comprobante = datos_comp.get("nro_comprobante") or None
    cvu_destino = datos_comp.get("cvu_destino") or None

    cuenta_destino, comision_rate = detectar_cuenta_destino(cvu_destino)
    envia_titular = nombre_es_titular(nombre_envia)
    recibe_titular = nombre_es_titular(nombre_recibe) or (cuenta_destino is not None)

    duplicado, fecha_original = es_duplicado(nombre_envia, monto_original, nro_comprobante)
    if duplicado:
        saldo = cargar_saldo()
        lineas = ["*DUPLICADO - Transferencia ya registrada*", "Envia: " + nombre_envia, "Monto: " + formatear_pesos(monto_original), "Nro comprobante: " + str(nro_comprobante or "no encontrado"), "Registrada el: " + fecha_original, "*Saldo sin cambios: " + formatear_pesos(saldo) + "*"]
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")
        return

    saldo = cargar_saldo()

    if recibe_titular and not envia_titular:
        if comision_rate == 0:
            comision_rate = COMISION_COPTER
        comision = monto_original * comision_rate
        monto_neto = monto_original - comision
        saldo = saldo + monto_neto
        guardar_saldo(saldo)
        cuenta_label = cuenta_destino if cuenta_destino else "Cuenta"
        guardar_comprobante("ingreso", nombre_envia, nombre_recibe, cuenta_label, monto_original, monto_neto, comision, 0, nro_comprobante)
        lineas = ["*INGRESO - " + cuenta_label + "*", "De: " + nombre_envia, "Para: " + nombre_recibe, "Monto recibido: " + formatear_pesos(monto_original), "Monto neto: " + formatear_pesos(monto_neto), "Nro comprobante: " + str(nro_comprobante or "no encontrado"), "*Saldo actual: " + formatear_pesos(saldo) + "*"]
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

    elif envia_titular and not recibe_titular:
        saldo = saldo - monto_original
        guardar_saldo(saldo)
        guardar_comprobante("egreso", nombre_envia, nombre_recibe, "", 0, 0, 0, monto_original, nro_comprobante)
        lineas = ["*EGRESO*", "De: " + nombre_envia, "Para: " + nombre_recibe, "Monto enviado: " + formatear_pesos(monto_original), "Nro comprobante: " + str(nro_comprobante or "no encontrado"), "*Saldo actual: " + formatear_pesos(saldo) + "*"]
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

    else:
        lineas = ["*IGNORADO*", "Esta transferencia no corresponde a Javier Requena", "De: " + nombre_envia, "Para: " + nombre_recibe, "*Saldo sin cambios: " + formatear_pesos(saldo) + "*"]
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

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
    saldo = cargar_saldo()
    lista = get_comprobantes_hoy()
    hoy = datetime.now().strftime("%Y-%m-%d")
    if not lista:
        await update.message.reply_text("No hay movimientos hoy.\n*Saldo actual: " + formatear_pesos(saldo) + "*", parse_mode="Markdown")
        return
    lineas = ["*Resumen " + hoy + "*\n"]
    for i, c in enumerate(lista, 1):
        if c[0] == "ingreso":
            lineas.append(str(i) + ". INGRESO de " + c[1] + " - Neto: " + formatear_pesos(c[3]) + " (" + c[5] + ")")
        else:
            lineas.append(str(i) + ". EGRESO a " + c[2] + " - " + formatear_pesos(c[4]) + " (" + c[5] + ")")
    lineas.append("\n*Saldo actual: " + formatear_pesos(saldo) + "*")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = cargar_saldo()
    rows = get_historial()
    if not rows:
        await update.message.reply_text("No hay historial.")
        return
    lineas = ["*Historial ultimos 7 dias*\n"]
    for r in rows:
        lineas.append(r[0] + ": +" + formatear_pesos(r[1]) + " / -" + formatear_pesos(r[2]))
    lineas.append("\n*Saldo actual: " + formatear_pesos(saldo) + "*")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = cargar_saldo()
    await update.message.reply_text("*Saldo actual: " + formatear_pesos(saldo) + "*", parse_mode="Markdown")

async def cmd_resetear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guardar_saldo(0)
    await update.message.reply_text("Saldo reseteado.\n*Saldo actual: $0*", parse_mode="Markdown")

async def cmd_setear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        monto = limpiar_monto(context.args[0] if context.args else None)
        guardar_saldo(monto)
        await update.message.reply_text("Saldo establecido.\n*Saldo actual: " + formatear_pesos(monto) + "*", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("Uso correcto: /setear 500000")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    saldo = cargar_saldo()
    await update.message.reply_text("*Bot de comprobantes activo*\n\nManda una foto o PDF de un comprobante y lo proceso automaticamente.\n\nComandos:\n/saldo - ver saldo actual\n/resumen - movimientos de hoy\n/historial - ultimos 7 dias\n/setear 500000 - establecer saldo inicial\n/resetear - poner saldo en 0\n\n*Saldo actual: " + formatear_pesos(saldo) + "*", parse_mode="Markdown")

if __name__ == "__main__":
    init_db()
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
