"""
每周保活 — 调一次 Webull API 防止 token 因 15 天空闲过期
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(__file__))


def main():
    path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(path) as f:
        config = yaml.safe_load(f)

    wc = config["webull"]["prod"]

    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient

    api_client = ApiClient(wc["app_key"], wc["app_secret"], "sg")
    api_client.add_endpoint("sg", wc["endpoint"])
    trade_client = TradeClient(api_client)

    res = trade_client.account_v2.get_account_list()
    if res.status_code == 200:
        print(f"  ✓ Keepalive OK — token still alive")
    else:
        print(f"  ⚠️ Keepalive failed: {res.status_code}")


if __name__ == "__main__":
    main()
