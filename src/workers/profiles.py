import json
import re
import threading
import uuid
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from workers.download_store import (
    ensure_config_seeded,
    get_stored_config,
    init_database,
    update_stored_defaults,
)


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    output_root: Path
    database_path: Path
    pin_salt: str = ""
    pin_hash: str = ""

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_salt and self.pin_hash)


class ProfileManager:
    def __init__(
        self,
        registry_path: Path,
        default_output_root: Path,
        default_database_path: Path,
    ) -> None:
        self.registry_path = registry_path.expanduser().resolve()
        self.lock = threading.RLock()
        self.profiles: Dict[str, Profile] = {}
        self.active_profile_id = "default"
        self._load(default_output_root, default_database_path)

    def _load(self, default_output_root: Path, default_database_path: Path) -> None:
        payload = self._read_registry()
        entries = payload.get("profiles") if payload else None
        if isinstance(entries, list):
            for entry in entries:
                profile = self._profile_from_entry(entry)
                if profile is not None:
                    self.profiles[profile.profile_id] = profile
        self._discover_profiles()
        existing_default = self.profiles.get("default")
        default_profile_root = self._profiles_root / "default"
        default_output_root = self._downloads_root / "default"
        default_profile = Profile(
            profile_id="default",
            name=existing_default.name if existing_default else "default",
            output_root=(
                existing_default.output_root
                if existing_default
                and existing_default.output_root
                in {
                    default_output_root,
                    default_profile_root,
                    default_profile_root / "downloads",
                }
                else self._output_root_for_profile(default_profile_root, "default")
            ),
            database_path=default_profile_root / "downloads.sqlite3",
            pin_salt=existing_default.pin_salt if existing_default else "",
            pin_hash=existing_default.pin_hash if existing_default else "",
        )
        self.profiles["default"] = default_profile
        requested_active = str((payload or {}).get("active_profile_id") or "default")
        if requested_active in self.profiles:
            self.active_profile_id = requested_active
        self._initialize_all()
        self._write_registry()

    @property
    def _profiles_root(self) -> Path:
        return self.registry_path.parent / "profiles"

    @property
    def _downloads_root(self) -> Path:
        return self.registry_path.parent / "downloads"

    def _discover_profiles(self) -> None:
        if not self._profiles_root.is_dir():
            return
        for profile_root in self._profiles_root.iterdir():
            if not profile_root.is_dir() or profile_root.name.startswith("."):
                continue
            profile_id = profile_root.name
            existing_profile = self.profiles.get(profile_id)
            output_root = self._output_root_for_profile(profile_root, profile_id)
            database_path = profile_root / "downloads.sqlite3"
            self.profiles[profile_id] = Profile(
                profile_id=profile_id,
                name=existing_profile.name if existing_profile else profile_id,
                output_root=output_root,
                database_path=database_path,
                pin_salt=existing_profile.pin_salt if existing_profile else "",
                pin_hash=existing_profile.pin_hash if existing_profile else "",
            )

    def _output_root_for_profile(self, profile_root: Path, profile_id: str) -> Path:
        download_output_root = self._downloads_root / profile_id
        if not profile_root.is_dir():
            return download_output_root
        ignored_names = {"downloads", "downloads.sqlite3", "cookies.txt"}
        has_content_at_profile_root = any(
            child.name not in ignored_names for child in profile_root.iterdir()
        )
        if has_content_at_profile_root:
            return profile_root
        return download_output_root

    def _read_registry(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {}
        try:
            raw = self.registry_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return parsed

    def _profile_from_entry(self, entry: Any) -> Optional[Profile]:
        if not isinstance(entry, dict):
            return None
        profile_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        output_root = str(entry.get("output_root") or "").strip()
        database_path = str(entry.get("database_path") or "").strip()
        if not profile_id or not name or not output_root or not database_path:
            return None
        return Profile(
            profile_id,
            name,
            Path(output_root).expanduser().resolve(),
            Path(database_path).expanduser().resolve(),
            str(entry.get("pin_salt") or ""),
            str(entry.get("pin_hash") or ""),
        )

    def _initialize_all(self) -> None:
        for profile in self.profiles.values():
            profile.output_root.mkdir(parents=True, exist_ok=True)
            init_database(str(profile.database_path))
            ensure_config_seeded(
                str(profile.database_path),
                {
                    "output_root": str(profile.output_root),
                    "database_path": str(profile.database_path),
                },
            )
            update_stored_defaults(
                str(profile.database_path),
                {
                    "output_root": str(profile.output_root),
                    "database_path": str(profile.database_path),
                },
            )

    def _write_registry(self) -> None:
        entries: List[Dict[str, str]] = []
        for profile in self.list_profiles():
            entries.append(
                {
                    "id": profile.profile_id,
                    "name": profile.name,
                    "output_root": str(profile.output_root),
                    "database_path": str(profile.database_path),
                    "pin_salt": profile.pin_salt,
                    "pin_hash": profile.pin_hash,
                }
            )
        payload = {"active_profile_id": self.active_profile_id, "profiles": entries}
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.registry_path.with_suffix(
            self.registry_path.suffix + ".tmp"
        )
        temporary_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        temporary_path.replace(self.registry_path)

    def _profile_sort_key(self, profile: Profile) -> Tuple[bool, str]:
        return profile.profile_id != "default", profile.name.casefold()

    def list_profiles(self) -> List[Profile]:
        profiles = list(self.profiles.values())
        profiles.sort(key=self._profile_sort_key)
        return profiles

    def get_active(self) -> Profile:
        with self.lock:
            return self.profiles[self.active_profile_id]

    def switch(self, profile_id: str) -> Profile:
        with self.lock:
            if profile_id not in self.profiles:
                raise ValueError("Unknown profile")
            self.active_profile_id = profile_id
            self._write_registry()
            return self.profiles[profile_id]

    def create(self, name: str) -> Profile:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Profile name is required")
        with self.lock:
            self._ensure_unique_name(clean_name)
            profile_id = self._new_profile_id(clean_name)
            profile_root = self._profiles_root / profile_id
            profile = Profile(
                profile_id=profile_id,
                name=clean_name,
                output_root=self._downloads_root / profile_id,
                database_path=profile_root / "downloads.sqlite3",
            )
            self.profiles[profile_id] = profile
            profile.output_root.mkdir(parents=True, exist_ok=True)
            init_database(str(profile.database_path))
            ensure_config_seeded(
                str(profile.database_path),
                {
                    "output_root": str(profile.output_root),
                    "database_path": str(profile.database_path),
                },
            )
            self.active_profile_id = profile_id
            self._write_registry()
            return profile

    def rename_active(self, name: str) -> Profile:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Profile name is required")
        with self.lock:
            self._ensure_unique_name(clean_name, ignored_id=self.active_profile_id)
            current = self.profiles[self.active_profile_id]
            renamed = Profile(
                current.profile_id,
                clean_name,
                current.output_root,
                current.database_path,
                current.pin_salt,
                current.pin_hash,
            )
            self.profiles[current.profile_id] = renamed
            self._write_registry()
            return renamed

    def set_pin(self, profile_id: str, pin: str) -> Profile:
        clean_pin = str(pin or "").strip()
        if clean_pin and (
            not clean_pin.isdigit() or len(clean_pin) < 4 or len(clean_pin) > 12
        ):
            raise ValueError("PIN must be 4 to 12 digits")
        with self.lock:
            if profile_id not in self.profiles:
                raise ValueError("Unknown profile")
            current = self.profiles[profile_id]
            if clean_pin:
                salt = secrets.token_hex(16)
                pin_hash = self._hash_pin(salt, clean_pin)
            else:
                salt = ""
                pin_hash = ""
            updated = Profile(
                current.profile_id,
                current.name,
                current.output_root,
                current.database_path,
                salt,
                pin_hash,
            )
            self.profiles[profile_id] = updated
            self._write_registry()
            return updated

    def verify_pin(self, profile_id: str, pin: str) -> bool:
        with self.lock:
            profile = self.profiles.get(profile_id)
            if profile is None:
                return False
            if not profile.has_pin:
                return True
            return secrets.compare_digest(
                profile.pin_hash,
                self._hash_pin(profile.pin_salt, str(pin or "")),
            )

    def _hash_pin(self, salt: str, pin: str) -> str:
        return hashlib.sha256(f"{salt}:{pin}".encode("utf-8")).hexdigest()

    def load_config(self, profile: Profile) -> Dict[str, Any]:
        stored = get_stored_config(str(profile.database_path))
        return {
            "defaults": stored["defaults"],
            "download_settings": stored["download_settings"],
            "youtube": stored["youtube"],
            "podcasts": stored["podcasts"],
        }

    def _ensure_unique_name(self, name: str, ignored_id: Optional[str] = None) -> None:
        for profile in self.profiles.values():
            if profile.profile_id == ignored_id:
                continue
            if profile.name.casefold() == name.casefold():
                raise ValueError("A profile with that name already exists")

    def _new_profile_id(self, name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if not slug:
            slug = "profile"
        candidate = slug
        while candidate in self.profiles:
            candidate = f"{slug}-{uuid.uuid4().hex[:6]}"
        return candidate
