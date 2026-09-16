#!/usr/bin/env python3
"""Backup completo de una cuenta de Trakt.tv a un .zip con archivos JSON.

Descarga historial, ratings, watchlist, colección, listas personalizadas,
comentarios y estadísticas usando la API pública de Trakt (v2), y empaqueta
todo en trakt_backup_<fecha>.zip.

Uso:
    python trakt_backup.py                # login (si hace falta) + backup completo
    python trakt_backup.py --login        # fuerza un nuevo login (device code flow)
    python trakt_backup.py --output DIR   # carpeta destino (default: directorio actual)

Configuración (ver .env.example):
    TRAKT_CLIENT_ID
    TRAKT_CLIENT_SECRET
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.trakt.tv"
TOKEN_FILE = Path(os.environ.get("TRAKT_TOKEN_FILE", ".trakt_token.json"))

CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TRAKT_CLIENT_SECRET")

HISTORY_PAGE_LIMIT = 100
MAX_RETRIES = 5
TOKEN_EXPIRY_MARGIN = 300  # renovar el token 5 min antes de que expire
DEVICE_POLL_TIMEOUT_PAD = 5  # segundos de margen antes de darse por vencido


def _require_credentials() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit(
            "Faltan TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET. Creá una app en "
            "https://trakt.tv/oauth/applications y configuralas como variables "
            "de entorno (ver .env.example)."
        )


def device_login() -> dict:
    """Corre el flujo OAuth2 de device code y devuelve el token."""
    _require_credentials()

    resp = requests.post(
        f"{API_BASE}/oauth/device/code",
        json={"client_id": CLIENT_ID},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_url = data["verification_url"]
    interval = data["interval"]
    expires_in = data["expires_in"]

    print(f"\nVisitá {verification_url} e ingresá el código: {user_code}\n")
    print("Esperando autorización...")

    deadline = time.monotonic() + expires_in - DEVICE_POLL_TIMEOUT_PAD
    while time.monotonic() < deadline:
        time.sleep(interval)
        token_resp = requests.post(
            f"{API_BASE}/oauth/device/token",
            json={
                "code": device_code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=30,
        )
        if token_resp.status_code == 200:
            token = token_resp.json()
            _save_token(token)
            print("Autenticación exitosa.\n")
            return token
        if token_resp.status_code == 400:
            continue  # autorización pendiente, seguir esperando
        if token_resp.status_code == 404:
            sys.exit("Device code inválido.")
        if token_resp.status_code == 409:
            sys.exit("Ese código ya fue usado.")
        if token_resp.status_code == 410:
            sys.exit("El código expiró, volvé a correr el script.")
        if token_resp.status_code == 418:
            sys.exit("Autorización denegada por el usuario.")
        if token_resp.status_code == 429:
            interval += 1  # bajar el ritmo de polling
            continue
        token_resp.raise_for_status()

    sys.exit("Tiempo de espera agotado para la autenticación.")


def _save_token(token: dict) -> None:
    token = dict(token)
    token["obtained_at"] = time.time()
    TOKEN_FILE.write_text(json.dumps(token, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _load_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def refresh_token(token: dict) -> dict:
    _require_credentials()
    resp = requests.post(
        f"{API_BASE}/oauth/token",
        json={
            "refresh_token": token["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    new_token = resp.json()
    _save_token(new_token)
    return new_token


def get_access_token(force_login: bool = False) -> str:
    if not force_login:
        token = _load_token()
        if token:
            obtained_at = token.get("obtained_at", 0)
            expires_in = token.get("expires_in", 0)
            if time.time() < obtained_at + expires_in - TOKEN_EXPIRY_MARGIN:
                return token["access_token"]
            if token.get("refresh_token"):
                try:
                    token = refresh_token(token)
                    return token["access_token"]
                except requests.HTTPError:
                    pass  # el refresh token no sirve, se cae al login

    token = device_login()
    return token["access_token"]


class TraktClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": CLIENT_ID,
                "Authorization": f"Bearer {access_token}",
            }
        )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{API_BASE}{path}"
        backoff = 1
        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                print(f"  Rate limit alcanzado, esperando {retry_after}s (intento {attempt}/{MAX_RETRIES})...")
                time.sleep(retry_after)
                backoff *= 2
                continue
            if resp.status_code >= 500:
                print(f"  Error {resp.status_code} del servidor, reintentando en {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        return self._request("GET", path, params=params)

    def fetch_json(self, path: str, params: dict | None = None) -> Any:
        resp = self.get(path, params=params)
        return resp.json() if resp.content else None

    def fetch_paginated(self, path: str, limit: int = HISTORY_PAGE_LIMIT) -> list:
        """Itera páginas de un endpoint hasta que se acaben los resultados."""
        results: list = []
        page = 1
        while True:
            resp = self.get(path, params={"page": page, "limit": limit})
            chunk = resp.json() if resp.content else []
            if not chunk:
                break
            results.extend(chunk)
            page_count = int(resp.headers.get("X-Pagination-Page-Count", page))
            if page >= page_count:
                break
            page += 1
        return results


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _safe_filename(name: str) -> str:
    keep = "-_ "
    cleaned = "".join(c if c.isalnum() or c in keep else "_" for c in name)
    return cleaned.strip().replace(" ", "_") or "lista"


def _export_lists(client: TraktClient, output_dir: Path) -> None:
    lists = client.fetch_json("/users/me/lists") or []
    lists_dir = output_dir / "lists"
    for lst in lists:
        trakt_id = lst["ids"]["trakt"]
        name = lst.get("name") or lst["ids"].get("slug") or str(trakt_id)
        items = client.fetch_json(f"/users/me/lists/{trakt_id}/items")
        path = lists_dir / f"{_safe_filename(name)}.json"
        if path.exists():
            path = lists_dir / f"{_safe_filename(name)}_{trakt_id}.json"
        _write_json(path, items)


def export_all(client: TraktClient, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Descargando historial...")
    _write_json(output_dir / "history.json", client.fetch_paginated("/users/me/history"))

    print("Descargando ratings...")
    _write_json(output_dir / "ratings.json", client.fetch_json("/users/me/ratings"))

    print("Descargando watchlist...")
    _write_json(output_dir / "watchlist.json", client.fetch_json("/users/me/watchlist"))

    print("Descargando colección de películas...")
    _write_json(output_dir / "collection_movies.json", client.fetch_json("/users/me/collection/movies"))

    print("Descargando colección de shows...")
    _write_json(output_dir / "collection_shows.json", client.fetch_json("/users/me/collection/shows"))

    print("Descargando listas personalizadas...")
    _export_lists(client, output_dir)

    try:
        print("Descargando comentarios...")
        _write_json(output_dir / "comments.json", client.fetch_paginated("/users/me/comments/all"))
    except requests.HTTPError as exc:
        print(f"  No se pudieron descargar los comentarios: {exc}")

    try:
        print("Descargando estadísticas...")
        _write_json(output_dir / "stats.json", client.fetch_json("/users/me/stats"))
    except requests.HTTPError as exc:
        print(f"  No se pudieron descargar las estadísticas: {exc}")

    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup de una cuenta de Trakt a un .zip con JSON.")
    parser.add_argument("--login", action="store_true", help="Forzar un nuevo login (device code flow).")
    parser.add_argument("--output", default=".", help="Directorio donde guardar el backup (default: directorio actual).")
    args = parser.parse_args()

    access_token = get_access_token(force_login=args.login)
    client = TraktClient(access_token)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_dir = Path(args.output)
    base_dir.mkdir(parents=True, exist_ok=True)
    backup_name = f"trakt_backup_{date_str}"
    backup_dir = base_dir / backup_name

    export_all(client, backup_dir)

    print("Comprimiendo backup...")
    archive_path = shutil.make_archive(str(base_dir / backup_name), "zip", root_dir=base_dir, base_dir=backup_name)
    shutil.rmtree(backup_dir)

    print(f"\nBackup listo: {archive_path}")


if __name__ == "__main__":
    main()
