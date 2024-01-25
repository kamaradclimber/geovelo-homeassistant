import logging
import aiohttp
from typing import Optional, Tuple
from aiohttp.client import ClientTimeout
from .const import GEOVELO_API_URL
import re
import base64
from urllib.parse import urlparse, urlunparse

DEFAULT_TIMEOUT = 120
CLIENT_TIMEOUT = ClientTimeout(total=DEFAULT_TIMEOUT)

_LOGGER = logging.getLogger(__name__)


class GeoveloApiError(RuntimeError):
    pass


API_KEY = "0f8c781a-b4b4-4d19-b931-1e82f22e769f"  # this api key does not seem to be a secret since we can find it in developer tools


class GeoveloApi:
    """Api to get data from geovelo"""

    def __init__(
        self, session: Optional[aiohttp.ClientSession] = None, timeout=CLIENT_TIMEOUT
    ) -> None:
        self._timeout = timeout
        self._session = session or aiohttp.ClientSession()

    async def get_authorization_header(self, username, password) -> str:
        url = f"{GEOVELO_API_URL}/api/v1/authentication/geovelo"
        _LOGGER.debug(f"Will contact {url} to get auth token")
        encoded_auth = (
            base64.b64encode(f"{username};{password}".encode("ascii"))
            .strip()
            .decode("ascii")
        )
        headers = {
            "Api-Key": API_KEY,
            "User-Agent": "https://github.com/kamaradclimber/geovelo-homeassistant",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en,en-US;q=0.5",
            # yes it is a semi-column separation in the password
            "Authentication": re.sub("\n", "", encoded_auth),
            "Source": "website",
            "Origin": "https://www.geovelo.fr",
            "Referer": "https://www.geovelo.fr/",
            "Content-Length": "0",
        }
        resp = await self._session.post(url, headers=headers)
        if resp.status != 200:
            raise GeoveloApiError(
                f"Unable to get authorization token for {username}. Status was {resp.status}"
            )

        _LOGGER.debug(f"Got auth data from geovelo ✅")
        return resp.headers["Authorization"]

    async def get_traces(
        self, user_id, authorization_header, start_date, end_date
    ) -> list:
        """All traces in the selected time period"""
        url = f"{GEOVELO_API_URL}/api/v5/users/{user_id}/traces?period=custom&date_start={start_date.strftime('%d-%m-%Y')}&date_end={end_date.strftime('%d-%m-%Y')}&ordering=-start_datetime&page=1&page_size=50"
        _LOGGER.debug(f"Will contact {url} to get traces")
        return await self.fetch_next(url, authorization_header, user_id)


    async def fetch_next(self, url, authorization_header, user_id) -> list:
        headers = {
            "Api-Key": API_KEY,
            "Authorization": authorization_header,
            "Source": "website",
            "User-Agent": "https://github.com/kamaradclimber/geovelo-homeassistant",
        }

        resp = await self._session.get(url, headers=headers)
        if resp.status != 200:
            d = await resp.text()
            _LOGGER.debug(f"Failure {resp}: {d}")
            raise GeoveloApiError(
                f"Unable to get traces for {user_id}, response code was {resp.status}"
            )

        data = await resp.json()
        # _LOGGER.debug("Got geovelo data : %s ", data)
        traces = []
        if data["next"] is not None:
            next = data["next"]
            next_page = urlparse(next)
            # geovelo api returns an http link but their backend makes a 308 which is followed
            # by aiohttp without forwarding creds (curl has that behavior as well)
            next_page = next_page._replace(scheme="https")
            _LOGGER.debug(f"Will contact {next_page} to get more traces")
            traces = await self.fetch_next(urlunparse(next_page), authorization_header, user_id)

        return data["results"] + traces
