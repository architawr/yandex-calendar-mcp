"""Проверка состава Git и пакетов без вывода содержимого секретов."""
from pathlib import Path
import json
import re
import subprocess
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"config.local.json", ".env", ".venv", ".calendar-trash", "__pycache__"}


def check(name, raw, needles):
    path = Path(name)
    if any(part in FORBIDDEN or part.startswith(".env.") for part in path.parts) or path.suffix in {".ics", ".pyc", ".pem", ".key", ".log"}:
        raise SystemExit(f"Запрещённый файл: {name}")
    if any(value and value in raw for value in needles):
        raise SystemExit(f"Личные данные в файле: {name}")
    patterns = [rb"gh[pousr]_[A-Za-z0-9]{30,}", rb"github_pat_[A-Za-z0-9_]{30,}",
                rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", rb"/Users/[A-Za-z0-9_.-]+/"]
    if any(re.search(pattern, raw) for pattern in patterns):
        raise SystemExit(f"Подозрительное содержимое: {name}")


def main():
    needles = [str(Path.home()).encode()]
    private = ROOT / "config.local.json"
    if private.exists():
        settings = json.loads(private.read_text())
        if settings.get("username"):
            needles.append(settings["username"].encode())
        for field in ("allowed_calendars", "allowed_write_calendars"):
            needles.extend(value.encode() for value in settings.get(field, []))
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    names = [name for name in result.stdout.decode().split("\0") if name]
    if not names:
        raise SystemExit("Нет файлов для проверки")
    for name in names:
        check(name, (ROOT / name).read_bytes(), needles)
    archives = list((ROOT / "dist").glob("*.whl")) + list((ROOT / "dist").glob("*.tar.gz"))
    if not archives:
        raise SystemExit("Нет пакетов: сначала выполните uv build")
    for archive in archives:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as data:
                for name in data.namelist():
                    check(name, data.read(name), needles)
        else:
            with tarfile.open(archive) as data:
                for item in data:
                    if item.isfile():
                        check(item.name, data.extractfile(item).read(), needles)
    print(f"Проверено файлов Git: {len(names)}; пакетов: {len(archives)}. Локальные данные не обнаружены.")


if __name__ == "__main__":
    main()
