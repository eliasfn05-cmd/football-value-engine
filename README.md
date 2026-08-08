# Football Value Engine

Motor cuantitativo para detectar apuestas de fútbol con valor esperado positivo (EV) en mercados BTTS y Over 2.5.

## Estado

Versión inicial de arquitectura (V1.0).

## Objetivos

- Escaneo diario de fixtures.
- Modelos BTTS y Over 2.5.
- Filtros contextuales y de riesgo.
- Cálculo de probabilidad implícita, cuota justa, Edge y EV.
- Ranking Tier A.
- Backtesting por versión del modelo.
- Despliegue en Render con PostgreSQL.

## Stack

- Python 3.12
- Django 5
- PostgreSQL
- Gunicorn
- Docker
- Render

## Próximos pasos

1. Integrar proveedor de datos deportivos.
2. Crear modelos de dominio y persistencia.
3. Implementar Daily Scanner.
4. Añadir dashboard de Tier A y backtesting.
