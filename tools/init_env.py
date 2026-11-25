"""Создать локальные настройки для первого запуска, сохранив существующий .env."""

from pathlib import Path
import secrets
import socket

root = Path(__file__).resolve().parents[1]


def free_port(preferred):
    for port in range(preferred, preferred + 50):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"Нет свободного порта рядом с {preferred}")


def main():
    target = root / ".env"
    if target.exists():
        print(".env already exists")
        return
    lines = []
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.endswith("PASSWORD"):
            line = key + "=" + secrets.token_urlsafe(30)
        elif sep and key.endswith("_PORT"):
            line = key + "=" + str(free_port(int(value)))
            print(line)
        lines.append(line)
    with target.open("x", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")
    target.chmod(0o600)


if __name__ == "__main__":
    main()
