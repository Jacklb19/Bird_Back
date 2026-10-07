"""Sign private uploads and verify their exact path and WAV format before linking."""
import os
import struct
from uuid import UUID

import httpx
from fastapi import HTTPException


class AudioStorage:
    def __init__(self) -> None:
        self.base = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.secret = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self.bucket = os.environ.get("SUPABASE_AUDIO_BUCKET", "")

    @staticmethod
    def path(owner: UUID, detection: UUID) -> str:
        return f"{owner}/{detection}.wav"

    def client(self) -> httpx.Client:
        if not self.base or not self.secret or not self.bucket:
            raise HTTPException(503, "Audio storage is not configured")
        return httpx.Client(base_url=f"{self.base}/storage/v1", headers={"apikey": self.secret, "Authorization": f"Bearer {self.secret}"}, timeout=20)

    def sign(self, owner: UUID, detection: UUID) -> dict[str, str]:
        path = self.path(owner, detection)
        try:
            with self.client() as client:
                response = client.post(f"/object/upload/sign/{self.bucket}/{path}", headers={"x-upsert": "true"}, json={})
                response.raise_for_status()
                url = response.json()["url"]
                if not isinstance(url, str) or not url.startswith("/object/upload/sign/"):
                    raise ValueError("Invalid storage response")
                return {"upload_url": f"{self.base}/storage/v1{url}", "audio_path": path}
        except (httpx.HTTPError, KeyError, ValueError):
            raise HTTPException(502, "Audio upload could not be prepared") from None

    def verify(self, owner: UUID, detection: UUID, path: str) -> None:
        if path != self.path(owner, detection):
            raise HTTPException(422, "Invalid audio path")
        try:
            with self.client() as client:
                with client.stream("GET", f"/object/{self.bucket}/{path}") as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 288044:
                            raise ValueError("Audio exceeds expected length")
                if len(content) != 288044 or content[:4] != b"RIFF" or content[8:16] != b"WAVEfmt " or content[36:40] != b"data":
                    raise ValueError("Invalid WAV")
                if struct.unpack_from("<IHHIIHH", content, 16) != (16, 1, 1, 48000, 96000, 2, 16) or struct.unpack_from("<I", content, 40)[0] != 288000:
                    raise ValueError("Unsupported WAV")
        except (httpx.HTTPError, ValueError):
            raise HTTPException(422, "Uploaded audio could not be verified") from None
