"""Start the web service and announce its listeners once they are accepting connections."""
import argparse
from contextlib import ExitStack
import json

import uvicorn

from app.config import Settings
from app.preflight import bind_http_socket


class WebServer(uvicorn.Server):
    def __init__(self, settings, lan_enabled, network_addresses):
        super().__init__(uvicorn.Config(
            "app.api.main:create_app", factory=True, host=settings.host, port=settings.port,
            access_log=False, workers=1,
        ))
        self.lan_enabled = lan_enabled
        self.network_addresses = network_addresses

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if not self.started or self.should_exit:
            return

        host = self.config.host
        port = self.config.port
        print("\nReCue 网页已就绪", flush=True)
        if host in ("127.0.0.1", "localhost", "0.0.0.0"):
            print(f"  本机打开      http://127.0.0.1:{port}/", flush=True)
        else:
            print(f"  浏览器打开    http://{host}:{port}/", flush=True)
        for address, interface in self.network_addresses:
            print(f"  局域网打开    http://{address}:{port}/  ({interface})", flush=True)
        if self.lan_enabled and not self.network_addresses:
            print("  暂无可用的局域网地址。接入可信局域网后，请重启 ReCue。", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lan-interfaces")
    args = parser.parse_args()
    settings = Settings.load()
    if args.lan_interfaces is None:
        WebServer(settings, False, []).run()
        return

    with ExitStack() as stack:
        sockets = [stack.enter_context(bind_http_socket(settings.host, settings.port))]
        network_addresses = []
        for address, interface in json.loads(args.lan_interfaces):
            try:
                listener = stack.enter_context(bind_http_socket(address, settings.port))
            except OSError as error:
                print(f"无法使用局域网地址 {address} ({interface})：{error}", flush=True)
                continue
            sockets.append(listener)
            network_addresses.append((address, interface))
        WebServer(settings, True, network_addresses).run(sockets=sockets)


if __name__ == "__main__":
    main()
