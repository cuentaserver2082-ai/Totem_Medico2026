# Tótem Médico Inteligente

Prototipo de triage con ingreso por RUT chileno y portal profesional de telemedicina.

## Ejecutar en local

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

- Tótem en `http://127.0.0.1:8000/`.
- Portal profesional en `http://127.0.0.1:8000/doctor`.
- Configura `GEMINI_API_KEY` en el entorno o en `.env` para activar Gemini. `GEMINI_MODEL` permite cambiar el modelo predeterminado, `gemini-3.8-flash`. El tótem usa una pregunta local cuando Gemini no está disponible.

## Flujo del portal

El profesional asigna la prioridad desde la cola. El portal ordena por prioridad y, en caso de empate, por llegada. Los pacientes ingresan sin clasificar.

En la consulta, **Colgar** termina la videollamada y deja el formulario abierto. **Guardar y finalizar** archiva el diagnóstico y la receta o indicaciones en el historial y retira al paciente de la cola. La base SQLite local se actualiza al arrancar.
