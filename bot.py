import os
import re
import json
import html
import time
import base64
import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

import pg8000
import anthropic
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("comprobantes")
# Evita que Railway muestre el token del bot en cada consulta a Telegram
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Hora de Argentina (UTC-3, sin horario de verano)
TZ_AR = timezone(timedelta(hours=-3))

# ---------------------------------------------------------------------------
# Reglas del negocio
# ---------------------------------------------------------------------------
PALABRAS_TITULAR = ["javier", "requena", "osiecki", "reguena"]
CUENTAS_TITULAR = {
    "Copter": ("0000053600000016266791", Decimal("0.01")),
    "Fiwind": ("0000267900000000287683", Decimal("0.05")),
}
COMISION_SIN_CUENTA = Decimal("0.01")

# Dos modelos distintos leen cada comprobante por separado.
# Si no coinciden, NO se registra nada.
MODELOS_LECTURA = ["claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
MODELOS_VERIFICACION = ["claude-opus-5-5", "claude-opus-5", "claude-sonnet-4-6"]
MODELO_LECTURA = MODELOS_LECTURA[0]
MODELO_VERIFICACION = MODELOS_VERIFICACION[0]

CENT = Decimal("0.01")
cliente_ia = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=3, timeout=90.0)

PROMPT_LECTURA = """Sos un lector de comprobantes de transferencias bancarias de Argentina.
Tu unica tarea es COPIAR los datos tal cual aparecen en el comprobante.
No calcules, no redondees, no corrijas y no inventes nada.

Responde UNICAMENTE con un objeto JSON, sin texto antes ni despues.
Todos los valores tienen que ir como TEXTO entre comillas dobles, o null si el dato no aparece.

{
  "monto_texto": "importe transferido copiado EXACTAMENTE como esta impreso, con $, puntos y comas",
  "monto_solo_digitos": "el mismo importe escrito solo con numeros, sin puntos; si tiene centavos, coma y centavos",
  "origen_nombre": "nombre completo de quien ENVIA el dinero",
  "origen_cuenta": "CVU o CBU de quien envia (solo los 22 numeros)",
  "destino_nombre": "nombre completo de quien RECIBE el dinero",
  "destino_cuenta": "CVU o CBU de quien recibe (solo los 22 numeros). Puede figurar como CVU o como CBU",
  "nro_operacion": "numero de operacion / de transaccion / de comprobante",
  "codigo_identificacion": "codigo de identificacion / ID COELSA / ID de transferencia, si aparece"
}

Reglas del importe:
- En Argentina el PUNTO separa los miles y la COMA separa los centavos.
- Ejemplo: si dice "$ 123.456" -> "monto_texto": "$ 123.456" y "monto_solo_digitos": "123456" (ciento veintitres mil cuatrocientos cincuenta y seis pesos).
- Ejemplo: si dice "$ 1.234.567,89" -> "monto_texto": "$ 1.234.567,89" y "monto_solo_digitos": "1234567,89".
- Copia TODOS los digitos, sin omitir ninguno.
- Usa el importe principal transferido (el que recibe el destinatario), no comisiones ni saldos.
- Si algo no se lee con total claridad, pone null. Nunca adivines."""

PROMPT_VERIFICACION = """Mira este comprobante de transferencia bancaria argentina y copia EXACTAMENTE, tal cual esta impreso:
1) el importe transferido,
2) el CVU o CBU de la cuenta que ENVIA el dinero,
3) el CVU o CBU de la cuenta que RECIBE el dinero (puede decir CVU o CBU).

Responde UNICAMENTE con este JSON, con los valores como TEXTO entre comillas, o null si no se ve:
{"importe": "...", "cuenta_origen": "...", "cuenta_destino": "..."}

En Argentina el punto separa los miles y la coma los centavos: "$ 98.765" son noventa y ocho mil setecientos sesenta y cinco pesos; "$ 2.500.000,75" son dos millones quinientos mil pesos con setenta y cinco centavos. Copia todos los digitos. Si algo no se lee con total claridad, pone null."""

# ---------------------------------------------------------------------------
# Utilidades de texto y montos
# ---------------------------------------------------------------------------
_NULOS = {"", "null", "none", "nan", "n/a", "na", "-", "--", "sin dato", "no disponible", "no aparece"}


def ahora_ar():
    return datetime.now(TZ_AR)


def limpiar_texto(valor):
    if valor is None:
        return None
    s = str(valor).strip()
    if s.lower() in _NULOS:
        return None
    return s


def parsear_monto(valor):
    """Convierte un importe al valor exacto usando las reglas argentinas.
    Punto = miles, coma = centavos. Si el formato es dudoso devuelve None (no se adivina)."""
    s = limpiar_texto(valor)
    if s is None:
        return None
    s = s.replace("\u00a0", "").replace(" ", "")
    s = re.sub(r"(?i)ars|pesos|\$", "", s)
    if not s or not re.fullmatch(r"[0-9.,]+", s) or not re.search(r"\d", s):
        return None
    if "," in s:
        if s.count(",") != 1:
            return None
        entero, dec = s.split(",")
        if not re.fullmatch(r"(?:\d{1,3}(?:\.\d{3})*|\d+)", entero):
            return None
        if not re.fullmatch(r"\d{1,2}", dec):
            return None
        numero = entero.replace(".", "") + "." + dec
    elif "." not in s:
        numero = s
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        # 486.392 / 1.234.567 -> puntos de miles
        numero = s.replace(".", "")
    elif re.fullmatch(r"\d+\.\d{1,2}", s):
        # 486392.50 -> decimal
        numero = s
    else:
        return None
    try:
        d = Decimal(numero)
    except InvalidOperation:
        return None
    if d <= 0:
        return None
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_pesos(valor):
    try:
        v = Decimal(str(valor)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        v = Decimal("0.00")
    signo = "-" if v < 0 else ""
    s = format(abs(v), ",.2f").replace(",", "X").replace(".", ",").replace("X", ".")
    return signo + "$" + s


def esc(valor):
    return html.escape(str(valor)) if valor is not None else "-"


def normalizar_id(valor):
    s = limpiar_texto(valor)
    if not s:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s if len(s) >= 6 else None


def solo_digitos(valor):
    s = limpiar_texto(valor)
    if s is None:
        return None
    d = re.sub(r"\D", "", s)
    return d or None


def identificar_cuenta(valor):
    """Devuelve 'Copter' o 'Fiwind' si el CVU/CBU es de Javier (tolera 1 digito mal leido)."""
    d = solo_digitos(valor)
    if not d or len(d) != 22:
        return None
    for nombre, (cvu, _) in CUENTAS_TITULAR.items():
        diferencias = sum(1 for a, b in zip(d, cvu) if a != b)
        if diferencias <= 1:
            return nombre
    return None


def nombre_es_titular(nombre):
    s = limpiar_texto(nombre)
    if not s:
        return False
    s = s.lower()
    return sum(1 for p in PALABRAS_TITULAR if p in s) >= 2


def decidir(origen_nombre, destino_nombre, origen_cuenta, destino_cuenta):
    cta_origen = identificar_cuenta(origen_cuenta)
    cta_destino = identificar_cuenta(destino_cuenta)
    envia_titular = nombre_es_titular(origen_nombre) or cta_origen is not None
    recibe_titular = nombre_es_titular(destino_nombre) or cta_destino is not None
    if recibe_titular and not envia_titular:
        comision = CUENTAS_TITULAR[cta_destino][1] if cta_destino else COMISION_SIN_CUENTA
        return {"tipo": "ingreso", "comision": comision, "cuenta": cta_destino}
    if envia_titular and not recibe_titular:
        return {"tipo": "egreso", "comision": Decimal("0"), "cuenta": cta_origen}
    if envia_titular and recibe_titular:
        return {"tipo": "ignorado", "comision": Decimal("0"), "cuenta": None}
    if limpiar_texto(destino_nombre) is None:
        # No se pudo leer a quien va la plata: no se descarta ni se registra a ciegas
        return {"tipo": "ilegible", "comision": Decimal("0"), "cuenta": None}
    return {"tipo": "ignorado", "comision": Decimal("0"), "cuenta": None}


# ---------------------------------------------------------------------------
# Lectura con IA
# ---------------------------------------------------------------------------
def detectar_media_type(data):
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def extraer_json(texto):
    if not texto:
        raise ValueError("respuesta vacia")
    ini = texto.find("{")
    fin = texto.rfind("}")
    if ini == -1 or fin <= ini:
        raise ValueError("la respuesta no tiene JSON")
    bloque = texto[ini:fin + 1]
    # Numeros sin comillas con ceros adelante (CVU/CBU) no son JSON valido: se pasan a texto
    bloque = re.sub(r'(:\s*)(0\d+)(\s*[,}])', r'\1"\2"\3', bloque)
    # parse_float/parse_int=str: NINGUN numero se convierte solo. "486.392" queda como texto
    # y despues se interpreta con reglas argentinas.
    datos = json.loads(bloque, parse_float=str, parse_int=str)
    if not isinstance(datos, dict):
        raise ValueError("JSON invalido")
    return datos


def leer_con_modelo(modelo, prompt, data, media_type, intentos=3):
    b64 = base64.standard_b64encode(data).decode("ascii")
    if media_type == "application/pdf":
        adjunto = {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    else:
        adjunto = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    ultimo_error = None
    for i in range(intentos):
        try:
            resp = cliente_ia.messages.create(
                model=modelo,
                max_tokens=1000,
                messages=[{"role": "user", "content": [adjunto, {"type": "text", "text": prompt}]}],
            )
            texto = "".join(getattr(b, "text", "") or "" for b in resp.content)
            datos = extraer_json(texto)
            logger.info("Lectura %s: %s", modelo, json.dumps(datos, ensure_ascii=False)[:700])
            return datos
        except Exception as e:
            ultimo_error = e
            logger.warning("Fallo la lectura con %s (intento %d): %s", modelo, i + 1, e)
            if i < intentos - 1:
                time.sleep(2 * (i + 1))
    raise ultimo_error


def elegir_modelo(candidatos):
    for m in candidatos:
        try:
            cliente_ia.messages.create(model=m, max_tokens=5, messages=[{"role": "user", "content": "ok"}])
            logger.info("Modelo disponible: %s", m)
            return m
        except Exception as e:
            logger.warning("Modelo %s no disponible: %s", m, e)
    logger.error("Ningun modelo respondio en la verificacion inicial, se usa %s", candidatos[0])
    return candidatos[0]


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def _conectar():
    url = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.connect(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/"),
        user=urllib.parse.unquote(url.username or ""),
        password=urllib.parse.unquote(url.password or ""),
        timeout=30,
    )


def conectar(intentos=4):
    ultimo_error = None
    for i in range(intentos):
        try:
            return _conectar()
        except Exception as e:
            ultimo_error = e
            logger.warning("No se pudo conectar a la base (intento %d): %s", i + 1, e)
            if i < intentos - 1:
                time.sleep(2 * (i + 1))
    raise ultimo_error


def init_db():
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS saldo (id INTEGER PRIMARY KEY, monto FLOAT DEFAULT 0)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS comprobantes (id SERIAL PRIMARY KEY, fecha TEXT, hora TEXT, tipo TEXT, "
            "envia TEXT, recibe TEXT, cuenta TEXT, monto_original FLOAT, monto_neto FLOAT, comision FLOAT, "
            "monto_egreso FLOAT, nro_comprobante TEXT)"
        )
        cur.execute("ALTER TABLE comprobantes ADD COLUMN IF NOT EXISTS codigo_id TEXT")
        cur.execute("ALTER TABLE comprobantes ADD COLUMN IF NOT EXISTS detalle TEXT")
        cur.execute("INSERT INTO saldo (id, monto) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
        conn.commit()
    finally:
        conn.close()


_SQL_NRO = "UPPER(regexp_replace(COALESCE(nro_comprobante, ''), '[^A-Za-z0-9]', '', 'g'))"
_SQL_COD = "UPPER(regexp_replace(COALESCE(codigo_id, ''), '[^A-Za-z0-9]', '', 'g'))"


def _buscar_duplicado(cur, ids, envia, monto):
    ids = [i for i in ids if i]
    if ids:
        marcas = ", ".join(["%s"] * len(ids))
        cur.execute(
            "SELECT fecha, hora FROM comprobantes WHERE " + _SQL_NRO + " IN (" + marcas + ") OR "
            + _SQL_COD + " IN (" + marcas + ") ORDER BY id LIMIT 1",
            tuple(ids) + tuple(ids),
        )
        fila = cur.fetchone()
        return (str(fila[0]) + " " + str(fila[1])) if fila else None
    if monto is None:
        return None
    # Comprobante sin ningun numero: mismo remitente + mismo monto + mismo dia
    hoy = ahora_ar().strftime("%Y-%m-%d")
    cur.execute(
        "SELECT fecha, hora FROM comprobantes WHERE LOWER(COALESCE(envia, '')) = LOWER(%s) "
        "AND ABS(COALESCE(monto_original, 0) + COALESCE(monto_egreso, 0) - %s) < 0.5 AND fecha = %s "
        "ORDER BY id LIMIT 1",
        (envia or "", float(monto), hoy),
    )
    fila = cur.fetchone()
    return (str(fila[0]) + " " + str(fila[1])) if fila else None


def buscar_duplicado(ids, envia, monto):
    conn = conectar()
    try:
        return _buscar_duplicado(conn.cursor(), ids, envia, monto)
    finally:
        conn.close()


def registrar_movimiento(mov):
    """Guarda el comprobante y actualiza el saldo en UNA sola operacion.
    O se guardan las dos cosas, o ninguna. El saldo queda bloqueado mientras tanto."""
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT monto FROM saldo WHERE id = 1 FOR UPDATE")
        fila = cur.fetchone()
        if fila is None:
            cur.execute("INSERT INTO saldo (id, monto) VALUES (1, 0)")
            saldo_previo = 0.0
        else:
            saldo_previo = fila[0]
        dup = _buscar_duplicado(cur, [mov["nro"], mov["codigo"]], mov["envia"], mov["monto"])
        if dup:
            conn.rollback()
            return {"duplicado": dup, "saldo": saldo_previo}
        ahora = ahora_ar()
        cur.execute(
            "INSERT INTO comprobantes (fecha, hora, tipo, envia, recibe, cuenta, monto_original, monto_neto, "
            "comision, monto_egreso, nro_comprobante, codigo_id, detalle) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ahora.strftime("%Y-%m-%d"), ahora.strftime("%H:%M"), mov["tipo"], mov["envia"], mov["recibe"],
                mov["cuenta"], float(mov["monto_original"]), float(mov["monto_neto"]), float(mov["comision"]),
                float(mov["monto_egreso"]), mov["nro"], mov["codigo"], mov["detalle"],
            ),
        )
        cur.execute(
            "UPDATE saldo SET monto = ROUND(CAST(monto AS NUMERIC) + %s, 2) WHERE id = 1 RETURNING monto",
            (mov["delta"],),
        )
        nuevo = cur.fetchone()[0]
        conn.commit()
        return {"duplicado": None, "saldo": nuevo}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def leer_saldo():
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT monto FROM saldo WHERE id = 1")
        fila = cur.fetchone()
        return fila[0] if fila else 0.0
    finally:
        conn.close()


def fijar_saldo(valor):
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO saldo (id, monto) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET monto = EXCLUDED.monto",
            (float(valor),),
        )
        conn.commit()
    finally:
        conn.close()


def movimientos_del_dia(fecha):
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT tipo, envia, recibe, monto_neto, monto_egreso, hora FROM comprobantes WHERE fecha = %s ORDER BY id",
            (fecha,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def historial(dias=7):
    desde = (ahora_ar() - timedelta(days=dias - 1)).strftime("%Y-%m-%d")
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT fecha, SUM(CASE WHEN tipo = 'ingreso' THEN COALESCE(monto_neto, 0) ELSE 0 END), "
            "SUM(CASE WHEN tipo = 'egreso' THEN COALESCE(monto_egreso, 0) ELSE 0 END) "
            "FROM comprobantes WHERE fecha >= %s GROUP BY fecha ORDER BY fecha DESC",
            (desde,),
        )
        return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Logica principal: verificar y registrar
# ---------------------------------------------------------------------------
MSG_ERROR = (
    "❌ <b>Error al procesar. NO se registró nada.</b>\n"
    "Reenviá el comprobante en unos segundos (si ya había quedado registrado, el bot lo va a marcar como duplicado)."
)


def evaluar_y_registrar(a, b):
    origen = limpiar_texto(a.get("origen_nombre"))
    destino = limpiar_texto(a.get("destino_nombre"))
    nro_mostrar = limpiar_texto(a.get("nro_operacion")) or limpiar_texto(a.get("codigo_identificacion"))
    nro = normalizar_id(a.get("nro_operacion"))
    codigo = normalizar_id(a.get("codigo_identificacion"))

    monto_texto_1 = limpiar_texto(a.get("monto_texto"))
    monto_texto_2 = limpiar_texto(b.get("importe"))
    monto_1 = parsear_monto(monto_texto_1)
    monto_1_digitos = parsear_monto(a.get("monto_solo_digitos"))
    monto_2 = parsear_monto(monto_texto_2)

    dec_1 = decidir(origen, destino, a.get("origen_cuenta"), a.get("destino_cuenta"))
    dec_2 = decidir(origen, destino, b.get("cuenta_origen"), b.get("cuenta_destino"))

    # 1) Transferencias que no son de Javier: no se tocan
    if dec_1["tipo"] == "ignorado" and dec_2["tipo"] == "ignorado":
        saldo = leer_saldo()
        return (
            "<b>IGNORADO</b>\n"
            "Esta transferencia no corresponde a Javier Requena\n"
            "De: " + esc(origen or "Desconocido") + "\n"
            "Para: " + esc(destino or "Desconocido") + "\n"
            "<b>Saldo sin cambios: " + fmt_pesos(saldo) + "</b>"
        )

    # 2) Duplicados (por numero de operacion o codigo de identificacion, sin importar el monto)
    monto_ref = monto_1 or monto_2
    dup = buscar_duplicado([nro, codigo], origen, monto_ref)
    if dup:
        saldo = leer_saldo()
        return (
            "<b>DUPLICADO - Transferencia ya registrada</b>\n"
            "Envía: " + esc(origen or "Desconocido") + "\n"
            "Monto: " + (fmt_pesos(monto_ref) if monto_ref else "-") + "\n"
            "Nro comprobante: " + esc(nro_mostrar or "no encontrado") + "\n"
            "Registrada el: " + esc(dup) + "\n"
            "<b>Saldo sin cambios: " + fmt_pesos(saldo) + "</b>"
        )

    # 3) Controles: si hay la minima duda, NO se registra
    problemas = []
    if dec_1["tipo"] == "ilegible" or dec_2["tipo"] == "ilegible":
        problemas.append("no se pudo leer a quién va la transferencia")
    if monto_1 is None:
        problemas.append("la lectura 1 no pudo leer el monto con seguridad")
    if monto_2 is None:
        problemas.append("la lectura 2 no pudo leer el monto con seguridad")
    if monto_1 is not None and monto_1_digitos is not None and monto_1 != monto_1_digitos:
        problemas.append("la lectura 1 dio dos montos distintos")
    if monto_1 is not None and monto_2 is not None and monto_1 != monto_2:
        problemas.append("las dos lecturas del monto no coinciden")
    if dec_1["tipo"] != dec_2["tipo"] or dec_1["comision"] != dec_2["comision"]:
        problemas.append("las dos lecturas no coinciden en la cuenta de origen/destino")

    if problemas:
        saldo = leer_saldo()
        logger.warning("NO REGISTRADO: %s | lectura1=%s | lectura2=%s", problemas, a, b)
        return (
            "⚠️ <b>NO REGISTRADO - no se pudo verificar</b>\n"
            "Motivo: " + esc("; ".join(problemas)) + "\n"
            "Lectura 1: " + esc(monto_texto_1 or "-") + "\n"
            "Lectura 2: " + esc(monto_texto_2 or "-") + "\n"
            "Reenviá el comprobante como captura de pantalla nítida (no foto de la pantalla).\n"
            "<b>Saldo sin cambios: " + fmt_pesos(saldo) + "</b>"
        )

    # 4) Todo coincide: calcular y registrar
    monto = monto_1
    tipo = dec_1["tipo"]
    cuenta = dec_1["cuenta"] or dec_2["cuenta"]
    detalle = json.dumps({"lectura_1": a, "lectura_2": b, "modelos": [MODELO_LECTURA, MODELO_VERIFICACION]},
                         ensure_ascii=False)

    if tipo == "ingreso":
        comision_pct = dec_1["comision"]
        neto = (monto * (Decimal("1") - comision_pct)).quantize(CENT, rounding=ROUND_HALF_UP)
        mov = {
            "tipo": "ingreso", "envia": origen, "recibe": destino, "cuenta": cuenta or "Cuenta",
            "monto_original": monto, "monto_neto": neto, "comision": monto - neto, "monto_egreso": Decimal("0"),
            "nro": nro, "codigo": codigo, "detalle": detalle, "monto": monto, "delta": neto,
        }
    else:
        mov = {
            "tipo": "egreso", "envia": origen, "recibe": destino, "cuenta": cuenta or "",
            "monto_original": Decimal("0"), "monto_neto": Decimal("0"), "comision": Decimal("0"),
            "monto_egreso": monto, "nro": nro, "codigo": codigo, "detalle": detalle, "monto": monto,
            "delta": -monto,
        }

    resultado = registrar_movimiento(mov)
    if resultado["duplicado"]:
        return (
            "<b>DUPLICADO - Transferencia ya registrada</b>\n"
            "Envía: " + esc(origen or "Desconocido") + "\n"
            "Monto: " + fmt_pesos(monto) + "\n"
            "Nro comprobante: " + esc(nro_mostrar or "no encontrado") + "\n"
            "Registrada el: " + esc(resultado["duplicado"]) + "\n"
            "<b>Saldo sin cambios: " + fmt_pesos(resultado["saldo"]) + "</b>"
        )

    saldo = resultado["saldo"]
    if tipo == "ingreso":
        return (
            "<b>INGRESO - " + esc(cuenta or "Cuenta") + "</b>\n"
            "De: " + esc(origen or "Desconocido") + "\n"
            "Para: " + esc(destino or "Desconocido") + "\n"
            "Monto recibido: " + fmt_pesos(monto) + "\n"
            "Monto neto: " + fmt_pesos(mov["monto_neto"]) + "\n"
            "Nro comprobante: " + esc(nro_mostrar or "no encontrado") + "\n"
            "✅ Monto verificado\n"
            "<b>Saldo actual: " + fmt_pesos(saldo) + "</b>"
        )
    return (
        "<b>EGRESO</b>\n"
        "De: " + esc(origen or "Desconocido") + "\n"
        "Para: " + esc(destino or "Desconocido") + "\n"
        "Monto enviado: " + fmt_pesos(monto) + "\n"
        "Nro comprobante: " + esc(nro_mostrar or "no encontrado") + "\n"
        "✅ Monto verificado\n"
        "<b>Saldo actual: " + fmt_pesos(saldo) + "</b>"
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
async def responder(update: Update, texto):
    msg = update.effective_message
    plano = html.unescape(re.sub(r"<[^>]+>", "", texto))
    for intento in range(3):
        try:
            await msg.reply_text(texto, parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:
            logger.warning("Telegram rechazo el mensaje con formato (%s), se envia sin formato", e)
            try:
                await msg.get_bot().send_message(chat_id=msg.chat_id, text=plano)
                return
            except Exception as e2:
                logger.warning("No se pudo enviar sin formato: %s", e2)
        except Exception as e:
            logger.warning("No se pudo enviar la respuesta (intento %d): %s", intento + 1, e)
        await asyncio.sleep(2 * (intento + 1))
    logger.error("No se pudo enviar la respuesta final: %s", plano)


async def procesar_archivo(update: Update, data):
    media_type = detectar_media_type(data)
    if media_type is None:
        await responder(update, "⚠️ Formato no soportado. Mandá el comprobante como foto, imagen o PDF.\n"
                                "<b>No se registró nada.</b>")
        return
    await responder(update, "Procesando...")
    try:
        lectura_1, lectura_2 = await asyncio.gather(
            asyncio.to_thread(leer_con_modelo, MODELO_LECTURA, PROMPT_LECTURA, data, media_type),
            asyncio.to_thread(leer_con_modelo, MODELO_VERIFICACION, PROMPT_VERIFICACION, data, media_type),
        )
    except Exception:
        logger.exception("Error leyendo el comprobante")
        await responder(update, MSG_ERROR)
        return
    try:
        texto = await asyncio.to_thread(evaluar_y_registrar, lectura_1, lectura_2)
    except Exception:
        logger.exception("Error registrando el comprobante")
        await responder(update, MSG_ERROR)
        return
    await responder(update, texto)


async def handle_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        foto = update.effective_message.photo[-1]
        archivo = await foto.get_file()
        data = bytes(await archivo.download_as_bytearray())
    except Exception:
        logger.exception("Error descargando la foto")
        await responder(update, MSG_ERROR)
        return
    await procesar_archivo(update, data)


async def handle_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.effective_message.document
        archivo = await doc.get_file()
        data = bytes(await archivo.download_as_bytearray())
    except Exception:
        logger.exception("Error descargando el documento")
        await responder(update, MSG_ERROR)
        return
    await procesar_archivo(update, data)


async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hoy = ahora_ar().strftime("%Y-%m-%d")
        saldo = await asyncio.to_thread(leer_saldo)
        filas = await asyncio.to_thread(movimientos_del_dia, hoy)
        if not filas:
            await responder(update, "No hay movimientos hoy.\n<b>Saldo actual: " + fmt_pesos(saldo) + "</b>")
            return
        lineas = ["<b>Resumen " + hoy + "</b>", ""]
        for i, f in enumerate(filas, 1):
            tipo, envia, recibe, neto, egreso, hora = f
            if tipo == "ingreso":
                lineas.append(str(i) + ". INGRESO de " + esc(envia) + " - Neto: " + fmt_pesos(neto or 0) + " (" + esc(hora) + ")")
            else:
                lineas.append(str(i) + ". EGRESO a " + esc(recibe) + " - " + fmt_pesos(egreso or 0) + " (" + esc(hora) + ")")
        lineas.append("")
        lineas.append("<b>Saldo actual: " + fmt_pesos(saldo) + "</b>")
        await responder(update, "\n".join(lineas))
    except Exception:
        logger.exception("Error en /resumen")
        await responder(update, "❌ No se pudo generar el resumen. Probá de nuevo en unos segundos.")


async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        saldo = await asyncio.to_thread(leer_saldo)
        filas = await asyncio.to_thread(historial, 7)
        if not filas:
            await responder(update, "No hay historial.\n<b>Saldo actual: " + fmt_pesos(saldo) + "</b>")
            return
        lineas = ["<b>Historial últimos 7 días</b>", ""]
        for fecha, ingresos, egresos in filas:
            lineas.append(esc(fecha) + ": +" + fmt_pesos(ingresos or 0) + " / -" + fmt_pesos(egresos or 0))
        lineas.append("")
        lineas.append("<b>Saldo actual: " + fmt_pesos(saldo) + "</b>")
        await responder(update, "\n".join(lineas))
    except Exception:
        logger.exception("Error en /historial")
        await responder(update, "❌ No se pudo generar el historial. Probá de nuevo en unos segundos.")


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        saldo = await asyncio.to_thread(leer_saldo)
        await responder(update, "<b>Saldo actual: " + fmt_pesos(saldo) + "</b>")
    except Exception:
        logger.exception("Error en /saldo")
        await responder(update, "❌ No se pudo leer el saldo. Probá de nuevo en unos segundos.")


async def cmd_resetear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(fijar_saldo, Decimal("0"))
        await responder(update, "Saldo reseteado.\n<b>Saldo actual: $0,00</b>")
    except Exception:
        logger.exception("Error en /resetear")
        await responder(update, "❌ No se pudo resetear el saldo. Probá de nuevo.")


async def cmd_setear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = "".join(context.args or []).strip()
    negativo = texto.startswith("-")
    texto = texto.lstrip("-+")
    if re.fullmatch(r"0+(?:[.,]0+)?", texto or "x"):
        valor = Decimal("0.00")
    else:
        valor = parsear_monto(texto)
    if valor is None:
        await responder(update, "Uso correcto: /setear 500000  (también sirve /setear 500.000)")
        return
    if negativo:
        valor = -valor
    try:
        await asyncio.to_thread(fijar_saldo, valor)
        await responder(update, "Saldo establecido.\n<b>Saldo actual: " + fmt_pesos(valor) + "</b>")
    except Exception:
        logger.exception("Error en /setear")
        await responder(update, "❌ No se pudo establecer el saldo. Probá de nuevo.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        saldo = await asyncio.to_thread(leer_saldo)
    except Exception:
        saldo = None
    await responder(
        update,
        "<b>Bot de comprobantes activo</b>\n\n"
        "Mandá una foto o PDF de un comprobante y lo proceso automáticamente.\n"
        "Cada comprobante se lee dos veces por separado: si las lecturas no coinciden, no se registra.\n\n"
        "Comandos:\n"
        "/saldo - ver saldo actual\n"
        "/resumen - movimientos de hoy\n"
        "/historial - últimos 7 días\n"
        "/setear 500000 - establecer saldo\n"
        "/resetear - poner saldo en 0\n\n"
        + ("<b>Saldo actual: " + fmt_pesos(saldo) + "</b>" if saldo is not None else ""),
    )


def main():
    global MODELO_LECTURA, MODELO_VERIFICACION
    faltan = [n for n, v in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
                             ("DATABASE_URL", DATABASE_URL)) if not v]
    if faltan:
        raise SystemExit("Faltan variables de entorno: " + ", ".join(faltan))
    init_db()
    MODELO_LECTURA = elegir_modelo(MODELOS_LECTURA)
    MODELO_VERIFICACION = elegir_modelo([m for m in MODELOS_VERIFICACION if m != MODELO_LECTURA]
                                        or MODELOS_VERIFICACION)
    logger.info("Lectura: %s | Verificacion: %s", MODELO_LECTURA, MODELO_VERIFICACION)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("resetear", cmd_resetear))
    app.add_handler(CommandHandler("setear", cmd_setear))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.PHOTO, handle_foto))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.Document.ALL, handle_documento))
    logger.info("Bot iniciado!")
    # drop_pending_updates=False: si el bot se reinicia, los comprobantes mandados en ese momento
    # se procesan al volver (antes se perdian sin aviso). Los duplicados los frena el control de duplicados.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
