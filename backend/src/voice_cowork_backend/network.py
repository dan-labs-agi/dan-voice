from fastapi import Request
from slowapi.util import get_remote_address

_CF_CONNECTING_IP_HEADER = "cf-connecting-ip"


def get_client_ip(request: Request) -> str:
    return request.headers.get(_CF_CONNECTING_IP_HEADER) or get_remote_address(request)


def is_tunnel_request(request: Request) -> bool:
    return _CF_CONNECTING_IP_HEADER in request.headers
