"""
Auto-triage: asigna la prioridad (1-4) de un paciente combinando
síntomas del chat + signos vitales del tótem.

Escala (coincide con el selector del portal médico):
    1 = Emergencia   2 = Urgente   3 = Moderada   4 = Leve

Estrategia (a prueba de fallos):
  1. SIEMPRE se calculan reglas clínicas locales (funcionan sin internet).
  2. Si hay Gemini disponible se le pide una segunda opinión en JSON.
  3. Prioridad final = la MÁS GRAVE (número menor) entre ambas. Así la IA nunca
     puede "bajar" una alerta detectada por las reglas.
El médico puede cambiarla manualmente desde el portal.

IMPORTANTE: mientras los signos vitales sean simulados, el triage es solo una
demostración y NO debe usarse para decisiones clínicas reales.
"""
import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

NIVELES = {1: "Emergencia", 2: "Urgente", 3: "Moderada", 4: "Leve"}

PROMPT_TRIAGE = """
Eres un apoyo de triage en un centro de salud de Chile. Con los síntomas relatados por el
paciente y sus signos vitales, sugiere una prioridad de atención del 1 al 4:
1 = Emergencia (riesgo vital inmediato), 2 = Urgente, 3 = Moderada, 4 = Leve.
No diagnostiques ni indiques fármacos. Responde SOLO un JSON:
{"prioridad": <1-4>, "justificacion": "<máximo 20 palabras>"}
"""

# (patrón sobre texto sin tildes, nivel, descripción). Se evalúan todos; gana el más grave.
REGLAS_SINTOMAS: List[Tuple[str, int, str]] = [
    # --- Nivel 1: emergencia ---
    (r"no (puedo|logro) respirar|me ahog|ahogo", 1, "dificultad respiratoria severa"),
    (r"desmay|perdi (la )?(conciencia|el conocimiento)|inconsciente|convuls", 1, "pérdida de conciencia / convulsión"),
    (r"sangrado (abundante|intenso|profuso)|mucha sangre|no para de sangrar", 1, "sangrado abundante"),
    (r"dolor (opresivo|que oprime)|opresion (en el )?pecho|(pecho|brazo izquierdo).*(sudor frio|mandibula)", 1, "dolor opresivo en el pecho"),
    (r"cara (torcida|caida)|boca torcida|no puedo (hablar|mover)|infarto|derrame", 1, "posibles signos de infarto / ACV"),
    (r"vomit\w* sangre|sangre al vomitar|labios (morados|azules)", 1, "vómito con sangre / cianosis"),
    (r"hinchazon (de )?(la )?(garganta|lengua|cara)|reaccion alergica grave", 1, "posible reacción alérgica grave"),
    # --- Nivel 2: urgente ---
    (r"(dolor|molestia|presion|punzada)\w* (en|de) (el )?pecho|pecho", 2, "molestia en el pecho"),
    (r"dificultad (para )?respirar|respiro mal|falta de aire", 2, "dificultad para respirar"),
    (r"fiebre (muy )?alta|mucha fiebre", 2, "fiebre alta referida"),
    (r"dolor (muy )?(intenso|insoportable)|insoportable", 2, "dolor muy intenso"),
    (r"sangr|palpitaciones|confusion|desorientad|golpe (fuerte )?en la cabeza|traumatismo", 2, "sangrado / palpitaciones / trauma"),
    # --- Nivel 3: moderada ---
    (r"fiebre|temperatura|escalofrio", 3, "fiebre"),
    (r"vomit|nausea|diarrea", 3, "vómitos / diarrea"),
    (r"dolor (de |en el )?(abdomen|estomago|guata|oido)|mareo|infeccion|ardor al orinar", 3, "dolor abdominal / mareo / infección"),
    # --- Nivel 4: leve (síntomas comunes) ---
    (r"dolor|duele|molestia|malestar|tos|resfr|congestion|garganta|decaimiento|cansancio|debilidad", 4, "malestar general / síntoma leve"),
]
_INTENSIDAD = re.compile(r"\b(muy|mucho|mucha|intens\w*|fuerte|terrible|insoportable)\b")


@dataclass
class ResultadoTriage:
    prioridad: int
    motivo: str
    origen: str = "reglas"   # "reglas" o "gemini+reglas"

    @property
    def etiqueta(self) -> str:
        return NIVELES[self.prioridad]

    def resumen(self) -> str:
        """Texto que se guarda como 'prediagnóstico' y ve el médico."""
        return f"Triage automático: {self.etiqueta}. {self.motivo}"


def _normalizar(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def evaluar_signos_vitales(v: Optional[Dict]) -> List[Tuple[int, str]]:
    """Devuelve la lista de hallazgos (nivel, descripción) fuera de rango."""
    if not v:
        return []
    h: List[Tuple[int, str]] = []
    spo2, temp, fc = v.get("spo2"), v.get("temp_c"), v.get("fc_lpm")
    sis, dia = v.get("pa_sis"), v.get("pa_dia")

    if spo2 is not None:
        if spo2 < 90: h.append((1, f"SpO₂ {spo2}% muy baja"))
        elif spo2 < 94: h.append((2, f"SpO₂ {spo2}% baja"))
        elif spo2 < 96: h.append((3, f"SpO₂ {spo2}% levemente baja"))
    if temp is not None:
        if temp >= 39.5 or temp < 35: h.append((2, f"temperatura {temp:.1f} °C"))
        elif temp >= 38: h.append((3, f"fiebre {temp:.1f} °C"))
    if fc is not None:
        if fc > 130 or fc < 40: h.append((1, f"frecuencia cardíaca {fc} lpm"))
        elif fc > 110 or fc < 50: h.append((2, f"frecuencia cardíaca {fc} lpm"))
        elif fc > 100 or fc < 55: h.append((3, f"frecuencia cardíaca {fc} lpm"))
    if sis is not None and dia is not None:
        if sis >= 180 or dia >= 120 or sis < 80: h.append((1, f"presión {sis}/{dia} mmHg"))
        elif sis >= 160 or dia >= 100 or sis < 90: h.append((2, f"presión {sis}/{dia} mmHg"))
        elif sis >= 140 or dia >= 90: h.append((3, f"presión {sis}/{dia} mmHg elevada"))
    return h


def evaluar_sintomas(sintomas: List[str]) -> List[Tuple[int, str]]:
    """Devuelve los hallazgos (nivel, descripción) detectados en el relato del paciente."""
    texto = _normalizar(" . ".join(sintomas))
    hallazgos: List[Tuple[int, str]] = []
    for patron, nivel, descripcion in REGLAS_SINTOMAS:
        if re.search(patron, texto):
            hallazgos.append((nivel, descripcion))
    # Si el paciente enfatiza la intensidad, sube un nivel (mínimo urgente).
    if hallazgos and _INTENSIDAD.search(texto):
        mejor = min(n for n, _ in hallazgos)
        if mejor >= 3:
            hallazgos = [(mejor - 1, "síntoma referido como intenso")] + hallazgos
    return hallazgos


def triage_por_reglas(sintomas: List[str], vitales: Optional[Dict]) -> ResultadoTriage:
    hallazgos = evaluar_sintomas(sintomas) + evaluar_signos_vitales(vitales)
    if not hallazgos:
        return ResultadoTriage(4, "Sin señales de alarma en síntomas ni signos vitales.")
    prioridad = min(n for n, _ in hallazgos)
    # Dos o más hallazgos moderados en conjunto (p. ej. fiebre + taquicardia) suben a urgente.
    if prioridad == 3 and sum(1 for n, _ in hallazgos if n == 3) >= 2:
        prioridad = 2
    razones = []
    for nivel, d in sorted(hallazgos, key=lambda x: x[0]):
        if nivel == 4 and prioridad < 4:      # el "síntoma leve" no aporta si hay algo más importante
            continue
        if d not in razones:
            razones.append(d)
    return ResultadoTriage(prioridad, "Se detectó: " + "; ".join(razones[:4]) + ".")


async def _segunda_opinion_gemini(cliente, modelo: str, sintomas: List[str], vitales: Optional[Dict]) -> Optional[Tuple[int, str]]:
    """Pide a Gemini una prioridad en JSON. Devuelve None si falla (sin romper el flujo)."""
    from google.genai import types  # import local: el módulo sigue siendo testeable sin la librería
    datos = {"sintomas": sintomas, "signos_vitales": vitales or "no disponibles"}
    try:
        resp = await asyncio.wait_for(
            cliente.aio.models.generate_content(
                model=modelo,
                contents=json.dumps(datos, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=PROMPT_TRIAGE,
                    response_mime_type="application/json",
                    max_output_tokens=200,
                ),
            ),
            timeout=8,
        )
        data = json.loads(resp.text)
        prioridad = int(data["prioridad"])
        if prioridad in NIVELES:
            return prioridad, str(data.get("justificacion", ""))[:200]
    except Exception as exc:
        print(f"Triage con Gemini no disponible ({type(exc).__name__}); se usan solo las reglas.")
    return None


async def clasificar(sintomas: List[str], vitales: Optional[Dict], cliente_gemini=None, modelo: str = "") -> ResultadoTriage:
    """Punto de entrada: reglas locales + (opcional) segunda opinión de Gemini."""
    resultado = triage_por_reglas(sintomas, vitales)
    if cliente_gemini:
        ia = await _segunda_opinion_gemini(cliente_gemini, modelo, sintomas, vitales)
        if ia:
            prioridad_ia, justificacion = ia
            if prioridad_ia < resultado.prioridad:       # la IA detectó algo más grave
                resultado = ResultadoTriage(prioridad_ia, f"IA: {justificacion}", "gemini+reglas")
            else:
                resultado.origen = "gemini+reglas"
    return resultado
