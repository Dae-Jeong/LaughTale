"""격리 실험 Secret 생성 전용입니다. 기본 실행은 검증만 하며 값을 출력하지 않습니다."""

import argparse
import io
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from platform_contracts.wire import Profile
from sqlalchemy.engine import make_url

CONTEXT = "k3d-laughtale-local"
NAMESPACE = "laughtale-chat-external"
RUN_ID = "g4-local-first"
SECRET_NAMES = ("chat-lab-env", "platform-mock-lab-env")
PART_OF = "app.kubernetes.io/part-of"
CHAT_KEYS = {
    "EXTERNAL_ENABLED",
    "DEV_SESSIONS_ENABLED",
    "EXTERNAL_CONTROL_TOKEN",
    "MOCK_API_URL",
    "MOCK_API_TOKEN",
    "EXTERNAL_CONNECTION_CREDENTIALS",
}
MOCK_KEYS = {
    "MOCK_SERVER_PORT",
    "MOCK_CHAT_BASE_URL",
    "MOCK_CONTROL_TOKEN",
    "MOCK_SERVICE_TOKEN",
    "MOCK_CONNECTION_TOKENS",
}


class Refused(Exception):
    """외부 입력이나 자격증명을 예외 본문으로 복사하지 않습니다."""


def command(args: list[str], *, data: str | None = None) -> str:
    result = subprocess.run(
        args, input=data, text=True, capture_output=True, timeout=20, check=False
    )
    if result.returncode:
        raise Refused()
    return result.stdout


def read_env(path: Path, repo: Path) -> dict[str, str]:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise Refused()
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > 65536
    ):
        raise Refused()
    command(["git", "-C", str(repo), "check-ignore", "-q", "--", str(path)])
    source = path.read_text(encoding="utf-8")
    names = set()
    for binding in parse_stream(io.StringIO(source)):
        if binding.error:
            raise Refused()
        if binding.key is not None:
            if binding.key in names:
                raise Refused()
            names.add(binding.key)
    values = dotenv_values(stream=io.StringIO(source), interpolate=False)
    checked: dict[str, str] = {}
    for name, value in values.items():
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            or not isinstance(value, str)
            or any(char in value for char in "\x00\r\n")
        ):
            raise Refused()
        checked[name] = value
    return checked


def documents(chat: dict[str, str], mock: dict[str, str], db_value: str) -> list[dict]:
    if set(chat) != CHAT_KEYS or set(mock) != MOCK_KEYS:
        raise Refused()
    if (
        chat["EXTERNAL_ENABLED"] != "true"
        or chat["DEV_SESSIONS_ENABLED"] != "true"
        or chat["MOCK_API_URL"] != "http://127.0.0.1:18087"
        or mock["MOCK_SERVER_PORT"] != "18087"
        or mock["MOCK_CHAT_BASE_URL"] != "http://127.0.0.1:18082"
    ):
        raise Refused()
    credentials = json.loads(chat["EXTERNAL_CONNECTION_CREDENTIALS"])
    tokens = json.loads(mock["MOCK_CONNECTION_TOKENS"])
    expected = {
        str(
            uuid5(NAMESPACE_URL, f"laughtale:external-smoke:{RUN_ID}:{profile.value}")
        ): profile.value
        for profile in Profile
    }
    if (
        not isinstance(credentials, dict)
        or not isinstance(tokens, dict)
        or set(credentials) != set(expected)
        or set(tokens) != set(expected)
    ):
        raise Refused()
    for id, profile in expected.items():
        value = credentials[id]
        if (
            not isinstance(value, dict)
            or set(value) != {"profile", "token"}
            or value["profile"] != profile
            or value["token"] != tokens[id]
        ):
            raise Refused()
    if chat["MOCK_API_TOKEN"] != mock["MOCK_SERVICE_TOKEN"]:
        raise Refused()
    secrets = [
        chat["EXTERNAL_CONTROL_TOKEN"],
        mock["MOCK_CONTROL_TOKEN"],
        mock["MOCK_SERVICE_TOKEN"],
        *tokens.values(),
    ]
    if any(
        not isinstance(value, str)
        or not 24 <= len(value) <= 256
        or not all(33 <= ord(char) <= 126 for char in value)
        for value in secrets
    ) or len(set(secrets)) != len(secrets):
        raise Refused()
    db = make_url(db_value)
    if (
        (db.drivername, db.host, db.port, db.database, db.username)
        != ("postgresql+asyncpg", "127.0.0.1", 5440, "laughtale_chat", "chat_writer")
        or not db.password
        or db.query
    ):
        raise Refused()
    # 암호는 메모리와 kubectl stdin에만 전달하며 명령 인자/파일로 내보내지 않습니다.
    chat_values = chat | {
        "DB_PRIMARY_URL": db.set(host="chat-primary", port=5432).render_as_string(
            hide_password=False
        )
    }
    result = []
    for name, values in zip(SECRET_NAMES, (chat_values, mock), strict=True):
        result.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "labels": {PART_OF: NAMESPACE, "laughtale.run": RUN_ID},
                },
                "type": "Opaque",
                "immutable": True,
                "stringData": values,
            }
        )
    return result


def create(documents: list[dict], *, execute=command, apply: bool = False) -> None:
    prefix = ["kubectl", "--context", CONTEXT, "--namespace", NAMESPACE]
    namespace = json.loads(
        execute(prefix + ["get", "namespace", NAMESPACE, "-o", "json"])
    )
    metadata = namespace.get("metadata", {})
    if (
        metadata.get("name") != NAMESPACE
        or metadata.get("labels", {}).get(PART_OF) != NAMESPACE
        or metadata.get("labels", {}).get("pod-security.kubernetes.io/enforce")
        != "restricted"
        or namespace.get("status", {}).get("phase") != "Active"
    ):
        raise Refused()
    # 둘 중 하나라도 존재하면 전체 생성을 거절하며 apply/update/replace를 쓰지 않습니다.
    existing = execute(
        prefix + ["get", "secret", *SECRET_NAMES, "--ignore-not-found", "-o", "name"]
    )
    if existing.strip():
        raise Refused()
    if apply:
        # Kubernetes List 생성은 원자적이지 않습니다. 실패 시 자동 삭제/덮어쓰기는 하지 않습니다.
        execute(
            prefix + ["create", "-f", "-"],
            data=json.dumps({"apiVersion": "v1", "kind": "List", "items": documents}),
        )


def self_test() -> int:
    ids = {
        str(
            uuid5(NAMESPACE_URL, f"laughtale:external-smoke:{RUN_ID}:{p.value}")
        ): p.value
        for p in Profile
    }
    tokens = {id: f"synthetic-token-{index:024}" for index, id in enumerate(ids)}
    chat = {
        "EXTERNAL_ENABLED": "true",
        "DEV_SESSIONS_ENABLED": "true",
        "EXTERNAL_CONTROL_TOKEN": "synthetic-chat-control-000000",
        "MOCK_API_URL": "http://127.0.0.1:18087",
        "MOCK_API_TOKEN": "synthetic-mock-service-000000",
        "EXTERNAL_CONNECTION_CREDENTIALS": json.dumps(
            {id: {"profile": p, "token": tokens[id]} for id, p in ids.items()}
        ),
    }
    mock = {
        "MOCK_SERVER_PORT": "18087",
        "MOCK_CHAT_BASE_URL": "http://127.0.0.1:18082",
        "MOCK_CONTROL_TOKEN": "synthetic-mock-control-000000",
        "MOCK_SERVICE_TOKEN": chat["MOCK_API_TOKEN"],
        "MOCK_CONNECTION_TOKENS": json.dumps(tokens),
    }
    db = "postgresql+asyncpg://chat_writer:synthetic-password@127.0.0.1:5440/laughtale_chat"
    built = documents(chat, mock, db)
    parsed = make_url(built[0]["stringData"]["DB_PRIMARY_URL"])
    assert (
        parsed.host == "chat-primary"
        and parsed.port == 5432
        and parsed.password == "synthetic-password"
    )
    count = 1
    for bad_chat, bad_mock, bad_db in [
        (chat | {"UNEXPECTED": "value"}, mock, db),
        (chat | {"EXTERNAL_CONNECTION_CREDENTIALS": "{}"}, mock, db),
        (chat, mock | {"MOCK_CONNECTION_TOKENS": "{}"}, db),
        (chat, mock | {"MOCK_SERVICE_TOKEN": "different-token-000000000000"}, db),
        (chat | {"EXTERNAL_CONTROL_TOKEN": "invalid whitespace token 000"}, mock, db),
        (chat, mock, db.replace("chat_writer", "chat_reader")),
        (chat, mock, db.replace(":5440", ":5433")),
        (chat, mock, db + "?sslmode=disable"),
    ]:
        try:
            documents(bad_chat, bad_mock, bad_db)
        except Refused:
            count += 1
        else:
            raise AssertionError()
    calls = []

    def fake(args, *, data=None):
        calls.append((args, data))
        assert "synthetic-password" not in " ".join(args)
        if "namespace" in args[args.index("get") + 1 :] if "get" in args else False:
            return json.dumps(
                {
                    "metadata": {
                        "name": NAMESPACE,
                        "labels": {
                            PART_OF: NAMESPACE,
                            "pod-security.kubernetes.io/enforce": "restricted",
                        },
                    },
                    "status": {"phase": "Active"},
                }
            )
        if "get" in args:
            return ""
        assert isinstance(data, str)
        assert args[-3:] == ["create", "-f", "-"] and json.loads(data)["items"] == built
        return ""

    create(built, execute=fake)
    assert len(calls) == 2
    create(built, execute=fake, apply=True)
    assert len(calls) == 5
    count += 2

    def existing(args, *, data=None):
        return "secret/chat-lab-env" if "secret" in args else fake(args, data=data)

    try:
        create(built, execute=existing, apply=True)
    except Refused:
        count += 1
    else:
        raise AssertionError()
    for bad_namespace in (
        {"metadata": {"name": "default"}, "status": {"phase": "Active"}},
        {
            "metadata": {"name": NAMESPACE, "labels": {PART_OF: "other"}},
            "status": {"phase": "Active"},
        },
    ):

        def wrong_namespace(args, *, data=None, result=bad_namespace):
            assert "create" not in args
            return json.dumps(result)

        try:
            create(built, execute=wrong_namespace, apply=True)
        except Refused:
            count += 1
        else:
            raise AssertionError()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-secrets", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            if args.apply_secrets:
                raise Refused()
            print(f"Self-test passed: {self_test()} cases; no cluster access.")
            return 0
        repo = Path(__file__).resolve().parents[3]
        root = repo / ".artifacts" / "chat-external" / RUN_ID
        chat = read_env(root / "chat.env", repo)
        mock = read_env(root / "mock.env", repo)
        source = read_env(repo / "services" / "chat" / ".env", repo)
        built = documents(chat, mock, source.get("DB_PRIMARY_URL", ""))
        create(built, apply=args.apply_secrets)
        print(
            "Created two immutable lab Secrets."
            if args.apply_secrets
            else "Validation passed; no Secrets created."
        )
        return 0
    except Exception:  # noqa: BLE001 -- CLI 경계에서 secret을 포함할 수 있는 예외 출력을 차단합니다.
        print(
            "Secret preparation refused; inspect target, private files, namespace labels and existing Secret names. A failed create may be partial; no cleanup or overwrite was attempted.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
