import os
import sqlite3
import json
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from uuid import uuid4
from typing import List, Optional, Dict
from fastapi import FastAPI, Header, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ------------------------------------------------------------------
# CONFIGURACIÓN DE VARIABLES DE ENTORNO Y GEMINI IA
# ------------------------------------------------------------------
load_dotenv()

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

# ------------------------------------------------------------------
# BASE DE DATOS SQLITE (PERSISTENTE)
# ------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "salud.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Tabla de pacientes en cola
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

    # Tabla de historial de atenciones
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

    if "prioridad" not in {col[1] for col in cursor.execute("PRAGMA table_info(cola_pacientes)")}:
        cursor.execute("""
            ALTER TABLE cola_pacientes ADD COLUMN prioridad INTEGER NOT NULL DEFAULT 4
            CHECK (prioridad BETWEEN 1 AND 4)
        """)
    if "receta" not in {col[1] for col in cursor.execute("PRAGMA table_info(historial_consultas)")}:
        cursor.execute("ALTER TABLE historial_consultas ADD COLUMN receta TEXT NOT NULL DEFAULT ''")

    for tabla in ("cola_pacientes", "historial_consultas"):
        if "acceso_resumen" not in {col[1] for col in cursor.execute(f"PRAGMA table_info({tabla})")}:
            cursor.execute(f"ALTER TABLE {tabla} ADD COLUMN acceso_resumen TEXT")
        cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{tabla}_resumen ON {tabla}(acceso_resumen)")

    # Sembrar la demostración en una base sin actividad.
    cursor.execute("""
        SELECT NOT EXISTS(SELECT 1 FROM cola_pacientes)
           AND NOT EXISTS(SELECT 1 FROM historial_consultas)
    """)
    if cursor.fetchone()[0]:
        cursor.execute("""
            INSERT INTO cola_pacientes (id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "AT-9015",
            "09:15",
            "18.234.567-8",
            "Carlos Alberto Soto",
            "34 años / Masculino",
            json.dumps(["Dolor intenso de garganta", "Fiebre moderada (38.2°C)", "Decaimiento general"]),
            json.dumps({"pa": "120/80 mmHg", "fc": "84 lpm", "temp": "38.2 °C", "sat": "98%"}),
            "Faringitis Aguda Bacteriana"
        ))

    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------------
# INICIALIZACIÓN DE FASTAPI
# ------------------------------------------------------------------
app = FastAPI(title="Sistema de Triage con IA y Telemedicina Médica")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------
# GERENTE DE CONEXIÓN WEBSOCKET Y SEÑALIZACIÓN WEBRTC
# ------------------------------------------------------------------
class ConnectionManager:
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
        for m in list(self.totem_messages):
            try:
                await websocket.send_text(m)
            except Exception:
                break

    def disconnect_totem(self):
        self.totem_ws = None
        self.totem_messages = []

    def disconnect_doctor(self):
        self.doctor_ws = None

    async def relay_from_totem(self, data: str):
        self.totem_messages.append(data)
        if self.doctor_ws:
            try:
                await self.doctor_ws.send_text(data)
            except Exception:
                self.doctor_ws = None

    async def relay_from_doctor(self, data: str):
        if self.totem_ws:
            try:
                await self.totem_ws.send_text(data)
            except Exception:
                self.totem_ws = None

manager = ConnectionManager()

# ------------------------------------------------------------------
# FUNCIONES AUXILIARES Y MODELOS PYDANTIC
# ------------------------------------------------------------------
def validar_rut_chileno(rut: str) -> bool:
    rut_limpio = rut.replace(".", "").replace("-", "").upper().strip()
    if not 2 <= len(rut_limpio) <= 9:
        return False
    cuerpo, dv = rut_limpio[:-1], rut_limpio[-1]
    if not cuerpo.isascii() or not cuerpo.isdecimal():
        return False
    suma = 0
    multiplicador = 2
    for c in reversed(cuerpo):
        suma += int(c) * multiplicador
        multiplicador = 2 if multiplicador == 7 else multiplicador + 1
    resto = suma % 11
    dv_esperado = 11 - resto
    dv_calc = '0' if dv_esperado == 11 else ('K' if dv_esperado == 10 else str(dv_esperado))
    return dv == dv_calc

class PacienteReq(BaseModel):
    rut: str

class ChatReq(BaseModel):
    rut: str
    mensaje: str
    paso: int
    historial: Optional[List[Dict]] = None
    acceso_resumen: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class PrioridadReq(BaseModel):
    prioridad: int = Field(ge=1, le=4)


class CierreConsultaReq(BaseModel):
    diagnostico: str = Field(default="", max_length=1000)
    receta: str = Field(default="", max_length=4000)

# ------------------------------------------------------------------
# ENDPOINTS REST DE LA API CON SQLITE
# ------------------------------------------------------------------
@app.get("/api/pacientes")
@app.get("/api/v1/cola-pacientes")
def obtener_cola():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico, prioridad
        FROM cola_pacientes ORDER BY prioridad, rowid
    """)
    rows = cursor.fetchall()
    conn.close()

    resultado = []
    for r in rows:
        resultado.append({
            "id": r[0],
            "hora": r[1],
            "rut": r[2],
            "nombre": r[3],
            "edad": r[4],
            "sintomas": json.loads(r[5]) if r[5] else [],
            "instrumentos": json.loads(r[6]) if r[6] else {},
            "prediagnostico": r[7],
            "prioridad": r[8]
        })
    return resultado

@app.get("/api/v1/historial")
def obtener_historial():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, fecha, rut, diagnostico, estado, receta
        FROM historial_consultas ORDER BY rowid DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    resultado = []
    for r in rows:
        resultado.append({
            "id": r[0],
            "fecha": r[1],
            "rut": r[2],
            "diagnostico": r[3],
            "estado": r[4],
            "receta": r[5]
        })
    return resultado

@app.patch("/api/v1/cola-pacientes/{paciente_id}/prioridad")
def actualizar_prioridad(paciente_id: str, datos: PrioridadReq):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        actualizado = conn.execute(
            "UPDATE cola_pacientes SET prioridad = ? WHERE id = ?",
            (datos.prioridad, paciente_id),
        )
        if not actualizado.rowcount:
            raise HTTPException(status_code=404, detail="El paciente ya no está en la cola.")
    return {"prioridad": datos.prioridad}


@app.post("/api/v1/cola-pacientes/{paciente_id}/finalizar")
def finalizar_atencion(paciente_id: str, datos: CierreConsultaReq):
    diagnostico, receta = datos.diagnostico.strip(), datos.receta.strip()
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
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
    return {"id": paciente_id, "estado": "Finalizada"}


@app.get("/api/v1/resumen-atencion")
def resumen_atencion(response: Response, authorization: str = Header(default="")):
    prefijo, _, acceso = authorization.partition(" ")
    if prefijo.lower() != "bearer" or len(acceso) != 64:
        raise HTTPException(status_code=401, detail="Falta el acceso a la atención.")

    acceso_hash = sha256(acceso.encode()).hexdigest()
    response.headers["Cache-Control"] = "no-store"
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("BEGIN")
        registro = conn.execute("""
            SELECT id, fecha, diagnostico, receta FROM historial_consultas WHERE acceso_resumen = ?
        """, (acceso_hash,)).fetchone()
        if registro:
            return dict(zip(("id", "fecha", "diagnostico", "receta"), registro))

        pendiente = conn.execute(
            "SELECT 1 FROM cola_pacientes WHERE acceso_resumen = ?", (acceso_hash,)
        ).fetchone()
        if pendiente:
            response.status_code = 202
            return {"estado": "Pendiente"}
    raise HTTPException(status_code=404, detail="La atención no está disponible.")


@app.post("/api/v1/iniciar-consulta")
def iniciar_consulta(paciente: PacienteReq):
    if not validar_rut_chileno(paciente.rut):
        raise HTTPException(status_code=400, detail="RUT inválido.")
    return {"status": "success", "rut": paciente.rut}


@app.post("/api/v1/chat-evaluacion")
async def procesar_chat(chat: ChatReq):
    if not validar_rut_chileno(chat.rut):
        raise HTTPException(status_code=400, detail="RUT inválido.")

    mensaje = chat.mensaje.strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="Ingrese un síntoma para continuar.")
    messages = []
    for item in chat.historial or []:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": "user" if role == "user" else "model", "parts": [{"text": content.strip()}]})
    actual = {"role": "user", "parts": [{"text": mensaje}]}
    if not messages or messages[-1] != actual:
        messages.append(actual)

    if chat.paso >= 2:
        rut = chat.rut.replace(".", "").replace("-", "").strip().upper()
        cuerpo, digito = rut[:-1], rut[-1]
        rut_formateado = f"{int(cuerpo):,}".replace(",", ".") + f"-{digito}"
        sintomas = list(dict.fromkeys(
            item["parts"][0]["text"] for item in messages if item["role"] == "user"
        ))
        acceso_hash = sha256(chat.acceso_resumen.encode()).hexdigest() if chat.acceso_resumen else None
        with closing(sqlite3.connect(DB_PATH)) as conn, conn:
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
                        (id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico, acceso_resumen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rut) DO UPDATE SET sintomas = excluded.sintomas
                    WHERE cola_pacientes.acceso_resumen IS excluded.acceso_resumen
                """, (
                    f"AT-{uuid4().hex[:12]}", datetime.now().strftime("%H:%M"),
                    rut_formateado, "Paciente Tótem", "Sin informar",
                    json.dumps(sintomas), "{}", "Pendiente de revisión profesional", acceso_hash,
                ))
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="El acceso corresponde a otra atención.")
            if cambio.rowcount == 0:
                raise HTTPException(status_code=409, detail="Ya hay una atención en curso para este RUT.")

        return {
            "respuesta_ia": "Registré sus síntomas. Ahora lo conectaremos con el profesional de turno.",
            "siguiente_accion": "DERIVAR_TELEMEDICINA"
        }

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

# ------------------------------------------------------------------
# CANALES WEBSOCKET PARA WEBRTC
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
# RUTAS PARA SERVIR LAS VISTAS HTML
# ------------------------------------------------------------------
@app.get("/doctor", response_class=HTMLResponse)
def get_doctor_page():
    path = os.path.join(BASE_DIR, "templates", "doctor.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Error: No se encontró templates/doctor.html</h1>"

@app.get("/", response_class=HTMLResponse)
def get_index_page():
    path = os.path.join(BASE_DIR, "templates", "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Error: No se encontró templates/index.html</h1>"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)