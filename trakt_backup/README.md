# Backup de Trakt

Script para descargar el historial completo de una cuenta de Trakt.tv y
empaquetarlo en un `.zip` con archivos JSON, usando la API pública v2 de
Trakt (no hay exportación nativa).

## Setup

1. Creá una app en https://trakt.tv/oauth/applications
   - Redirect URI: `urn:ietf:wg:oauth:2.0:oob`
2. Copiá `.env.example` a `.env` y completá `TRAKT_CLIENT_ID` /
   `TRAKT_CLIENT_SECRET`.
3. Instalá las dependencias:
   ```
   pip install -r requirements.txt
   ```

## Uso

```
python trakt_backup.py
```

La primera vez te va a pedir que visites `https://trakt.tv/activate` e
ingreses un código para autorizar la app (device code flow). El token
queda cacheado en `.trakt_token.json` (nunca se commitea) y se renueva
solo en corridas siguientes.

Opciones:

- `--login`: fuerza un nuevo login aunque haya un token cacheado.
- `--output DIR`: carpeta donde se genera el backup (default: directorio
  actual).

## Qué exporta

`history.json`, `ratings.json`, `watchlist.json`, `collection_movies.json`,
`collection_shows.json`, `lists/<nombre>.json` (una por lista personalizada),
`comments.json` y `stats.json`, todo comprimido en
`trakt_backup_<fecha>.zip`.

## Notas

- Nunca commitees `.env` ni `.trakt_token.json` (ya están en `.gitignore`).
- Si Trakt responde `429` (rate limit), el script reintenta solo con
  backoff exponencial respetando el header `Retry-After`.
