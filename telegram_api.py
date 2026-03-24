from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class TelegramAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class TelegramBotAPI:
    token: str
    base_url: str = ""

    def __post_init__(self) -> None:
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self._call(
            "deleteWebhook",
            {"drop_pending_updates": "true" if drop_pending_updates else "false"},
        )

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params = {
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = str(offset)
        return self._call("getUpdates", params)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "chat_id": str(chat_id),
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._call("sendMessage", payload)

    def send_photo(
        self,
        chat_id: int,
        photo_bytes: bytes,
        filename: str = "chart.png",
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._call_multipart(
            "sendPhoto",
            fields=fields,
            file_field="photo",
            filename=filename,
            file_bytes=photo_bytes,
            content_type="image/png",
        )

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self._call("setMyCommands", {"commands": json.dumps(commands)})

    def _call(self, method: str, params: dict[str, str]) -> Any:
        encoded = urlencode(params).encode("utf-8")
        request = Request(
            url=f"{self.base_url}/{method}",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=70) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise TelegramAPIError(
                f"Telegram API error {exc.code}: {message}"
            ) from exc
        except URLError as exc:
            raise TelegramAPIError(f"Telegram API недоступний: {exc}") from exc

        if not payload.get("ok"):
            raise TelegramAPIError(str(payload))
        return payload["result"]

    def _call_multipart(
        self,
        method: str,
        fields: dict[str, str],
        file_field: str,
        filename: str,
        file_bytes: bytes,
        content_type: str,
    ) -> Any:
        boundary = f"----CodexBoundary{uuid4().hex}"
        body = bytearray()

        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
            )
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        request = Request(
            url=f"{self.base_url}/{method}",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=70) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise TelegramAPIError(
                f"Telegram API error {exc.code}: {message}"
            ) from exc
        except URLError as exc:
            raise TelegramAPIError(f"Telegram API недоступний: {exc}") from exc

        if not payload.get("ok"):
            raise TelegramAPIError(str(payload))
        return payload["result"]
