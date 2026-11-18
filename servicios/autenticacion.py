"""
Autenticación sencilla del portal médico.

* Contraseñas con PBKDF2-SHA256 + sal (nunca en texto plano).
* Sesión = token aleatorio guardado en una cookie HttpOnly; en la base solo se
  guarda su hash SHA-256 (si se filtra la base, los tokens no sirven).
* Límite de intentos fallidos por IP+correo para frenar fuerza bruta.
"""
import hmac
import secrets
import time
from datetime import datetime, timedelta
from hashlib import pbkdf2_hmac, sha256
from typing import Dict, Optional

ITERACIONES = 200_000
DURACION_SESION_HORAS = 8
MAX_INTENTOS = 5
VENTANA_BLOQUEO_SEG = 300


def hashear_clave(clave: str, sal: Optional[bytes] = None) -> str:
    sal = sal or secrets.token_bytes(16)
    h = pbkdf2_hmac("sha256", clave.encode(), sal, ITERACIONES)
    return f"pbkdf2${ITERACIONES}${sal.hex()}${h.hex()}"


def verificar_clave(clave: str, almacenado: str) -> bool:
    try:
        _, iteraciones, sal, esperado = almacenado.split("$")
        h = pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(sal), int(iteraciones))
        return hmac.compare_digest(h.hex(), esperado)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def crear_sesion(conn, medico_id: int) -> str:
    """Crea una sesión nueva y devuelve el token (solo se entrega una vez)."""
    token = secrets.token_urlsafe(32)
    ahora = datetime.now()
    conn.execute("DELETE FROM sesiones_medico WHERE expira < ?", (ahora.isoformat(timespec="seconds"),))
    conn.execute(
        "INSERT INTO sesiones_medico (token_hash, medico_id, expira) VALUES (?, ?, ?)",
        (_hash_token(token), medico_id,
         (ahora + timedelta(hours=DURACION_SESION_HORAS)).isoformat(timespec="seconds")),
    )
    return token


def obtener_medico_por_token(conn, token: Optional[str]) -> Optional[Dict]:
    """Devuelve los datos del médico si el token es válido y no expiró; si no, None."""
    if not token:
        return None
    fila = conn.execute("""
        SELECT m.id, m.email, m.nombre, m.especialidad, m.registro
        FROM sesiones_medico s JOIN medicos m ON m.id = s.medico_id
        WHERE s.token_hash = ? AND s.expira > ?
    """, (_hash_token(token), datetime.now().isoformat(timespec="seconds"))).fetchone()
    if not fila:
        return None
    return dict(zip(("id", "email", "nombre", "especialidad", "registro"), fila))


def cerrar_sesion(conn, token: Optional[str]) -> None:
    if token:
        conn.execute("DELETE FROM sesiones_medico WHERE token_hash = ?", (_hash_token(token),))


class LimitadorIntentos:
    """Bloquea temporalmente una clave (IP+correo) tras demasiados intentos fallidos."""

    def __init__(self):
        self._fallos: Dict[str, list] = {}

    def _recientes(self, clave: str) -> list:
        limite = time.monotonic() - VENTANA_BLOQUEO_SEG
        self._fallos[clave] = [t for t in self._fallos.get(clave, []) if t > limite]
        return self._fallos[clave]

    def bloqueado(self, clave: str) -> bool:
        return len(self._recientes(clave)) >= MAX_INTENTOS

    def registrar_fallo(self, clave: str) -> None:
        self._recientes(clave).append(time.monotonic())

    def limpiar(self, clave: str) -> None:
        self._fallos.pop(clave, None)
