"""
Filtro y limpieza de mensajes del chatbot del tótem.

Se usa en el backend (autoridad final) y su lógica está replicada en
`templates/index.html` (función `validarMensaje`) para dar respuesta inmediata
al paciente sin esperar al servidor. SI CAMBIA UNA REGLA AQUÍ, ACTUALÍCELA TAMBIÉN
EN EL JAVASCRIPT.

Qué bloquea:
  * mensajes vacíos o sin letras (solo números / símbolos),
  * groserías e insultos (incluye variantes tipo "weeeon", "m1erda"),
  * texto sin sentido: letras repetidas ("aaaaa"), "teclazos" ("asdfgh"),
    palabras sin vocales ("bcdfgh"),
  * mensajes irrelevantes que solo contienen saludos o relleno ("hola", "ok", "jaja").
Nunca bloquea términos anatómicos (p. ej. "culo", "poto") porque un paciente
puede necesitarlos para describir un malestar real.
"""
import re
import unicodedata
from dataclasses import dataclass

LARGO_MAXIMO = 500

# --- Listas editables ------------------------------------------------------
# Todo se compara sin tildes, en minúsculas y con letras repetidas colapsadas
# ("weeeon" -> "weon"), por eso las listas se escriben "normalizadas".
GROSERIAS_EXACTAS = {
    "puta", "putas", "puto", "putos", "putamadre", "hdp", "ctm", "csm",
    "mrd", "verga", "carajo", "chucha", "culiao", "culiado", "weon", "weona",
    "huevon", "hueon", "aweonao", "conchetumare", "conchesumadre", "zorra",
    "maricon", "cabron", "pendejo", "pelotudo", "imbecil", "idiota", "estupido",
    "estupida", "basuras", "mierda", "mierdas",
}
GROSERIAS_PREFIJOS = (
    "weon", "hueon", "aweon", "culia", "conchetu", "conchesu", "mierd",
    "pendej", "imbecil", "idiot", "estupid", "maricon", "chucha", "putamadre",
)

# Mensajes que, por sí solos, no describen ningún malestar.
RELLENO = {
    "hola", "holi", "buenas", "buenos", "ok", "okey", "oka", "test", "prueba", "xd", "jaja", "jeje", "jiji",
    "jaj", "hmm", "hm", "eh", "ehh", "em", "este", "nada", "algo", "cosa", "cosas",
    "bien", "gracias", "chao", "adios", "ola", "hey", "alo", "a", "e", "o", "y",
}

# Filas del teclado para detectar "teclazos" (asdfgh, qwerty, zxcvb...).
FILAS_TECLADO = ("qwertyuiop", "asdfghjklñ", "zxcvbnm")
VOCALES = set("aeiou")

MENSAJES = {
    "vacio": "Por favor, cuéntenos qué molestia tiene hoy. Puede escribirla o hablarla por el micrófono.",
    "groseria": "Entendemos que puede sentirse mal. Le pedimos usar un lenguaje respetuoso y contarnos con calma qué siente.",
    "sinsentido": "No logramos entender su mensaje. ¿Podría describir con claridad qué siente? Por ejemplo: «me duele la cabeza desde ayer».",
    "irrelevante": "Para ayudarle necesitamos saber qué molestia tiene. Por ejemplo: «tengo tos y fiebre desde ayer».",
}


@dataclass
class ResultadoFiltro:
    valido: bool
    texto: str          # texto ya limpio (si es válido) o el original limpiado (si no)
    codigo: str = ""    # "", "vacio", "groseria", "sinsentido", "irrelevante"
    mensaje: str = ""   # mensaje amable para mostrar al paciente


def limpiar_texto(texto: str) -> str:
    """Quita caracteres de control, colapsa espacios y recorta el largo."""
    texto = "".join(c for c in (texto or "") if c in "\n\t " or unicodedata.category(c)[0] != "C")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:LARGO_MAXIMO]


def _sin_tildes(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    # Conserva la ñ (la fila del teclado la incluye) y elimina el resto de tildes.
    return "".join(
        c for c in descompuesto
        if unicodedata.category(c) != "Mn" or c == "̃"
    ).replace("ñ", "ñ")


def _colapsar(palabra: str) -> str:
    """'weeeon' -> 'weon' (letras repetidas seguidas pasan a una sola)."""
    return re.sub(r"(.)\1+", r"\1", palabra)


_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_EXACTAS = {_colapsar(p) for p in GROSERIAS_EXACTAS}
_PREFIJOS = tuple(_colapsar(p) for p in GROSERIAS_PREFIJOS)


def _tiene_groseria(texto_normalizado: str) -> bool:
    for palabra in re.findall(r"[a-zñ0-9@$]+", texto_normalizado):
        # Solo se aplica "leet" a palabras que mezclan letras y símbolos (m1erda),
        # para no convertir números legítimos ("3 días") en letras.
        if re.search(r"[a-zñ]", palabra):
            palabra = palabra.translate(_LEET)
        palabra = _colapsar(palabra)
        if palabra in _EXACTAS or palabra.startswith(_PREFIJOS):
            return True
    return False


def _es_teclazo(palabra: str) -> bool:
    """Detecta 4+ letras consecutivas del teclado (en cualquier sentido)."""
    if len(palabra) < 4:
        return False
    for fila in FILAS_TECLADO:
        for texto in (fila, fila[::-1]):
            for i in range(len(texto) - 3):
                if texto[i:i + 4] in palabra:
                    return True
    return False


def _palabra_sin_sentido(palabra: str) -> bool:
    if len(palabra) >= 5 and not (set(palabra) & VOCALES):
        return True                                  # "bcdfgh"
    if len(palabra) >= 6 and len(set(palabra)) <= 2:
        return True                                  # "ababab"
    return _es_teclazo(palabra)                      # "asdfgh"


def validar_mensaje(texto: str) -> ResultadoFiltro:
    """Valida un mensaje del paciente. Devuelve el texto limpio o el motivo de rechazo."""
    limpio = limpiar_texto(texto)

    def rechazo(codigo: str) -> ResultadoFiltro:
        return ResultadoFiltro(False, limpio, codigo, MENSAJES[codigo])

    if not limpio:
        return rechazo("vacio")

    normal = _sin_tildes(limpio)
    letras = re.sub(r"[^a-zñ]", "", normal)
    if len(letras) < 2:
        return rechazo("sinsentido")        # solo números/símbolos o una sola letra

    if _tiene_groseria(normal):
        return rechazo("groseria")

    if re.search(r"(.)\1{3,}", normal):     # "aaaaa", "!!!!!"
        return rechazo("sinsentido")

    # "buenos días / buenas tardes" es un saludo, pero "3 días" es una respuesta válida.
    normal = re.sub(r"buen[oa]s (dias|tardes|noches)", " ", normal)
    palabras = re.findall(r"[a-zñ]+", normal)
    if any(_palabra_sin_sentido(p) for p in palabras):
        return rechazo("sinsentido")
    if len(letras) / max(1, len(re.sub(r"\s", "", normal))) < 0.5:
        return rechazo("sinsentido")        # demasiados números/símbolos

    # "si" y "no" son respuestas válidas a las preguntas de seguimiento.
    significativas = [p for p in palabras if p not in RELLENO]
    if not significativas:
        return rechazo("irrelevante")

    return ResultadoFiltro(True, limpio)
