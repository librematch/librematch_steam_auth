import gevent.monkey

gevent.monkey.patch_all()

import asyncio  # noqa
import asyncio_gevent  # noqa

asyncio.set_event_loop_policy(asyncio_gevent.EventLoopPolicy())

from steam.client import SteamClient  # noqa
from steam.guard import SteamAuthenticator  # noqa

import aiohttp.client_exceptions  # noqa
from aiohttp import ClientSession, web  # noqa
import base64  # noqa
from dataclasses import dataclass, field  # noqa
from dotenv import load_dotenv  # noqa
import json  # noqa
import multidict  # noqa
import os  # noqa
import sys  # noqa
import time  # noqa
import datetime  # noqa

# GAME IDs
APPID = (813780, "age2")


def get_package_dir():
    try:
        # get package directory, when run from poetry
        return __path__[0]
    except NameError:
        # get package directory, when run directly
        return os.getcwd()


@dataclass
class AppTicket:
    ticket: str
    last_update: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RelicLinkSession:
    session_id: str
    last_update: int = field(default_factory=lambda: int(time.time()))


class TicketError(BaseException):
    pass


class LoginError(BaseException):
    pass


class RelicLinkProxy:
    def __init__(self, account_name, password):
        # set up steam
        self.steam = None
        self.steam_account_name = account_name
        self.steam_password = password

        # set up proxy
        with open(os.path.join(get_package_dir(), "api_keys.json")) as api_keys_file:
            self.api_keys = json.load(api_keys_file).values()

        self.webapp = None
        self.app_ticket = None
        self.relic_session = None
        self.http = ClientSession("https://aoe-api.reliclink.com")

    async def steam_login(self):
        with open(
            os.path.join(get_package_dir(), "steam_secrets.json")
        ) as secrets_file:
            self.steam = SteamClient()
            secrets = json.load(secrets_file)

            sa = SteamAuthenticator(secrets)
            two_factor_code = sa.get_code()

            self.steam.login(
                self.steam_account_name,
                password=self.steam_password,
                two_factor_code=two_factor_code,
            )

            print(f"Logged on as: {self.steam.user.name}")

    async def get_encoded_ticket(self):
        # if we don't have a ticket or if it's older than 45 minutes, renew it:
        if self.app_ticket is None or time.time() > self.app_ticket.last_update + 2700:
            # get an app ticket from steam, serialize it and base64 encode it
            try:
                self.app_ticket = AppTicket(
                    base64.standard_b64encode(
                        self.steam.get_encrypted_app_ticket(
                            APPID[0], userdata=b"RLINK"
                        ).encrypted_app_ticket.SerializeToString(deterministic=True)
                    ).decode()
                )
                print("[Relic Login] Refreshed app ticket")
            except AttributeError as exc:
                print("[Relic Login] Could not get encrypted app ticket from steam")
                raise TicketError() from exc

    async def relic_login(self):
        # if we don't have a session or if it's older than 3 minutes renew it:
        if (
            self.relic_session is None
            or time.time() > self.relic_session.last_update + 200
        ):
            try:
                login_request = await self.http.post(
                    "/game/login/platformlogin",
                    data={
                        "accountType": "STEAM",
                        "activeMatchId": "-1",
                        "alias": str(self.steam.user.name),
                        "appID": str(APPID[0]),
                        "auth": self.app_ticket.ticket,
                        "callNum": "0",
                        "clientLibVersion": "169",
                        "connect_id": "",
                        "country": "US",
                        "installationType": "windows",
                        "language": "en",
                        "lastCallTime": "33072262",
                        "macAddress": "57-4F-4C-4F-4C-4F",
                        "majorVersion": "4.0.0",
                        "minorVersion": "0",
                        "platformUserID": str(self.steam.steam_id.as_64),
                        "startGameToken": "",
                        "syncHash": "[3705476802, 2905248376]",
                        "timeoutOverride": "0",
                        "title": str(APPID[1]),
                    },
                )

                content = await login_request.text()

                if f"/steam/{self.steam.steam_id.as_64}" in content:
                    print("[Relic Login] Refreshed session")
                    data = await login_request.json()
                    self.relic_session = RelicLinkSession(data[1])
                else:
                    print("[Relic Login] Relic login failed")
                    raise LoginError()
            except (aiohttp.client_exceptions.ClientError, IndexError) as exc:
                print("[Relic Login] Relic login failed")
                raise LoginError() from exc

    async def update_token(self):
        while True:
            try:
                await self.get_encoded_ticket()
                await self.relic_login()
                await asyncio.sleep(10)
            except (TicketError, LoginError):
                # Wait 5 seconds and try again
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                # Break from loop if we get cancelled
                return

    async def dot(self, _):
        data = {}
        if self.app_ticket is not None:
            data.update(
                {
                    "encrypted_app_token": {
                        "last_update": self.app_ticket.last_update,
                        "utc_string": datetime.datetime.fromtimestamp(
                            self.app_ticket.last_update, tz=datetime.timezone.utc
                        ).isoformat(),
                    }
                }
            )

        if self.relic_session is not None:
            data.update(
                {
                    "relic_session": {
                        "last_update": self.relic_session.last_update,
                        "utc_string": datetime.datetime.fromtimestamp(
                            self.relic_session.last_update, tz=datetime.timezone.utc
                        ).isoformat(),
                    }
                }
            )
        return web.json_response(data=data)

    async def forward_request(self, request):
        api_key = request.headers.get("api_key")
        if api_key not in self.api_keys:
            return web.Response(text="Missing or wrong api key", status=403)

        endpoint = request.match_info.get("endpoint")
        print(f"{request.method} {endpoint}")

        excluded_headers = [
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
            "api_key",
            "host",
            "user-agent",
        ]
        headers = [
            (name, value)
            for (name, value) in request.headers.items()
            if name.lower() not in excluded_headers
        ]

        if request.method == "GET":
            data = multidict.MultiDict(request.rel_url.query)
            data.update(
                {
                    "callNum": 0,
                    "connect_id": self.relic_session.session_id,
                    "lastCallTime": 33072262,
                    "sessionID": self.relic_session.session_id,
                }
            )

            print(f"Request Headers: {str(headers)}")
            print(f"Request Data: {str(data)}")
            response = await self.http.get(f"/{endpoint}", params=data, headers=headers)
            print(f"Response Headers: {str(response.headers)}")
            return web.Response(text=await response.text())
        elif request.method == "POST":
            data = multidict.MultiDict(await request.post())
            data.update(
                {
                    "callNum": 0,
                    "connect_id": self.relic_session.session_id,
                    "lastCallTime": 33072262,
                    "sessionID": self.relic_session.session_id,
                }
            )

            print(f"Request Headers: {str(headers)}")
            print(f"Request Data: {str(data)}")
            response = await self.http.post(f"/{endpoint}", data=data, headers=headers)
            print(f"Response Headers: {str(response.headers)}")
            return web.Response(text=await response.text())
        else:
            return web.Response(text="Method not allowed", status=405)

    async def run_server(self):
        app = web.Application()
        app.add_routes(
            [
                web.route("*", "/relic", self.dot),
                web.route("*", "/relic/{endpoint:[^{}]+}", self.forward_request),
            ]
        )

        self.webapp = web.AppRunner(app)
        try:
            await self.webapp.setup()
            site = web.TCPSite(self.webapp, "0.0.0.0", 5000)
            await site.start()
            print("[aiohttp Server] Site started")

            while True:
                await asyncio.sleep(3600)  # sleep forever
        except asyncio.CancelledError:
            await self.webapp.cleanup()
            return


async def run():
    load_dotenv()

    # Environment
    account_name = os.getenv("STEAM_ACCOUNT_NAME")
    password = os.getenv("STEAM_PASSWORD")  # TODO: Set environment variable

    if not account_name or not password:
        print("Please set your account in the .env")

    proxy = RelicLinkProxy(account_name, password)
    await proxy.steam_login()
    await asyncio.gather(*[proxy.run_server(), proxy.update_token()])


def main():
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, RuntimeError):
        print("Goodbye...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
