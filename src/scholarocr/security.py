"""File-access policy: sources may only be read from authorized directories."""

from __future__ import annotations

from pathlib import Path

from .exceptions import AccessDeniedError, InputError, LimitExceededError


class PathPolicy:
    def __init__(self, allowed_dirs: list[Path], max_file_mb: float):
        self.allowed = [Path(d).expanduser().resolve() for d in allowed_dirs]
        self.max_bytes = int(max_file_mb * 1024 * 1024)

    def check_file(self, raw: str | Path) -> Path:
        """Resolve ``raw`` (following symlinks) and ensure it lies inside an allowed dir."""
        if raw is None or str(raw).strip() == "":
            raise InputError("A file path is required")
        if "\x00" in str(raw):
            raise AccessDeniedError("Invalid path")
        p = Path(str(raw)).expanduser()
        try:
            resolved = p.resolve(strict=True)
        except FileNotFoundError:
            # Do not reveal whether files exist outside the allowed area.
            if not self._inside(p.resolve(strict=False)):
                raise AccessDeniedError(self._deny_message(p)) from None
            raise InputError(f"File not found: {p}") from None
        except (OSError, RuntimeError) as e:
            raise InputError(f"Cannot access {p}: {e.__class__.__name__}") from None
        if not self._inside(resolved):
            raise AccessDeniedError(self._deny_message(p))
        if resolved.is_dir():
            return resolved
        if not resolved.is_file():
            raise InputError(f"Not a regular file: {p}")
        size = resolved.stat().st_size
        if size > self.max_bytes:
            raise LimitExceededError(
                f"File is {size / 1048576:.1f} MB; limit is {self.max_bytes / 1048576:.0f} MB (SCHOLAROCR_MAX_FILE_MB)"
            )
        return resolved

    def check_output_dir(self, raw: str | Path, data_dir: Path) -> Path:
        p = Path(str(raw)).expanduser().resolve(strict=False)
        if not (self._inside(p) or p.is_relative_to(data_dir.resolve())):
            raise AccessDeniedError(self._deny_message(p))
        return p

    def _inside(self, p: Path) -> bool:
        return any(p == root or p.is_relative_to(root) for root in self.allowed)

    def _deny_message(self, p: Path) -> str:
        return (
            f"Access denied: {p} is outside the authorized directories. "
            "Add its folder to SCHOLAROCR_ALLOWED_DIRS to allow it."
        )
