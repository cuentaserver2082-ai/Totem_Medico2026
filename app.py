"""
Tótem Médico Inteligente - Backend (FastAPI + SQLite + WebSocket/WebRTC + Gemini)

Estructura del archivo (búscalas por los encabezados):
  1. Configuración y variables de entorno
  2. Base de datos SQLite (tablas, migraciones y datos de demostración)
  3. Inicialización de FastAPI (archivos estáticos y cabeceras de seguridad)
  4. Gestor WebSocket para la señalización WebRTC
  5. Funciones auxiliares y modelos Pydantic
  6. Autenticación del médico (login / sesión por cookie)
  7. API del paciente (tótem): RUT, chat, abandono de cola
  8. API del médico (protegida): cola, historial, prioridad, finalizar
  9. Canales WebSocket
 10. Rutas de las vistas HTML

Lógica separada en módulos (para ampliar sin tocar este archivo):
  servicios/filtro_texto.py   -> filtro de groserías / spam del chatbot
  servicios/triage.py         -> auto-triage (reglas clínicas + Gemini)
  servicios/autenticacion.py  -> hash de claves, sesiones, límite de intentos
"""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from typing import Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from servicios import autenticacion, triage
from servicios.filtro_texto import limpiar_texto, validar_mensaje

# ------------------------------------------------------------------
# 1. CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ------------------------------------------------------------------
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DB_PATH = os.path.join(BASE_DIR, "salud.db")

# --- Gemini ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
gemini_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=8000, retry_options=types.HttpRetryOptions(attempts=1)),
) if GEMINI_API_KEY else None

SYSTEM_PROMPT = """
Eres un asistente médico virtual de triage para un tótem de atención en salud en Chile.
Tu objetivo es evaluar brevemente los síntomas del paciente antes de derivarlo al médico.
Instrucciones:
1. Responde de forma empática, clara, formal y breve (máximo 2 oraciones por respuesta).
2. Haz una pregunta sobre el malestar principal. Limítate a recopilar información, sin diagnosticar, indicar fármacos ni asignar prioridad clínica.
3. Si el paciente menciona una urgencia vital (dolor opresivo al pecho, pérdida de conciencia, dificultad respiratoria severa o sangrado abundante), indica que se requiere atención presencial urgente.
"""

# --- Acceso del médico (definir en .env; ver .env.example) ---
DOCTOR_EMAIL = os.getenv("DOCTOR_EMAIL", "marcos.gonzalez@salud.cl").strip().lower()
DOCTOR_PASSWORD = os.getenv("DOCTOR_PASSWORD")          # si falta, se usa una clave de DEMO (ver más abajo)
DOCTOR_NOMBRE = os.getenv("DOCTOR_NOMBRE", "Dr. Marcos González")
CLAVE_DEMO = "Totem2026!"
# --- Modo demostración (ajustar en .env) ---
# MODO_DEMO=1  -> el login del médico muestra un botón "Entrar como médico de demostración" (sin escribir clave).
# RUT_ESTRICTO=0 -> el tótem acepta cualquier RUT con formato válido, aunque sea inventado.
# Para uso real poner MODO_DEMO=0 y RUT_ESTRICTO=1.
MODO_DEMO = os.getenv("MODO_DEMO", "1") == "1"
RUT_ESTRICTO = os.getenv("RUT_ESTRICTO", "0") == "1"
COOKIE_SESION = "sesion_medico"
COOKIE_SEGURA = os.getenv("COOKIE_SECURE", "0") == "1"   # poner COOKIE_SECURE=1 cuando se sirva por HTTPS

limitador_login = autenticacion.LimitadorIntentos()
_CLAVE_FALSA = autenticacion.hashear_clave("clave-inexistente")  # iguala tiempos si el correo no existe

# ------------------------------------------------------------------
# 2. BASE DE DATOS SQLITE (PERSISTENTE)
# ------------------------------------------------------------------
def _columnas(cursor, tabla: str) -> set:
    return {col[1] for col in cursor.execute(f"PRAGMA table_info({tabla})")}


def init_db():
    """Crea las tablas si no existen y aplica migraciones sobre bases antiguas."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Pacientes en cola. `prioridad`: 1 Emergencia, 2 Urgente, 3 Moderada, 4 Leve.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cola_pacientes (
            id TEXT PRIMARY KEY,
            hora TEXT,
            rut TEXT UNIQUE,
            nombre TEXT,
            edad TEXT,
            sintomas TEXT,
            instrumentos TEXT,
            prediagnostico TEXT,
            prioridad INTEGER NOT NULL DEFAULT 4 CHECK (prioridad BETWEEN 1 AND 4)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_consultas (
            id TEXT PRIMARY KEY,
            fecha TEXT,
            rut TEXT,
            diagnostico TEXT,
            estado TEXT,
            receta TEXT NOT NULL DEFAULT ''
        )
    """)

    # Médicos autorizados y sus sesiones activas.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS medicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            nombre TEXT NOT NULL,
            especialidad TEXT NOT NULL DEFAULT 'Medicina General',
            registro TEXT NOT NULL DEFAULT '',
            clave_hash TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sesiones_medico (
            token_hash TEXT PRIMARY KEY,
            medico_id INTEGER NOT NULL REFERENCES medicos(id),
            expira TEXT NOT NULL
        )
    """)

    # --- Migraciones para bases creadas con versiones anteriores ---
    if "prioridad" not in _columnas(cursor, "cola_pacientes"):
        cursor.execute("""
            ALTER TABLE cola_pacientes ADD COLUMN prioridad INTEGER NOT NULL DEFAULT 4
            CHECK (prioridad BETWEEN 1 AND 4)
        """)
    if "prioridad_origen" not in _columnas(cursor, "cola_pacientes"):
        # 'auto' = asignada por el auto-triage, 'manual' = cambiada por el médico
        cursor.execute("ALTER TABLE cola_pacientes ADD COLUMN prioridad_origen TEXT NOT NULL DEFAULT 'auto'")
    if "receta" not in _columnas(cursor, "historial_consultas"):
        cursor.execute("ALTER TABLE historial_consultas ADD COLUMN receta TEXT NOT NULL DEFAULT ''")
    for tabla in ("cola_pacientes", "historial_consultas"):
        if "acceso_resumen" not in _columnas(cursor, tabla):
            cursor.execute(f"ALTER TABLE {tabla} ADD COLUMN acceso_resumen TEXT")
        cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{tabla}_resumen ON {tabla}(acceso_resumen)")

    # --- Paciente de demostración (solo en una base totalmente vacía) ---
    cursor.execute("""
        SELECT NOT EXISTS(SELECT 1 FROM cola_pacientes)
           AND NOT EXISTS(SELECT 1 FROM historial_consultas)
    """)
    if cursor.fetchone()[0]:
        cursor.execute("""
            INSERT INTO cola_pacientes
                (id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico, prioridad)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "AT-9015", "09:15", "18.234.567-8", "Carlos Alberto Soto", "34 años / Masculino",
            json.dumps(["Dolor intenso de garganta", "Fiebre moderada (38.2°C)", "Decaimiento general"]),
            json.dumps({"pa": "120/80 mmHg", "fc": "84 lpm", "temp": "38.2 °C", "sat": "98%", "temp_c": 38.2,
                        "spo2": 98, "fc_lpm": 84, "pa_sis": 120, "pa_dia": 80, "simulado": True}),
            "Triage automático: Moderada. Se detectó: fiebre 38.2 °C.", 3,
        ))

    sembrar_medico(cursor)
    conn.commit()
    conn.close()


def sembrar_medico(cursor):
    """
    Crea/actualiza el médico de acceso a partir del .env.
    Si DOCTOR_PASSWORD no está definido se usa una clave de DEMO y se advierte en consola.
    """
    clave = DOCTOR_PASSWORD or CLAVE_DEMO
    if not DOCTOR_PASSWORD:
        print(f"⚠️  DOCTOR_PASSWORD no está definido en .env: usando clave de DEMO ({CLAVE_DEMO}). "
              "Defínala antes de usar el sistema fuera de pruebas.")
    existe = cursor.execute("SELECT id, clave_hash FROM medicos WHERE email = ?", (DOCTOR_EMAIL,)).fetchone()
    if existe is None:
        cursor.execute(
            "INSERT INTO medicos (email, nombre, especialidad, registro, clave_hash) VALUES (?, ?, ?, ?, ?)",
            (DOCTOR_EMAIL, DOCTOR_NOMBRE, "Medicina General", "489201", autenticacion.hashear_clave(clave)),
        )
    else:
        # El .env manda: si cambió la clave o el nombre, se actualiza al arrancar.
        nuevo_hash = existe[1] if autenticacion.verificar_clave(clave, existe[1]) else autenticacion.hashear_clave(clave)
        cursor.execute("UPDATE medicos SET nombre = ?, clave_hash = ? WHERE id = ?", (DOCTOR_NOMBRE, nuevo_hash, existe[0]))


init_db()
if MODO_DEMO:
    print("ℹ️  MODO_DEMO activo: el portal médico permite entrar sin clave. Ponga MODO_DEMO=0 para uso real.")
if not RUT_ESTRICTO:
    print("ℹ️  RUT_ESTRICTO=0: el tótem acepta cualquier RUT con formato válido (demo).")

# ------------------------------------------------------------------
# 3. INICIALIZACIÓN DE FASTAPI
# ------------------------------------------------------------------
app = FastAPI(title="Sistema de Triage con IA y Telemedicina Médica")

os.makedirs(os.path.join(STATIC_DIR, "img"), exist_ok=True)
# Imágenes y recursos del tótem (p. ej. /static/img/image_4135df.jpg)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    """Evita que el navegador guarde datos de pacientes y limita permisos del navegador."""
    respuesta = await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path in ("/", "/doctor"):
        respuesta.headers["Cache-Control"] = "no-store"
    respuesta.headers.setdefault("X-Content-Type-Options", "nosniff")
    respuesta.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self)")
    return respuesta

# ------------------------------------------------------------------
# 4. GESTOR DE CONEXIÓN WEBSOCKET Y SEÑALIZACIÓN WEBRTC
# ------------------------------------------------------------------
class ConnectionManager:
    """Un tótem y un médico a la vez (prototipo). Reenvía los mensajes de señalización."""

    def __init__(self):
        self.totem_ws: Optional[WebSocket] = None
        self.doctor_ws: Optional[WebSocket] = None
        self.totem_messages: List[str] = []

    async def connect_totem(self, websocket: WebSocket):
        await websocket.accept()
        self.totem_ws = websocket
        self.totem_messages = []

    async def connect_doctor(self, websocket: WebSocket):
        await websocket.accept()
        self.doctor_ws = websocket
        # Reenvía la oferta WebRTC que el tótem dejó esperando.
        for m in list(self.totem_messages):
            try:
                await websocket.send_text(m)
            except Exception:
                break
        # Avisa al tótem que el médico ya inició la llamada (detiene su cuenta regresiva de espera).
        if self.totem_ws:
            try:
                await self.totem_ws.send_text(json.dumps({"type": "doctor_connected"}))
            except Exception:
                self.totem_ws = None

    def disconnect_totem(self):
        self.totem_ws = None
        self.totem_messages = []

    def disconnect_doctor(self):
        self.doctor_ws = None

    # --- Chat escrito médico <-> paciente (mensajes tipados {"tipo":"chat",...}) ---
    TIPOS_SERVIDOR = ("chat",)
    MAX_CHAT = 500

    @classmethod
    def _normalizar_chat(cls, data: str, emisor: str) -> Optional[str]:
        """Si `data` es un mensaje de chat válido devuelve el JSON saneado (el emisor lo fija
        el servidor según el canal, no el cliente); si no es chat devuelve None."""
        try:
            msg = json.loads(data)
        except (TypeError, ValueError):
            return None
        if not isinstance(msg, dict) or msg.get("tipo") not in cls.TIPOS_SERVIDOR:
            return None
        texto = limpiar_texto(str(msg.get("texto", "")))[: cls.MAX_CHAT]
        if not texto:
            return ""          # chat vacío: se descarta
        return json.dumps({"tipo": "chat", "emisor": emisor, "texto": texto}, ensure_ascii=False)

    async def relay_from_totem(self, data: str):
        chat = self._normalizar_chat(data, "paciente")
        if chat == "":
            return
        if chat is None:
            # Solo la señalización WebRTC se guarda para reenviarla si el médico llega después.
            self.totem_messages.append(data)
            payload = data
        else:
            payload = chat      # el chat NO se almacena: no queda texto médico en memoria
        if self.doctor_ws:
            try:
                await self.doctor_ws.send_text(payload)
            except Exception:
                self.doctor_ws = None

    async def relay_from_doctor(self, data: str):
        chat = self._normalizar_chat(data, "doctor")
        if chat == "":
            return
        payload = data if chat is None else chat
        if self.totem_ws:
            try:
                await self.totem_ws.send_text(payload)
            except Exception:
                self.totem_ws = None

    async def avisar_totem(self, mensaje: dict):
        """Empuja un aviso al tótem (p. ej. 'resumen_listo' cuando el médico guarda el diagnóstico)."""
        if self.totem_ws:
            try:
                await self.totem_ws.send_text(json.dumps(mensaje))
            except Exception:
                self.totem_ws = None


manager = ConnectionManager()

# ------------------------------------------------------------------
# 5. FUNCIONES AUXILIARES Y MODELOS PYDANTIC
# ------------------------------------------------------------------
def validar_rut_chileno(rut: str) -> bool:
    rut_limpio = rut.replace(".", "").replace("-", "").upper().strip()
    if not 2 <= len(rut_limpio) <= 9:
        return False
    cuerpo, dv = rut_limpio[:-1], rut_limpio[-1]
    if not cuerpo.isascii() or not cuerpo.isdecimal():
        return False
    if not (dv.isascii() and (dv.isdecimal() or dv == "K")):
        return False
    if not RUT_ESTRICTO:      # demo: basta con el formato (7-8 dígitos + dígito verificador)
        return True
    suma = 0
    multiplicador = 2
    for c in reversed(cuerpo):
        suma += int(c) * multiplicador
        multiplicador = 2 if multiplicador == 7 else multiplicador + 1
    resto = suma % 11
    dv_esperado = 11 - resto
    dv_calc = '0' if dv_esperado == 11 else ('K' if dv_esperado == 10 else str(dv_esperado))
    return dv == dv_calc


def formatear_rut(rut: str) -> str:
    """'182345678' -> '18.234.567-8'"""
    limpio = rut.replace(".", "").replace("-", "").strip().upper()
    return f"{int(limpio[:-1]):,}".replace(",", ".") + f"-{limpio[-1]}"


def abrir_db():
    return closing(sqlite3.connect(DB_PATH))


class PacienteReq(BaseModel):
    rut: str


class SignosVitalesReq(BaseModel):
    """Signos vitales estimados por el escaneo del tótem (hoy SIMULADOS). Los rangos evitan datos absurdos."""
    temp_c: float = Field(ge=34, le=42)
    spo2: int = Field(ge=70, le=100)
    fc_lpm: int = Field(ge=30, le=220)
    pa_sis: int = Field(ge=60, le=260)
    pa_dia: int = Field(ge=30, le=160)
    simulado: bool = True


class ChatReq(BaseModel):
    rut: str
    mensaje: str = Field(max_length=2000)
    paso: int
    historial: Optional[List[Dict]] = None
    acceso_resumen: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    signos_vitales: Optional[SignosVitalesReq] = None


class AbandonoReq(BaseModel):
    rut: str
    acceso_resumen: str = Field(pattern=r"^[a-f0-9]{64}$")
    motivo: str = Field(default="tiempo_espera", pattern=r"^(tiempo_espera|cancelado)$")


class PrioridadReq(BaseModel):
    prioridad: int = Field(ge=1, le=4)


class CierreConsultaReq(BaseModel):
    diagnostico: str = Field(default="", max_length=1000)
    receta: str = Field(default="", max_length=4000)


class LoginReq(BaseModel):
    email: str = Field(max_length=200)
    clave: str = Field(max_length=200)


def formatear_instrumentos(sv: Optional[SignosVitalesReq]) -> dict:
    """Guarda los signos vitales en el JSON `instrumentos` (textos para el médico + números para reglas)."""
    if sv is None:
        return {}
    return {
        "pa": f"{sv.pa_sis}/{sv.pa_dia} mmHg",
        "fc": f"{sv.fc_lpm} lpm",
        "temp": f"{sv.temp_c:.1f} °C",
        "sat": f"{sv.spo2}%",
        "temp_c": round(sv.temp_c, 1), "spo2": sv.spo2, "fc_lpm": sv.fc_lpm,
        "pa_sis": sv.pa_sis, "pa_dia": sv.pa_dia,
        "simulado": sv.simulado,
    }

# ------------------------------------------------------------------
# 6. AUTENTICACIÓN DEL MÉDICO
# ------------------------------------------------------------------
def medico_actual(request: Request) -> dict:
    """Dependencia: exige una sesión de médico válida (cookie HttpOnly)."""
    with abrir_db() as conn:
        medico = autenticacion.obtener_medico_por_token(conn, request.cookies.get(COOKIE_SESION))
    if not medico:
        raise HTTPException(status_code=401, detail="Sesión no válida o expirada.")
    return medico


@app.post("/api/v1/auth/login")
def login(datos: LoginReq, request: Request, response: Response):
    email = datos.email.strip().lower()
    clave_limite = f"{request.client.host if request.client else '?'}|{email}"
    if limitador_login.bloqueado(clave_limite):
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espere unos minutos e inténtelo de nuevo.")

    with abrir_db() as conn, conn:
        fila = conn.execute("SELECT id, clave_hash FROM medicos WHERE email = ?", (email,)).fetchone()
        valida = autenticacion.verificar_clave(datos.clave, fila[1] if fila else _CLAVE_FALSA)
        if not (fila and valida):
            limitador_login.registrar_fallo(clave_limite)
            raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
        token = autenticacion.crear_sesion(conn, fila[0])
        medico = autenticacion.obtener_medico_por_token(conn, token)

    limitador_login.limpiar(clave_limite)
    _poner_cookie_sesion(response, token, request)
    return medico


def _poner_cookie_sesion(response: Response, token: str, request: Request):
    # La cookie "Secure" solo se usa si la página realmente va por HTTPS; con http:// el navegador la
    # descartaría y el login "funcionaría" pero la sesión nunca quedaría guardada.
    por_https = request.url.scheme == "https"
    response.set_cookie(
        COOKIE_SESION, token, httponly=True, samesite="strict", secure=COOKIE_SEGURA and por_https,
        max_age=autenticacion.DURACION_SESION_HORAS * 3600, path="/",
    )


@app.get("/api/v1/auth/config")
def config_login():
    """Le dice a la pantalla de login si debe mostrar el botón de acceso de demostración."""
    return {"modo_demo": MODO_DEMO}


@app.post("/api/v1/auth/login-demo")
def login_demo(request: Request, response: Response):
    """Entra directo como el médico de .env, sin clave. SOLO si MODO_DEMO=1."""
    if not MODO_DEMO:
        raise HTTPException(status_code=404, detail="No disponible.")
    with abrir_db() as conn, conn:
        fila = conn.execute("SELECT id FROM medicos WHERE email = ?", (DOCTOR_EMAIL,)).fetchone()
        token = autenticacion.crear_sesion(conn, fila[0])
        medico = autenticacion.obtener_medico_por_token(conn, token)
    _poner_cookie_sesion(response, token, request)
    return medico


@app.get("/api/v1/auth/sesion")
def sesion_actual(medico: dict = Depends(medico_actual)):
    return medico


@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response):
    with abrir_db() as conn, conn:
        autenticacion.cerrar_sesion(conn, request.cookies.get(COOKIE_SESION))
    response.delete_cookie(COOKIE_SESION, path="/")
    return {"ok": True}

# ------------------------------------------------------------------
# 7. API DEL PACIENTE (TÓTEM) - acceso libre, el paciente solo ingresa su RUT
# ------------------------------------------------------------------
@app.post("/api/v1/iniciar-consulta")
def iniciar_consulta(paciente: PacienteReq):
    if not validar_rut_chileno(paciente.rut):
        raise HTTPException(status_code=400, detail="RUT inválido.")
    return {"status": "success", "rut": paciente.rut}


@app.get("/api/v1/resumen-atencion")
def resumen_atencion(response: Response, authorization: str = Header(default="")):
    prefijo, _, acceso = authorization.partition(" ")
    if prefijo.lower() != "bearer" or len(acceso) != 64:
        raise HTTPException(status_code=401, detail="Falta el acceso a la atención.")

    acceso_hash = sha256(acceso.encode()).hexdigest()
    response.headers["Cache-Control"] = "no-store"
    with abrir_db() as conn, conn:
        conn.execute("BEGIN")
        registro = conn.execute("""
            SELECT id, fecha, diagnostico, receta, estado FROM historial_consultas WHERE acceso_resumen = ?
        """, (acceso_hash,)).fetchone()
        if registro:
            return dict(zip(("id", "fecha", "diagnostico", "receta", "estado"), registro))

        pendiente = conn.execute(
            "SELECT 1 FROM cola_pacientes WHERE acceso_resumen = ?", (acceso_hash,)
        ).fetchone()
        if pendiente:
            response.status_code = 202
            return {"estado": "Pendiente"}
    raise HTTPException(status_code=404, detail="La atención no está disponible.")


@app.post("/api/v1/chat-evaluacion")
async def procesar_chat(chat: ChatReq):
    if not validar_rut_chileno(chat.rut):
        raise HTTPException(status_code=400, detail="RUT inválido.")

    # --- Filtro de groserías / spam / vacío (el frontend ya lo aplica, aquí es la validación definitiva) ---
    filtro = validar_mensaje(chat.mensaje)
    if not filtro.valido:
        raise HTTPException(status_code=422, detail={"codigo": filtro.codigo, "mensaje": filtro.mensaje})
    mensaje = filtro.texto

    # Historial: solo roles válidos, texto limpio y máximo 20 turnos.
    messages = []
    for item in (chat.historial or [])[-20:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and limpiar_texto(content):
            messages.append({"role": "user" if role == "user" else "model", "parts": [{"text": limpiar_texto(content)}]})
    actual = {"role": "user", "parts": [{"text": mensaje}]}
    if not messages or messages[-1] != actual:
        messages.append(actual)

    # --- Paso final: registrar al paciente en la cola con su auto-triage ---
    if chat.paso >= 2:
        rut_formateado = formatear_rut(chat.rut)
        sintomas = list(dict.fromkeys(
            item["parts"][0]["text"] for item in messages if item["role"] == "user"
        ))
        instrumentos = formatear_instrumentos(chat.signos_vitales)
        # Auto-triage (reglas + Gemini). Se calcula ANTES de abrir la transacción para no bloquear la base.
        resultado = await triage.clasificar(sintomas, instrumentos or None, gemini_client, GEMINI_MODEL)
        acceso_hash = sha256(chat.acceso_resumen.encode()).hexdigest() if chat.acceso_resumen else None

        with abrir_db() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            archivada = conn.execute(
                "SELECT rut FROM historial_consultas WHERE acceso_resumen = ?", (acceso_hash,)
            ).fetchone()
            if archivada:
                if archivada[0] != rut_formateado:
                    raise HTTPException(status_code=409, detail="El acceso corresponde a otra atención.")
                return {
                    "respuesta_ia": "La atención ya está disponible.",
                    "siguiente_accion": "MOSTRAR_RESUMEN",
                }
            try:
                cambio = conn.execute("""
                    INSERT INTO cola_pacientes
                        (id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico,
                         prioridad, prioridad_origen, acceso_resumen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?)
                    ON CONFLICT(rut) DO UPDATE SET
                        sintomas = excluded.sintomas,
                        instrumentos = excluded.instrumentos,
                        prediagnostico = excluded.prediagnostico,
                        -- si el médico ya fijó la prioridad a mano, no se la pisa
                        prioridad = CASE WHEN cola_pacientes.prioridad_origen = 'manual'
                                         THEN cola_pacientes.prioridad ELSE excluded.prioridad END
                    WHERE cola_pacientes.acceso_resumen IS excluded.acceso_resumen
                """, (
                    f"AT-{uuid4().hex[:12]}", datetime.now().strftime("%H:%M"),
                    rut_formateado, "Paciente Tótem", "Sin informar",
                    json.dumps(sintomas), json.dumps(instrumentos), resultado.resumen(),
                    resultado.prioridad, acceso_hash,
                ))
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="El acceso corresponde a otra atención.")
            if cambio.rowcount == 0:
                raise HTTPException(status_code=409, detail="Ya hay una atención en curso para este RUT.")

        return {
            "respuesta_ia": "Registré sus síntomas. Ahora lo conectaremos con el profesional de turno.",
            "siguiente_accion": "DERIVAR_TELEMEDICINA",
            "prioridad": resultado.prioridad,
        }

    # --- Pasos intermedios: pregunta de seguimiento con Gemini (o pregunta local de respaldo) ---
    if gemini_client:
        try:
            response = await gemini_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=messages,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=512),
            )
            if response.text and response.text.strip():
                return {
                    "respuesta_ia": response.text.strip(),
                    "siguiente_accion": "CONTINUAR_CUESTIONARIO"
                }
        except Exception as exc:
            print(f"Gemini no disponible ({type(exc).__name__}); se usará el cuestionario local.")

    return {
        "respuesta_ia": "¿Hace cuánto comenzaron los síntomas y qué intensidad tienen?",
        "siguiente_accion": "CONTINUAR_CUESTIONARIO"
    }


@app.post("/api/v1/cola-pacientes/abandonar")
def abandonar_cola(datos: AbandonoReq):
    """
    El tótem llama a este endpoint cuando vence el tiempo máximo de espera (5 min) o el paciente
    cancela. Saca al paciente de la cola (para que no quede bloqueado) y deja registro en el historial.
    Requiere el código secreto `acceso_resumen` que solo conoce la pantalla del paciente.
    """
    if not validar_rut_chileno(datos.rut):
        raise HTTPException(status_code=400, detail="RUT inválido.")
    acceso_hash = sha256(datos.acceso_resumen.encode()).hexdigest()
    estado = ("Sin atención: tiempo de espera agotado" if datos.motivo == "tiempo_espera"
              else "Cancelada por el paciente")
    with abrir_db() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        fila = conn.execute(
            "SELECT id, rut FROM cola_pacientes WHERE acceso_resumen = ?", (acceso_hash,)
        ).fetchone()
        if not fila:   # ya fue atendido o ya se limpió: operación idempotente
            return {"estado": "sin_cambios"}
        conn.execute("""
            INSERT INTO historial_consultas (id, fecha, rut, diagnostico, estado, receta, acceso_resumen)
            VALUES (?, ?, ?, '', ?, '', ?)
        """, (fila[0], datetime.now().isoformat(timespec="seconds"), fila[1], estado, acceso_hash))
        conn.execute("DELETE FROM cola_pacientes WHERE id = ?", (fila[0],))
    return {"estado": "retirado"}

# ------------------------------------------------------------------
# 8. API DEL MÉDICO (todas exigen sesión iniciada)
# ------------------------------------------------------------------
@app.get("/api/pacientes", dependencies=[Depends(medico_actual)])
@app.get("/api/v1/cola-pacientes", dependencies=[Depends(medico_actual)])
def obtener_cola():
    with abrir_db() as conn:
        rows = conn.execute("""
            SELECT id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico, prioridad, prioridad_origen
            FROM cola_pacientes ORDER BY prioridad, rowid
        """).fetchall()
    return [{
        "id": r[0], "hora": r[1], "rut": r[2], "nombre": r[3], "edad": r[4],
        "sintomas": json.loads(r[5]) if r[5] else [],
        "instrumentos": json.loads(r[6]) if r[6] else {},
        "prediagnostico": r[7], "prioridad": r[8], "prioridad_origen": r[9],
    } for r in rows]


@app.get("/api/v1/historial", dependencies=[Depends(medico_actual)])
def obtener_historial():
    with abrir_db() as conn:
        rows = conn.execute("""
            SELECT id, fecha, rut, diagnostico, estado, receta
            FROM historial_consultas ORDER BY rowid DESC
        """).fetchall()
    return [dict(zip(("id", "fecha", "rut", "diagnostico", "estado", "receta"), r)) for r in rows]


@app.patch("/api/v1/cola-pacientes/{paciente_id}/prioridad", dependencies=[Depends(medico_actual)])
def actualizar_prioridad(paciente_id: str, datos: PrioridadReq):
    """El médico corrige la prioridad del auto-triage; queda marcada como 'manual'."""
    with abrir_db() as conn, conn:
        actualizado = conn.execute(
            "UPDATE cola_pacientes SET prioridad = ?, prioridad_origen = 'manual' WHERE id = ?",
            (datos.prioridad, paciente_id),
        )
        if not actualizado.rowcount:
            raise HTTPException(status_code=404, detail="El paciente ya no está en la cola.")
    return {"prioridad": datos.prioridad, "prioridad_origen": "manual"}


@app.post("/api/v1/cola-pacientes/{paciente_id}/finalizar", dependencies=[Depends(medico_actual)])
async def finalizar_atencion(paciente_id: str, datos: CierreConsultaReq):
    diagnostico, receta = datos.diagnostico.strip(), datos.receta.strip()
    with abrir_db() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        paciente = conn.execute(
            "SELECT rut, acceso_resumen FROM cola_pacientes WHERE id = ?", (paciente_id,)
        ).fetchone()
        if paciente:
            conn.execute("""
                INSERT INTO historial_consultas (id, fecha, rut, diagnostico, estado, receta, acceso_resumen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                paciente_id, datetime.now().isoformat(timespec="seconds"), paciente[0],
                diagnostico, "Finalizada", receta, paciente[1],
            ))
            conn.execute("DELETE FROM cola_pacientes WHERE id = ?", (paciente_id,))
        else:
            registro = conn.execute(
                "SELECT diagnostico, receta FROM historial_consultas WHERE id = ?", (paciente_id,)
            ).fetchone()
            if registro is None:
                raise HTTPException(status_code=404, detail="La atención ya no está disponible.")
            if registro != (diagnostico, receta):
                raise HTTPException(status_code=409, detail="La atención ya se guardó con otros datos.")
    # Avisa al tótem para que muestre sola la tarjeta de Diagnóstico y Receta (no viaja el contenido:
    # el tótem lo pide con su código secreto en /api/v1/resumen-atencion).
    await manager.avisar_totem({"tipo": "resumen_listo", "id": paciente_id})
    return {"id": paciente_id, "estado": "Finalizada"}

# ------------------------------------------------------------------
# 9. CANALES WEBSOCKET PARA WEBRTC
# ------------------------------------------------------------------
@app.websocket("/ws/telemedicina/totem")
async def websocket_totem(websocket: WebSocket):
    await manager.connect_totem(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.relay_from_totem(data)
    except WebSocketDisconnect:
        manager.disconnect_totem()
    except Exception:
        manager.disconnect_totem()


@app.websocket("/ws/telemedicina/doctor")
async def websocket_doctor(websocket: WebSocket):
    # Solo un médico con sesión válida puede unirse a la videollamada.
    with abrir_db() as conn:
        medico = autenticacion.obtener_medico_por_token(conn, websocket.cookies.get(COOKIE_SESION))
    if not medico:
        await websocket.close(code=4401)
        return
    await manager.connect_doctor(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.relay_from_doctor(data)
    except WebSocketDisconnect:
        manager.disconnect_doctor()
    except Exception:
        manager.disconnect_doctor()

# ------------------------------------------------------------------
# 10. RUTAS PARA SERVIR LAS VISTAS HTML
#     "/"       -> Tótem del paciente (modo kiosco, sin login)
#     "/doctor" -> Portal médico (el login está dentro de la página y la API lo exige)
# ------------------------------------------------------------------
def _servir_vista(nombre: str):
    ruta = os.path.join(TEMPLATES_DIR, nombre)
    if os.path.exists(ruta):
        return FileResponse(ruta, media_type="text/html")
    return HTMLResponse(f"<h1>Error: No se encontró templates/{nombre}</h1>", status_code=500)


@app.get("/doctor", response_class=HTMLResponse)
def get_doctor_page():
    return _servir_vista("doctor.html")


@app.get("/", response_class=HTMLResponse)
def get_index_page():
    return _servir_vista("index.html")


# --- App instalable (PWA) para la tablet en modo tótem ---
# El service worker debe servirse desde la raíz para controlar todo el sitio.
@app.get("/manifest.webmanifest", include_in_schema=False)
def get_manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def get_service_worker():
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
