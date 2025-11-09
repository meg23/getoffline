import json
import os
from importlib import import_module
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from logger import log
from utils import ensure_dir, sanitize

COOMER_API_BASE = "https://coomer.su/api/v1"


def download_creator_posts(config: Dict[str, Any], downloaded_items: List[str]) -> None:
    """Download the latest posts for each configured creator."""
    defaults = config.get("defaults", {})
    root_folder = os.path.join(defaults.get("output_root", "./downloads"), "creators")

    for entry in config.get("coomer", []):
        try:
            display_name = entry["name"]
            creator_identifier = entry.get("artist") or entry.get("creator") or entry["name"]
            service = entry.get("service", "onlyfans")
            limit = int(entry.get("limit", 10))

            folder_name = sanitize(display_name)
            creator_folder = os.path.join(root_folder, folder_name)
            ensure_dir(creator_folder)

            posts = _fetch_latest_posts(service, creator_identifier, limit)
            if not posts:
                log.info("📭 No posts returned for %s on %s", creator_identifier, service)
                continue

            archive_path = os.path.join(creator_folder, "downloaded_posts.txt")
            processed_ids = _load_archive(archive_path)

            new_ids: List[str] = []
            for post in posts:
                post_id = _extract_post_id(post)
                if not post_id or post_id in processed_ids:
                    continue

                target_path = os.path.join(creator_folder, f"{post_id}.json")
                with open(target_path, "w", encoding="utf-8") as handle:
                    json.dump(post, handle, ensure_ascii=False, indent=2)

                new_ids.append(post_id)

            if new_ids:
                _append_archive(archive_path, new_ids)
                downloaded_items.append(
                    f"Coomer: {display_name} – {len(new_ids)} new post{'s' if len(new_ids) != 1 else ''}"
                )
                log.info(
                    "🧵 Stored %s new post(s) for %s (%s)",
                    len(new_ids),
                    creator_identifier,
                    service,
                )
            else:
                log.info("✅ Creator %s (%s) is already up to date", creator_identifier, service)
        except Exception as exc:  # pragma: no cover - defensive logging
            log.error("❌ Failed to process creator entry %s: %s", entry, exc)


def _load_archive(path: str) -> Iterable[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        return set(line.strip() for line in handle if line.strip())


def _append_archive(path: str, identifiers: Iterable[str]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for identifier in identifiers:
            handle.write(f"{identifier}\n")


def _extract_post_id(post: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "post_id", "postId", "UID"):
        value = post.get(key)
        if value is not None:
            return str(value)
    return None


def _fetch_latest_posts(service: str, creator: str, limit: int) -> List[Dict[str, Any]]:
    fetcher = _resolve_library_fetcher()
    if fetcher is not None:
        try:
            posts = fetcher(service, creator, limit)
            if posts:
                return list(posts)
        except Exception as exc:  # pragma: no cover - best effort fallback
            log.warning(
                "⚠️  Coomer CLI library fetch failed for %s/%s: %s. Falling back to HTTP API.",
                service,
                creator,
                exc,
            )
    return _fetch_via_http(service, creator, limit)


def _resolve_library_fetcher() -> Optional[Callable[[str, str, int], Iterable[Dict[str, Any]]]]:
    candidates = [
        ("coomer_cli.client", "CoomerClient"),
        ("coomer_cli.cli", "CoomerClient"),
        ("coomer.client", "CoomerClient"),
        ("coomer.client", "Client"),
    ]

    for module_name, attribute in candidates:
        try:
            module = import_module(module_name)
        except ImportError:
            continue

        client_class = getattr(module, attribute, None)
        if client_class is None:
            continue

        try:
            client = client_class()
        except TypeError:
            try:
                client = client_class(base_url=COOMER_API_BASE)
            except Exception:
                continue
        except Exception:
            continue

        fetch_method = _resolve_fetch_method(client)
        if fetch_method is not None:
            return fetch_method

    # Function-style API fallbacks
    function_candidates = [
        ("coomer_cli.client", "get_creator_posts"),
        ("coomer_cli.cli", "get_creator_posts"),
        ("coomer.api", "get_creator_posts"),
        ("coomer_cli.api", "get_creator_posts"),
    ]

    for module_name, attribute in function_candidates:
        try:
            module = import_module(module_name)
        except ImportError:
            continue
        func = getattr(module, attribute, None)
        if callable(func):
            return lambda service, creator, limit, _func=func: _call_with_variants(
                _func,
                service,
                creator,
                limit,
            )

    return None


def _resolve_fetch_method(client: Any) -> Optional[Callable[[str, str, int], Iterable[Dict[str, Any]]]]:
    method_candidates = [
        "get_creator_posts",
        "get_posts",
        "creator_posts",
        "posts",
    ]

    for method_name in method_candidates:
        method = getattr(client, method_name, None)
        if callable(method):
            return lambda service, creator, limit, _method=method: _call_with_variants(
                _method,
                service,
                creator,
                limit,
            )
    return None


def _call_with_variants(func: Callable[..., Any], service: str, creator: str, limit: int) -> Any:
    variants = [
        {"service": service, "creator": creator, "limit": limit},
        {"service": service, "name": creator, "limit": limit},
        {"service": service, "identifier": creator, "limit": limit},
        {"service": service, "user": creator, "limit": limit},
        {"service": service, "username": creator, "limit": limit},
        {"service": service, "model": creator, "limit": limit},
        {"service": service, "model_name": creator, "limit": limit},
        {"service": service, "artist": creator, "limit": limit},
        {"service": service, "creator_name": creator, "limit": limit},
        {"service": service, "creator": creator, "limit": limit, "per_page": limit},
        {"service": service, "name": creator, "per_page": limit},
        (service, creator, limit),
        (service, creator),
        (creator, limit),
    ]

    for variant in variants:
        try:
            if isinstance(variant, dict):
                return func(**variant)
            return func(*variant)
        except TypeError:
            continue
    raise RuntimeError("Unable to invoke Coomer client with available signatures")


def _fetch_via_http(service: str, creator: str, limit: int) -> List[Dict[str, Any]]:
    url = f"{COOMER_API_BASE}/creators/{service}/{creator}/posts"
    params = {"limit": limit}
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        log.error("❌ HTTP request for %s/%s failed: %s", service, creator, exc)
        return []
    except ValueError as exc:
        log.error("❌ Unable to decode JSON for %s/%s: %s", service, creator, exc)
        return []

    if isinstance(data, dict):
        for key in ("posts", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key][:limit]
    if isinstance(data, list):
        return data[:limit]

    log.warning("⚠️  Unexpected response format for %s/%s", service, creator)
    return []
