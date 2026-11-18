# Tótem Médico Inteligente

Prototipo para reducir las **filas de espera** en centros de salud: el paciente (pensado para adultos mayores) se
identifica con su RUT en una tablet en modo kiosco, se "mide" con la cámara, cuenta su malestar a un asistente y
habla por videollamada con el médico, que ve la cola ordenada por prioridad.

## Ejecutar en local

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt        # Windows: .venv\Scripts\python
cp .env.example .env                                         # y complete DOCTOR_PASSWORD (y GEMINI_API_KEY si desea IA)
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

| Vista | URL | Acceso |
|---|---|---|
| Tótem del paciente | `http://127.0.0.1:8000/` | Libre: solo ingresa su RUT |
| Portal médico | `http://127.0.0.1:8000/doctor` | Correo + contraseña (definidos en `.env`) |

> **Cámara y micrófono** solo funcionan en `localhost` o en **HTTPS**. Si la tablet abre el sistema por IP de red
> (`http://192.168.x.x:8000`) el navegador los bloqueará: sirva por HTTPS (y ponga `COOKIE_SECURE=1`) o ejecute el
> navegador en el mismo equipo que el servidor.
> Modo kiosco en Chrome: `chrome --kiosk http://127.0.0.1:8000/`.

Si no define `DOCTOR_PASSWORD`, se usa la clave de demostración `Totem2026!` (el servidor lo avisa en consola).

## Estructura

```
app.py                      API FastAPI, base SQLite, WebSocket/WebRTC (secciones numeradas y comentadas)
servicios/filtro_texto.py   Filtro de groserías / spam / vacío del chatbot (espejo en index.html)
servicios/triage.py         Auto-triage: reglas clínicas locales + segunda opinión de Gemini
servicios/autenticacion.py  Hash de claves, sesiones por cookie, límite de intentos
templates/index.html        Tótem (paciente)        templates/doctor.html   Portal médico
static/img/image_4135df.jpg Fondo de la pantalla de bienvenida
tests/test_servicios.py     python -m unittest discover tests -v
```

## Flujo del paciente (tótem)

1. **RUT** (teclado grande, validado con módulo 11 en el tótem y en el servidor) sobre la foto del hospital.
2. **Medición con cámara** (6–8 s, simulada): temperatura, SpO₂, pulso y presión → se guardan en `instrumentos`.
3. **Chat de síntomas** con filtro de groserías / texto sin sentido; voz y texto.
4. **Espera y videollamada**: cuenta regresiva de 5 min hasta que el médico se conecta.

### Tiempos (en `CONFIG`, arriba del `<script>` de `index.html`)
* **5 min de inactividad** en medición o chat → aviso "¿Sigue ahí?" 30 s antes → reinicio a la pantalla de RUT con mensaje y datos borrados.
* **5 min de espera del médico** → mensaje *"Tiempo de espera finalizado, por favor intente nuevamente o acérquese a mesón"*,
  el paciente sale de la cola (queda registrado en el historial) y se vuelve al inicio.
* Para demos se pueden acortar por URL (segundos): `/?inactividad=20&espera=15&escaneo=3&sensores=3`.

## Auto-triage

Escala: **1 Emergencia · 2 Urgente · 3 Moderada · 4 Leve**. Se calcula al terminar el chat con reglas locales
(síntomas + signos vitales) y, si hay `GEMINI_API_KEY`, una segunda opinión de Gemini; se conserva **la más grave**.
El médico puede cambiarla en la cola (queda marcada "Ajustada por médico" y el sistema ya no la pisa).

> ⚠️ Los signos vitales son **simulados** (`generarTemperatura()` y `generarLecturaSensores()` en `index.html`). Mientras sea así, el triage es una
> demostración y no debe usarse para decisiones clínicas. Para conectar un sensor real, reemplace esa función.

## Seguridad

* Todas las rutas del médico (`/api/v1/cola-pacientes`, `/historial`, `/prioridad`, `/finalizar`) y su WebSocket exigen sesión.
* Contraseñas con PBKDF2; la sesión es una cookie `HttpOnly` + `SameSite=Strict` y en la BD solo se guarda su hash.
* Máximo 5 intentos de login fallidos por IP+correo cada 5 min.
* Pendiente recomendado: mover las credenciales TURN de `rtcConfig` (están en el código del navegador) a variables de entorno.

## Limitación conocida
La señalización WebRTC admite **un tótem y un médico a la vez**. Para varios tótems hay que identificar cada sala (p. ej. por id de atención).


## Novedades de la Ronda 2
* **WebRTC sin cámara**: el médico responde a la oferta aunque no tenga cámara/micrófono (o niegue permisos); lo que no puede enviar queda en `recvonly`, así recibe igual video y audio del paciente. Se muestra el aviso "Médico sin cámara (solo audio/vista)".
* **Cámara térmica simulada** (5 s, solo temperatura) separada del **módulo de sensores** (oxímetro + brazalete: SpO2, pulso, presión). Los 4 valores se unen en `instrumentos`.
* **Chat médico↔paciente** por los mismos WebSockets: `{"tipo":"chat","emisor":"doctor|paciente","texto":"..."}` (el servidor fija el emisor y no guarda el chat).
* **Fin de la atención**: botón grande "Finalizar llamada"; al guardar el médico, el tótem muestra solo la tarjeta de Diagnóstico y Receta (aviso `resumen_listo`) con "Finalizar y Salir".
* **Historial** con búsqueda por RUT/fecha, detalle en ventana y botones "Volver" sin cerrar sesión.
