# Sprint 3 — Activación de datos reales y Render

## 1. Crear/obtener API key de API-Football

La clave debe guardarse únicamente como variable de entorno. Nunca debe escribirse en el repositorio, README, issues o logs.

Variable requerida:

```text
API_FOOTBALL_KEY=<secret>
```

## 2. Diagnóstico local/Render

```bash
python manage.py provider_diagnostic --date 2026-08-08
```

Para inspeccionar cuotas y alineaciones de un fixture concreto:

```bash
python manage.py provider_diagnostic --date 2026-08-08 --fixture-id <fixture_id>
```

El reporte comprueba:

- conectividad con API-Football;
- estado de la suscripción;
- disponibilidad del bookmaker preferido (`Betano`);
- número de fixtures disponibles;
- cobertura de cuotas BTTS/Over 2.5 para un fixture;
- disponibilidad de alineaciones.

## 3. Scanner real

```bash
python manage.py scan_daily
```

La fecha por defecto se calcula con `APP_TIMEZONE=America/Lima`.

Salida operativa:

- fixtures del proveedor;
- fixtures procesados;
- fixtures con cuotas Betano;
- porcentaje de cobertura Betano;
- fixtures con alineaciones;
- errores por fixture;
- Tier A ordenado por EV.

## 4. Render Blueprint

`render.yaml` crea:

1. Web service `football-value-engine`.
2. PostgreSQL `football-value-engine-db`.
3. Cron `football-value-engine-daily-scan`.

El cron está programado a las `11:00 UTC`, equivalente a las `06:00` en Perú (UTC-5), y ejecuta:

```bash
python manage.py scan_daily
```

## 5. Variables que deben configurarse en Render

- `API_FOOTBALL_KEY`: secreto, obligatorio.
- `PREFERRED_BOOKMAKER=Betano`.
- `APP_TIMEZONE=America/Lima`.
- `MIN_EDGE=0.06`.
- `MIN_EV=0.08`.

`DATABASE_URL` se conecta automáticamente al PostgreSQL creado por el Blueprint.

## 6. Regla de seguridad de cuotas

Si Betano no aparece para un fixture/mercado, el sistema no reemplaza silenciosamente la cuota con otra casa. La predicción puede persistirse para estudio, pero no puede ser Tier A financiero sin cuota válida para calcular Edge y EV.

## 7. Siguiente bloque

Después de desplegar y validar una API key real:

- ejecutar `provider_diagnostic`;
- verificar cobertura Betano;
- ejecutar el primer `scan_daily` real;
- revisar consumo de requests;
- añadir revalidación prepartido para alineaciones y movimiento de cuotas.
