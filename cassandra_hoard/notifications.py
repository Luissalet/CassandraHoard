"""Optional, best-effort Boop transport for incident lifecycle notifications."""

from urllib.parse import urlsplit

import httpx


def _valid_base(value: str, *, secret: bool = False) -> bool:
    try:
        url = urlsplit(value)
        # Never put credentials in URLs. Permit plaintext only on loopback for
        # the transport carrying a project key; remote Boop must use TLS.
        return bool(
            url.hostname and url.scheme in ("http", "https")
            and not url.username and not url.password and not url.query and not url.fragment
            and (not secret or url.scheme == "https" or url.hostname in ("localhost", "127.0.0.1", "::1"))
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        return False


class BoopNotifier:
    def __init__(self, config, *, client=None):
        self._url = config.boop_url.rstrip("/")
        self._key = config.boop_api_key
        self._public_url = config.public_url.rstrip("/")
        self._client = client
        self.accepted = 0
        self.failed = 0
        self.last_error = None
        self.enabled = False
        if not config.boop_enabled:
            return
        if not config.bus:
            self.last_error = "Boop requires CASSANDRA_BUS=1"
        elif not self._key or not self._key.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in self._key):
            self.last_error = "Boop project key missing or invalid"
        elif not _valid_base(self._url, secret=True) or not _valid_base(self._public_url):
            self.last_error = "Boop or Cassandra public URL missing or invalid"
        else:
            self.enabled = True

    def send(self, type_: str, data: dict) -> bool:
        if not self.enabled or type_ not in ("cassandra.incident.opened", "cassandra.incident.closed"):
            return False
        iid = data.get("incident_id")
        if not isinstance(iid, int) or isinstance(iid, bool) or iid < 1:
            return False
        phase = "opened" if type_.endswith(".opened") else "closed"
        fingerprint = f"cassandra:incident:{iid}"
        # Do not forward logs, service names, command lines or inferred causes.
        payload = {
            "title": f"Cassandra: incident #{iid} {phase}",
            "body": "Open Cassandra for details.",
            "level": "warning" if phase == "opened" else "info",
            "source": "cassandra",
            "external_id": f"{fingerprint}:{phase}",
            "fingerprint": fingerprint,
            "actions": [{"label": "Open Cassandra", "url": f"{self._public_url}/#/incidents/{iid}"}],
        }
        client = self._client or httpx.Client(timeout=3.0, trust_env=False, follow_redirects=False)
        try:
            response = client.post(
                f"{self._url}/api/v1/events", json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=3.0, follow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                self.last_error = f"Boop HTTP {response.status_code}"
                self.failed += 1
                return False
            self.accepted += 1
            self.last_error = None
            return True
        except (httpx.HTTPError, ValueError):
            # Exception text and response bodies may contain secrets.
            self.failed += 1
            self.last_error = "Boop request failed"
            return False
        finally:
            if self._client is None:
                client.close()

    def status(self) -> dict:
        return {"enabled": self.enabled, "accepted": self.accepted,
                "failed": self.failed, "last_error": self.last_error}
