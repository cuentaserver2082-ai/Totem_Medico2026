import os
import sqlite3
import json
from datetime import datetime
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import google.generativeai as genai
from dotenv import load_dotenv

# ------------------------------------------------------------------
# CONFIGURACIÓN DE VARIABLES DE ENTORNO Y GEMINI IA
# ------------------------------------------------------------------
load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
Eres un asistente médico virtual de triage para un tótem de atención en salud en Chile.
Tu objetivo es evaluar brevemente los síntomas del paciente antes de derivarlo al médico.
Instrucciones:
1. Responde de forma empática, clara, formal y breve (máximo 2 oraciones por respuesta).
2. Haz preguntas específicas sobre el malestar principal (duración, intensidad o síntomas asociados).
3. Si el paciente menciona una urgencia vital (dolor opresivo al pecho, pérdida de conciencia, dificultad respiratoria severa o sangrado abundante), indica que se requiere atención presencial urgente.
"""

model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT
)

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
            prediagnostico TEXT
        )
    """)

    # Tabla de historial de atenciones
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_consultas (
            id TEXT PRIMARY KEY,
            fecha TEXT,
            rut TEXT,
            diagnostico TEXT,
            estado TEXT
        )
    """)

    # Insertar paciente inicial de prueba si la tabla está vacía
    cursor.execute("SELECT COUNT(*) FROM cola_pacientes")
    if cursor.fetchone()[0] == 0:
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

sesion_activa: Dict = {
    "rut": None,
    "nombre": "Paciente Tótem",
    "edad": "En evaluación",
    "sintomas": [],
    "instrumentos": {"pa": "120/80 mmHg", "fc": "80 lpm", "temp": "37.0 °C", "sat": "98%"},
    "prediagnostico": "Triage de Entrada"
}

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
    if len(rut_limpio) < 2:
        return False
    cuerpo, dv = rut_limpio[:-1], rut_limpio[-1]
    if not cuerpo.isdigit():
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
    historial: Optional[List[Dict]] = []

# ------------------------------------------------------------------
# ENDPOINTS REST DE LA API CON SQLITE
# ------------------------------------------------------------------
@app.get("/api/pacientes")
@app.get("/api/v1/cola-pacientes")
def obtener_cola():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico FROM cola_pacientes")
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
            "prediagnostico": r[7]
        })
    return resultado

@app.get("/api/v1/historial")
def obtener_historial():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, fecha, rut, diagnostico, estado FROM historial_consultas")
    rows = cursor.fetchall()
    conn.close()

    resultado = []
    for r in rows:
        resultado.append({
            "id": r[0],
            "fecha": r[1],
            "rut": r[2],
            "diagnostico": r[3],
            "estado": r[4]
        })
    return resultado

@app.post("/api/v1/iniciar-consulta")
def iniciar_consulta(paciente: PacienteReq):
    if not validar_rut_chileno(paciente.rut):
        raise HTTPException(status_code=400, detail="RUT inválido.")
    global sesion_activa
    sesion_activa["rut"] = paciente.rut
    sesion_activa["sintomas"] = []
    return {"status": "success", "rut": paciente.rut}

@app.post("/api/v1/chat-evaluacion")
async def procesar_chat(chat: ChatReq):
    global sesion_activa
    
    sesion_activa["rut"] = chat.rut
    if chat.mensaje not in sesion_activa["sintomas"]:
        sesion_activa["sintomas"].append(chat.mensaje)

    if chat.paso >= 2:
        prediagnostico_final = "Evaluado por IA / Sospecha de Cuadro Clínico General"
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM cola_pacientes WHERE rut = ?", (chat.rut,))
        existe = cursor.fetchone()[0] > 0

        if not existe:
            cursor.execute("SELECT COUNT(*) FROM cola_pacientes")
            total = cursor.fetchone()[0]
            nuevo_id = f"AT-{total + 9017}"
            hora_actual = datetime.now().strftime("%H:%M")

            sintomas_json = json.dumps(list(sesion_activa["sintomas"]))
            instrumentos_json = json.dumps({"pa": "120/80 mmHg", "fc": "80 lpm", "temp": "37.0 °C", "sat": "98%"})

            cursor.execute("""
                INSERT INTO cola_pacientes (id, hora, rut, nombre, edad, sintomas, instrumentos, prediagnostico)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                nuevo_id,
                hora_actual,
                chat.rut,
                "Paciente Tótem",
                "30 años / Paciente",
                sintomas_json,
                instrumentos_json,
                prediagnostico_final
            ))
            conn.commit()
        conn.close()

        return {
            "respuesta_ia": "Gracias por detallar sus síntomas. He registrado su ficha y lo estoy derivando en este momento con el médico de turno...",
            "confianza": "85%",
            "siguiente_accion": "DERIVAR_TELEMEDICINA"
        }

    if GEMINI_API_KEY:
        try:
            messages = []
            if chat.historial:
                for item in chat.historial:
                    role = "user" if item.get("role") == "user" else "model"
                    messages.append({"role": role, "parts": [item.get("content", "")]})
            
            messages.append({"role": "user", "parts": [chat.mensaje]})
            
            response = model.generate_content(messages)
            respuesta_texto = response.text.strip()

            return {
                "respuesta_ia": respuesta_texto,
                "confianza": "70%",
                "siguiente_accion": "CONTINUAR_CUESTIONARIO"
            }
        except Exception as e:
            print(f"Error en Gemini API: {e}")

    return {
        "respuesta_ia": "Comprendo lo que me indica. ¿Hace cuántos días aproximadamente comenzaron estos malestares y con qué intensidad los siente?",
        "confianza": "50%",
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