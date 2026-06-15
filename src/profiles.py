import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database import ensure_config_seeded, get_stored_config, init_database


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    output_root: Path
    database_path: Path


class ProfileManager:
    def __init__(self, registry_path: Path, default_output_root: Path, default_database_path: Path) -> None:
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
        default_profile = Profile(
            profile_id="default",
            name=existing_default.name if existing_default else "default",
            output_root=default_profile_root / "downloads",
            database_path=default_profile_root / "downloads.sqlite3",
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

    def _discover_profiles(self) -> None:
        if not self._profiles_root.is_dir():
            return
        for profile_root in self._profiles_root.iterdir():
            if not profile_root.is_dir() or profile_root.name.startswith("."):
                continue
            profile_id = profile_root.name
            if profile_id in self.profiles:
                continue
            output_root = profile_root / "downloads"
            database_path = profile_root / "downloads.sqlite3"
            if not output_root.is_dir() and not database_path.is_file():
                continue
            self.profiles[profile_id] = Profile(
                profile_id=profile_id,
                name=profile_id,
                output_root=output_root,
                database_path=database_path,
            )

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
        )

    def _initialize_all(self) -> None:
        for profile in self.profiles.values():
            profile.output_root.mkdir(parents=True, exist_ok=True)
            init_database(str(profile.database_path))
            ensure_config_seeded(
                str(profile.database_path),
                {"output_root": str(profile.output_root), "database_path": str(profile.database_path)},
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
                }
            )
        payload = {"active_profile_id": self.active_profile_id, "profiles": entries}
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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
                output_root=profile_root / "downloads",
                database_path=profile_root / "downloads.sqlite3",
            )
            self.profiles[profile_id] = profile
            profile.output_root.mkdir(parents=True, exist_ok=True)
            init_database(str(profile.database_path))
            ensure_config_seeded(
                str(profile.database_path),
                {"output_root": str(profile.output_root), "database_path": str(profile.database_path)},
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
            renamed = Profile(current.profile_id, clean_name, current.output_root, current.database_path)
            self.profiles[current.profile_id] = renamed
            self._write_registry()
            return renamed

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
